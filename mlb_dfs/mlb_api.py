"""Thin client for the public MLB Stats API (statsapi.mlb.com).

No API key required. Endpoints used:
  - /api/v1/schedule                -> games on a date, probable pitchers
  - /api/v1/game/{pk}/boxscore      -> live & final box scores
  - /api/v1/game/{pk}/feed/live     -> play-by-play (used for game state)
  - /api/v1/people/{id}/stats       -> rolling stat windows for projections
  - /api/v1/teams/{id}/roster       -> team rosters (for draft pool)
"""

from __future__ import annotations

import time
from datetime import date as Date, timedelta
from typing import Any, Iterable

import requests

BASE = "https://statsapi.mlb.com/api/v1"
BASE_V11 = "https://statsapi.mlb.com/api/v1.1"

_session = requests.Session()
_session.headers.update({"User-Agent": "mlb_dfs/0.1"})


class MlbApiError(RuntimeError):
    pass


def _get(path: str, *, base: str = BASE, params: dict | None = None, retries: int = 3) -> dict:
    url = f"{base}{path}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params or {}, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(0.5 * (2**attempt))
    raise MlbApiError(f"GET {url} failed: {last_exc}")


# ----- schedule / slate -------------------------------------------------------


def schedule(d: Date) -> list[dict]:
    """Games on date `d` with probable pitchers + lineup hydration."""
    params = {
        "sportId": 1,
        "date": d.isoformat(),
        "hydrate": "probablePitcher,team,lineups,linescore",
    }
    data = _get("/schedule", params=params)
    games = []
    for entry in data.get("dates", []):
        games.extend(entry.get("games", []))
    return games


def slate(d: Date) -> list[dict]:
    """Normalized slate — one row per game with the bits the UI/draft cares about."""
    rows = []
    for g in schedule(d):
        teams = g.get("teams", {})
        away = teams.get("away", {})
        home = teams.get("home", {})
        rows.append({
            "gamePk": g.get("gamePk"),
            "gameDate": g.get("gameDate"),
            "status": (g.get("status") or {}).get("abstractGameState"),
            "detailedStatus": (g.get("status") or {}).get("detailedState"),
            "venue": (g.get("venue") or {}).get("name"),
            "away": {
                "id": (away.get("team") or {}).get("id"),
                "name": (away.get("team") or {}).get("name"),
                "abbr": (away.get("team") or {}).get("abbreviation"),
                "probablePitcher": _probable(away),
            },
            "home": {
                "id": (home.get("team") or {}).get("id"),
                "name": (home.get("team") or {}).get("name"),
                "abbr": (home.get("team") or {}).get("abbreviation"),
                "probablePitcher": _probable(home),
            },
        })
    return rows


def _probable(side: dict) -> dict | None:
    pp = side.get("probablePitcher")
    if not pp:
        return None
    return {"id": pp.get("id"), "name": pp.get("fullName")}


# ----- box scores -------------------------------------------------------------


def boxscore(game_pk: int) -> dict:
    return _get(f"/game/{game_pk}/boxscore")


def live_feed(game_pk: int) -> dict:
    return _get(f"/game/{game_pk}/feed/live", base=BASE_V11)


# ----- rosters / players ------------------------------------------------------


def teams() -> list[dict]:
    data = _get("/teams", params={"sportId": 1, "activeStatus": "Y"})
    return data.get("teams", [])


def roster(team_id: int) -> list[dict]:
    data = _get(f"/teams/{team_id}/roster", params={"rosterType": "active"})
    return data.get("roster", [])


def player_stats(
    person_id: int,
    *,
    group: str,  # "hitting" or "pitching"
    season: int,
    last_n_days: int | None = None,
) -> dict:
    """Return season-to-date or last-N-days stats for a player."""
    if last_n_days:
        params = {
            "stats": "byDateRange",
            "group": group,
            "startDate": (Date.today() - timedelta(days=last_n_days)).isoformat(),
            "endDate": Date.today().isoformat(),
        }
    else:
        params = {"stats": "season", "group": group, "season": season}
    data = _get(f"/people/{person_id}/stats", params=params)
    splits = []
    for s in data.get("stats", []):
        splits.extend(s.get("splits", []))
    if not splits:
        return {}
    return splits[0].get("stat", {})


