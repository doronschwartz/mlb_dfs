"""Smart projections.

For each player on the slate we estimate expected fantasy points using:

Hitters:
    14-day rate stats (per game) -> base projection
    + park & opposing-pitcher adjustments
    - if no recent data, fall back to season rates, then league avg

Pitchers (starters only):
    14-day pitching rates -> base projection
    + opposing-team K% / wRC+ proxy via opponent recent runs/game

The point is to be useful, not Vegas-grade. Everything is transparent so the
draft assistant can show *why* it likes a player.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Iterable

from . import mlb_api
from .scoring import HITTER_POINTS, PITCHER_POINTS

LEAGUE_AVG_HITTER_POINTS_PER_GAME = 6.5
LEAGUE_AVG_SP_POINTS_PER_START = 11.0


@dataclass
class Projection:
    player_id: int
    name: str
    team_id: int | None
    position: str | None
    role: str  # "hitter" or "pitcher"
    projected_points: float
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def project_hitter(
    pid: int,
    name: str,
    *,
    team_id: int | None,
    position: str | None,
    season: int,
    opposing_sp_id: int | None,
) -> Projection:
    last14 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=14)
    seasn = mlb_api.player_stats(pid, group="hitting", season=season)

    games_14 = _safe_float(last14.get("gamesPlayed"))
    base_pg = LEAGUE_AVG_HITTER_POINTS_PER_GAME
    notes: list[str] = []

    if games_14 >= 5:
        base_pg = _per_game_hitter_points(last14)
        notes.append(f"14d sample: {int(games_14)} G, {base_pg:.2f} pts/G")
    elif _safe_float(seasn.get("gamesPlayed")) >= 10:
        base_pg = _per_game_hitter_points(seasn)
        notes.append(f"season fallback: {int(_safe_float(seasn.get('gamesPlayed')))} G, {base_pg:.2f} pts/G")
    else:
        notes.append("no sample, league average")

    # Opposing SP adjustment: scale by opponent SP's allowed rate vs league avg.
    sp_factor = 1.0
    if opposing_sp_id:
        sp_season = mlb_api.player_stats(opposing_sp_id, group="pitching", season=season)
        ip = _safe_float(sp_season.get("inningsPitched"))
        if ip >= 20:
            era = _safe_float(sp_season.get("era"), default=4.20)
            whip = _safe_float(sp_season.get("whip"), default=1.30)
            # league baselines: 4.20 ERA / 1.30 WHIP
            sp_factor = (era / 4.20) * 0.6 + (whip / 1.30) * 0.4
            sp_factor = max(0.6, min(sp_factor, 1.45))
            notes.append(f"opposing SP adj x{sp_factor:.2f} (ERA {era:.2f} WHIP {whip:.2f})")

    proj = base_pg * sp_factor
    return Projection(
        player_id=pid,
        name=name,
        team_id=team_id,
        position=position,
        role="hitter",
        projected_points=round(proj, 2),
        components={"base_pg": round(base_pg, 2), "sp_factor": round(sp_factor, 3)},
        notes=notes,
    )


def project_pitcher(
    pid: int,
    name: str,
    *,
    team_id: int | None,
    season: int,
    opponent_team_id: int | None,
) -> Projection:
    last14 = mlb_api.player_stats(pid, group="pitching", season=season, last_n_days=14)
    seasn = mlb_api.player_stats(pid, group="pitching", season=season)

    base = LEAGUE_AVG_SP_POINTS_PER_START
    notes: list[str] = []

    starts_14 = _safe_float(last14.get("gamesStarted"))
    if starts_14 >= 1:
        base = _per_start_pitcher_points(last14)
        notes.append(f"14d sample: {int(starts_14)} GS, {base:.2f} pts/start")
    elif _safe_float(seasn.get("gamesStarted")) >= 2:
        base = _per_start_pitcher_points(seasn)
        notes.append(f"season fallback: {int(_safe_float(seasn.get('gamesStarted')))} GS, {base:.2f} pts/start")
    else:
        notes.append("no sample, league average")

    # Opponent quality factor — use opponent team runs/game as a proxy.
    opp_factor = 1.0
    if opponent_team_id:
        try:
            data = mlb_api._get(
                f"/teams/{opponent_team_id}/stats",
                params={"stats": "season", "group": "hitting", "season": season},
            )
            splits = []
            for s in data.get("stats", []):
                splits.extend(s.get("splits", []))
            if splits:
                stat = splits[0].get("stat", {})
                rpg = _safe_float(stat.get("runs")) / max(_safe_float(stat.get("gamesPlayed"), 1), 1)
                # league avg ~ 4.5 R/G; better offense -> tougher matchup -> lower projection
                opp_factor = (4.5 / max(rpg, 2.5)) ** 0.7
                opp_factor = max(0.7, min(opp_factor, 1.35))
                notes.append(f"opponent adj x{opp_factor:.2f} ({rpg:.2f} R/G)")
        except Exception:
            pass

    proj = base * opp_factor
    return Projection(
        player_id=pid,
        name=name,
        team_id=team_id,
        position="SP",
        role="pitcher",
        projected_points=round(proj, 2),
        components={"base_per_start": round(base, 2), "opp_factor": round(opp_factor, 3)},
        notes=notes,
    )


def _per_game_hitter_points(stats: dict) -> float:
    g = max(_safe_float(stats.get("gamesPlayed")), 1.0)
    h = _safe_float(stats.get("hits"))
    d = _safe_float(stats.get("doubles"))
    t = _safe_float(stats.get("triples"))
    hr = _safe_float(stats.get("homeRuns"))
    singles = max(h - d - t - hr, 0)
    p = HITTER_POINTS
    pts = (
        singles * p["single"]
        + d * p["double"]
        + t * p["triple"]
        + hr * p["homeRun"]
        + _safe_float(stats.get("runs")) * p["run"]
        + _safe_float(stats.get("rbi")) * p["rbi"]
        + _safe_float(stats.get("baseOnBalls")) * p["baseOnBalls"]
        + _safe_float(stats.get("hitByPitch")) * p["hitByPitch"]
        + _safe_float(stats.get("stolenBases")) * p["stolenBase"]
        + _safe_float(stats.get("groundIntoDoublePlay")) * p["groundIntoDoublePlay"]
        + _safe_float(stats.get("strikeOuts")) * p["strikeOut"]
    )
    return pts / g


def _per_start_pitcher_points(stats: dict) -> float:
    gs = max(_safe_float(stats.get("gamesStarted")), 1.0)
    ip = _safe_float(stats.get("inningsPitched"))
    # convert IP "x.y" already lost — use floor/decimal approximation
    outs = int(ip) * 3 + round((ip - int(ip)) * 10)
    p = PITCHER_POINTS
    pts = (
        outs * p["out"]
        + _safe_float(stats.get("strikeOuts")) * p["strikeOut"]
        + _safe_float(stats.get("earnedRuns")) * p["earnedRun"]
        + _safe_float(stats.get("hits")) * p["hitAllowed"]
        + _safe_float(stats.get("baseOnBalls")) * p["walkIssued"]
        + _safe_float(stats.get("hitBatsmen")) * p["hitBatsman"]
    )
    qs = _safe_float(stats.get("qualityStarts"))
    pts += qs * p["qualityStart"]
    pts += _safe_float(stats.get("completeGames")) * p["completeGame"]
    pts += _safe_float(stats.get("shutouts")) * p["shutout"]
    return pts / gs


# In-memory cache for project_slate so repeated polls don't hammer MLB Stats API.
_PROJ_CACHE: dict[tuple, tuple[float, list]] = {}
_PROJ_TTL_SEC = 300  # 5 minutes


def project_slate_cached(d: Date, *, team_filter: set[int] | None = None) -> list["Projection"]:
    """Same as project_slate but memoizes per (date, team_filter) for 5 minutes.

    Projections are based on rolling stat windows + season averages — these
    don't move minute-to-minute, so a TTL cache makes draft polling cheap
    without staleness that matters in practice.
    """
    key = (d.isoformat(), tuple(sorted(team_filter)) if team_filter else None)
    now = time.time()
    cached = _PROJ_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _PROJ_TTL_SEC:
        return cached[1]
    projs = project_slate(d, team_filter=team_filter)
    _PROJ_CACHE[key] = (now, projs)
    return projs


def project_slate(d: Date, *, team_filter: set[int] | None = None) -> list[Projection]:
    """Project everyone playing today. Handles probable SPs + position players.

    If `team_filter` is provided, only project players from those team IDs (used
    when a draft is restricted to a subset of the day's games).
    """
    season = d.year
    games = mlb_api.schedule(d)

    # Build matchup map: team_id -> opposing team_id and opposing SP id
    matchups: dict[int, dict] = {}
    probable_sps: dict[int, dict] = {}  # sp_id -> {team_id, opp_team_id}
    for g in games:
        home = ((g.get("teams") or {}).get("home") or {})
        away = ((g.get("teams") or {}).get("away") or {})
        home_team = (home.get("team") or {}).get("id")
        away_team = (away.get("team") or {}).get("id")
        home_sp = (home.get("probablePitcher") or {}).get("id")
        away_sp = (away.get("probablePitcher") or {}).get("id")
        if home_team and away_team:
            matchups[home_team] = {"opp": away_team, "opp_sp": away_sp}
            matchups[away_team] = {"opp": home_team, "opp_sp": home_sp}
        if home_sp:
            probable_sps[home_sp] = {
                "team_id": home_team, "opp_team_id": away_team,
                "name": (home.get("probablePitcher") or {}).get("fullName"),
            }
        if away_sp:
            probable_sps[away_sp] = {
                "team_id": away_team, "opp_team_id": home_team,
                "name": (away.get("probablePitcher") or {}).get("fullName"),
            }

    pool = mlb_api.players_in_slate(d)
    projections: list[Projection] = []

    # Pitchers — only project the probable starters; relievers aren't draftable as SP
    for sp_id, info in probable_sps.items():
        if team_filter is not None and info["team_id"] not in team_filter:
            continue
        projections.append(project_pitcher(
            sp_id, info["name"] or pool.get(sp_id, {}).get("name", "?"),
            team_id=info["team_id"], season=season,
            opponent_team_id=info["opp_team_id"],
        ))

    # Hitters — everyone non-pitcher in the slate roster pool
    for pid, meta in pool.items():
        if meta.get("positionType") == "Pitcher":
            continue
        if team_filter is not None and meta.get("teamId") not in team_filter:
            continue
        m = matchups.get(meta.get("teamId") or 0, {})
        projections.append(project_hitter(
            pid, meta["name"],
            team_id=meta.get("teamId"),
            position=meta.get("position"),
            season=season,
            opposing_sp_id=m.get("opp_sp"),
        ))

    projections.sort(key=lambda p: p.projected_points, reverse=True)
    return projections