def players_in_slate(d: Date) -> dict[int, dict]:
    """Map of player_id -> {name, primaryPosition, teamId} for everyone on a roster
    of a team playing today. This is the draft pool."""
    games = schedule(d)
    team_ids: set[int] = set()
    for g in games:
        for side in ("home", "away"):
            t = ((g.get("teams") or {}).get(side) or {}).get("team") or {}
            if t.get("id"):
                team_ids.add(t["id"])

    pool: dict[int, dict] = {}
    for tid in team_ids:
        for r in roster(tid):
            person = r.get("person") or {}
            pid = person.get("id")
            if not pid:
                continue
            pool[pid] = {
                "id": pid,
                "name": person.get("fullName"),
                "position": (r.get("position") or {}).get("abbreviation"),
                "positionType": (r.get("position") or {}).get("type"),
                "teamId": tid,
            }
    return pool


def lineups_by_date(d: Date, *, game_pks: set[int] | None = None) -> dict[int, dict]:
    """Map of player_id -> lineup status for everyone the slate cares about.

    Each entry: {
      "status": "in"|"out"|"pending",   # in lineup / scratched / not posted
      "game_pk": int | None,
      "team_id": int,
      "is_probable_pitcher": bool,
    }

    Logic:
      - If a team's lineup card is posted, every player on the team's roster
        is either in (their id appears in homePlayers/awayPlayers) or out.
      - Probable pitchers are flagged separately and treated as "in" — they're
        the announced starter; if they're scratched a substitute pitcher
        will appear once lineups post.
      - If lineups aren't yet posted for a game, all of that team's roster
        players are "pending".
    """
    games = schedule(d)
    pool = players_in_slate(d)

    team_to_lineup: dict[int, set[int]] = {}
    team_to_game_pk: dict[int, int] = {}
    team_to_posted: dict[int, bool] = {}
    probable_pitchers: set[int] = set()

    for g in games:
        pk = g.get("gamePk")
        if game_pks is not None and pk not in game_pks:
            continue
        teams = g.get("teams") or {}
        lineups = g.get("lineups") or {}
        for side_key, players_key in (("home", "homePlayers"), ("away", "awayPlayers")):
            side = teams.get(side_key) or {}
            team_id = (side.get("team") or {}).get("id")
            if not team_id:
                continue
            posted_players = lineups.get(players_key)
            if posted_players is not None:
                ids = {p.get("id") for p in posted_players if p.get("id")}
                team_to_lineup.setdefault(team_id, set()).update(ids)
                team_to_posted[team_id] = team_to_posted.get(team_id, False) or True
            else:
                team_to_posted.setdefault(team_id, False)
            team_to_game_pk[team_id] = pk
            pp = side.get("probablePitcher") or {}
            if pp.get("id"):
                probable_pitchers.add(pp["id"])

    out: dict[int, dict] = {}
    for pid, meta in pool.items():
        team_id = meta.get("teamId")
        if not team_id:
            continue
        is_pp = pid in probable_pitchers
        if is_pp:
            status = "in"
        elif team_to_posted.get(team_id):
            status = "in" if pid in team_to_lineup.get(team_id, set()) else "out"
        else:
            status = "pending"
        out[pid] = {
            "status": status,
            "game_pk": team_to_game_pk.get(team_id),
            "team_id": team_id,
            "is_probable_pitcher": is_pp,
        }
    return out


def iter_boxscore_batters(box: dict) -> Iterable[tuple[dict, dict]]:
    """Yield (player_meta, hitting_stats) for everyone with a hitting line."""
    for side in ("home", "away"):
        team = (box.get("teams") or {}).get(side) or {}
        for pid_key, pdata in (team.get("players") or {}).items():
            stats = ((pdata.get("stats") or {}).get("batting")) or {}
            if stats:
                yield pdata.get("person") or {}, stats


def iter_boxscore_pitchers(box: dict) -> Iterable[tuple[dict, dict]]:
    """Yield (player_meta, pitching_stats) for pitchers who appeared.
    Includes a 'isStarter' flag derived from the team's pitchers list order."""
    for side in ("home", "away"):
        team = (box.get("teams") or {}).get(side) or {}
        pitcher_ids = team.get("pitchers") or []
        starter_id = pitcher_ids[0] if pitcher_ids else None
        for pid_key, pdata in (team.get("players") or {}).items():
            stats = ((pdata.get("stats") or {}).get("pitching")) or {}
            if stats:
                person = pdata.get("person") or {}
                person = {**person, "isStarter": person.get("id") == starter_id}
                yield person, stats
