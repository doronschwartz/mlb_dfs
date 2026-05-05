"""Fantrax integration via the unofficial fantraxapi library (v1.x).

Auth (private leagues only — public leagues work without it):

  Option A (simplest): paste a Cookie header into the in-app /api/fantrax/cookie
    endpoint, or set FANTRAX_COOKIE env var. The cookie should be the full
    Cookie value from a logged-in fantrax.com request — open DevTools >
    Network, find any /fxpa/req call, copy the request header `Cookie: ...`
    value (everything after `Cookie: `).

  Option B (more robust, survives sessions longer): use a real browser to
    log into fantrax.com, then export cookies.txt (Netscape format) and
    upload to /data/cache/fantrax_cookies.txt on the server.

The library posts to https://www.fantrax.com/fxpa/req with the league_id
as a query param. Requests use a requests.Session; cookies must be present
in either the session.cookies jar OR the Cookie header.
"""
from __future__ import annotations

import http.cookiejar
import os

import requests
from fantraxapi import FantraxAPI, NotLoggedIn, NotMemberOfLeague

CACHE_DIR = os.environ.get("MLB_DFS_CACHE_DIR", "/data/cache")
_COOKIE_HEADER_FILE = os.path.join(CACHE_DIR, "fantrax_cookie.txt")
_COOKIES_TXT_FILE = os.path.join(CACHE_DIR, "fantrax_cookies.txt")
_UA = "Mozilla/5.0 (mlb_dfs/0.1)"


# ---- cookie storage --------------------------------------------------------


def _read_cookie_header_from_disk() -> str:
    try:
        with open(_COOKIE_HEADER_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def save_cookie(cookie: str) -> None:
    """Persist a pasted `Cookie: ...` header value to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{_COOKIE_HEADER_FILE}.tmp"
    with open(tmp, "w") as f:
        f.write(cookie.strip())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _COOKIE_HEADER_FILE)


def is_authenticated() -> bool:
    return bool(
        os.environ.get("FANTRAX_COOKIE")
        or _read_cookie_header_from_disk()
        or os.path.exists(_COOKIES_TXT_FILE)
    )


# ---- session construction -------------------------------------------------


def _session() -> requests.Session:
    """Build a requests.Session with whatever auth we have. Order of precedence:
    1) FANTRAX_COOKIE env var (header)
    2) cookies.txt on disk (cookiejar)
    3) saved Cookie header on disk
    Public leagues fall through with no auth.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})

    # Prefer cookies.txt — proper jar means requests handles per-request
    # cookie selection, expiration, and Set-Cookie updates from responses.
    if os.path.exists(_COOKIES_TXT_FILE):
        jar = http.cookiejar.MozillaCookieJar(_COOKIES_TXT_FILE)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            s.cookies.update(jar)
            return s
        except Exception:
            pass

    # Fallback: raw Cookie header.
    cookie = os.environ.get("FANTRAX_COOKIE") or _read_cookie_header_from_disk()
    if cookie:
        s.headers.update({"Cookie": cookie})
    return s


# ---- public API -----------------------------------------------------------


def get_league_info(league_id: str, deep: str | None = None) -> dict:
    """Probe Fantrax API methods to find scoring config. When `deep` is set
    to a method name, returns the FULL untrimmed response for that method only
    (so we can spot scoring categories that the trim hid)."""
    from fantraxapi.api import Method, _request
    sess = _session()
    if deep:
        try:
            r = _request(league_id, [Method(deep)], session=sess)
            return {"deep_method": deep, "response": r}
        except Exception as e:
            return {"deep_method": deep, "error": str(e)}

    # Always grab fantasySettings for league metadata.
    base = _request(league_id, [Method("getFantasyLeagueInfo")], session=sess)
    fs = (base or {}).get("fantasySettings", {}) if isinstance(base, dict) else {}

    # Probe a wide set of method names — Fantrax doesn't publish a method list,
    # so we try plausible candidates. Anything that returns useful data lights
    # up. Confirmed-existing methods from the fantraxapi library are first.
    candidates = [
        ("getStandings", {}),                                  # confirmed exists — H2H Cat standings show categories
        ("getStandingsInfo", {}),
        ("getRefObject", {"type": "ScoringFormula"}),
        ("getRefObject", {"type": "ScoringSystem"}),
        ("getRefObject", {"type": "StatCategory"}),
        ("getRefObject", {"type": "ScoringCategory"}),
        ("getRefObject", {"type": "FantasyScoringSystem"}),
        ("getRefObject", {"type": "ScoreCategoryGroup"}),
        ("getRefObject", {"type": "Stat"}),
        ("getLeagueRules", {}),
        ("getLeagueSetup", {}),
        ("getLeagueSettings", {}),
        ("getLeagueInfo", {}),
        ("getScoringSystemInfo", {}),
        ("getScoringSystemRules", {}),
        ("getScoringSystem", {}),
        ("getScoringConfig", {}),
        ("getScoreCategories", {}),
        ("getStatCategories", {}),
        ("getMatchupTable", {}),
        ("getMatchupResult", {}),
        ("getMatchupBreakdown", {}),
        ("getTeamScores", {}),
        ("getCurrentScoringPeriod", {}),
        ("getScoringPeriodInfo", {}),
        ("getMatchups", {}),
        ("getLeagueLeaders", {}),
    ]
    probes = {}
    for name, kw in candidates:
        try:
            r = _request(league_id, [Method(name, **kw)], session=sess)
            # Cap each probe at top-level keys for readability.
            if isinstance(r, dict):
                probes[f"{name}({kw})"] = {"_keys": sorted(r.keys())[:30], "_sample": _trim(r)}
            else:
                probes[f"{name}({kw})"] = {"_type": type(r).__name__, "_sample": str(r)[:300]}
        except Exception as e:
            probes[f"{name}({kw})"] = {"_error": str(e)[:200]}

    return {
        "leagueName": fs.get("leagueName"),
        "subtitle": fs.get("subtitle"),
        "headToHead": fs.get("headToHead"),
        "season": fs.get("season"),
        "sport": fs.get("sport"),
        "_fantasy_settings_keys": sorted(fs.keys()) if isinstance(fs, dict) else [],
        "probes": probes,
    }


def _trim(obj, depth: int = 0, max_depth: int = 4, max_str: int = 300, max_list: int = 30):
    """Recursively trim large API responses to a glanceable shape."""
    if depth > max_depth:
        return f"<{type(obj).__name__}>"
    if isinstance(obj, dict):
        return {k: _trim(v, depth + 1, max_depth, max_str, max_list) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [_trim(v, depth + 1, max_depth, max_str, max_list) for v in obj[:max_list]]
    if isinstance(obj, str):
        return obj[:max_str] + ("…" if len(obj) > max_str else "")
    return obj


def get_current_matchup(league_id: str, team_id: str) -> dict:
    """Returns the user's current week's matchup with per-category values for
    both teams. Uses the H2H Cat standings response which carries period-level
    breakdowns. Returns {} if not found.

    Output shape:
      {
        "period": "Scoring Period: 6",
        "subCaption": "(Mon May 4 - Sun May 10)",
        "category_short_names": ["R","HR","RBI","SB","OPS","QS","K","ERA","WHIP","SVH"],
        "my_team": "...", "opp_team": "...",
        "values": {"R": (my, opp), "HR": (my, opp), ...},
      }
    """
    from fantraxapi.api import Method, _request
    sess = _session()
    raw = _request(league_id, [Method("getStandings")], session=sess)
    if not isinstance(raw, dict):
        return {}
    tables = raw.get("tableList", []) or []
    # Period tables are H2hRotisserie2 type. They appear in the response with
    # the CURRENT period FIRST (highest period number) followed by past periods.
    # Take the first one that contains our team — that's the live matchup.
    for table in tables:
        if table.get("tableType") != "H2hRotisserie2":
            continue
        rows = table.get("rows", [])
        my_row = None
        opp_row = None
        for r in rows:
            tid = (r.get("fixedCells") or [{}])[0].get("teamId")
            if tid == team_id:
                my_row = r
                # Opponent shares matchupId.
                mid = r.get("matchupId")
                for r2 in rows:
                    tid2 = (r2.get("fixedCells") or [{}])[0].get("teamId")
                    if r2.get("matchupId") == mid and tid2 != team_id:
                        opp_row = r2
                        break
                break
        if not my_row or not opp_row:
            continue
        # Header gives column names. First 4 cols are W/L/T/Pts; the rest are cats.
        header_cells = (table.get("header") or {}).get("cells", [])
        cat_short = [c.get("shortName") for c in header_cells]
        my_cells = my_row.get("cells", [])
        opp_cells = opp_row.get("cells", [])
        values = {}
        for i, sn in enumerate(cat_short):
            if sn in ("W", "L", "T", "Pts"):
                continue
            try:
                my_v = float((my_cells[i] or {}).get("toolTip") or (my_cells[i] or {}).get("content") or 0)
                opp_v = float((opp_cells[i] or {}).get("toolTip") or (opp_cells[i] or {}).get("content") or 0)
            except (ValueError, TypeError):
                my_v = opp_v = 0.0
            values[sn] = (my_v, opp_v)
        return {
            "period": table.get("caption"),
            "subCaption": table.get("subCaption"),
            "category_short_names": [s for s in cat_short if s not in ("W","L","T","Pts")],
            "my_team": (my_row.get("fixedCells") or [{}])[0].get("content"),
            "opp_team": (opp_row.get("fixedCells") or [{}])[0].get("content"),
            "values": values,
        }
    return {}


def get_position_counts(league_id: str, team_id: str) -> dict:
    """Returns the active-roster slot config for the given team. Each entry is
    a position short-name (C, 1B, OF, UT, SP, RP, P, BN, IR…) → {min, max, gp}.
    These are the daily slots we have to fill."""
    api = _api(league_id)
    pc = api.position_counts(team_id)
    return {short: {"name": p.name, "min": p.min, "max": p.max, "gp": p.gp}
            for short, p in pc.items()}


def list_teams(league_id: str) -> list[dict]:
    """Returns [{team_id, name, short}] for every team in the league."""
    api = _api(league_id)
    # NB: team_lookup is a @property (returns dict), NOT a method.
    return [
        {"team_id": tid, "name": t.name, "short": t.short}
        for tid, t in api.team_lookup.items()
    ]


def get_roster(league_id: str, team_id: str | None = None) -> dict:
    """Returns {team_id, team_name, players: [...]}.

    When team_id is None and the league has more than one team, returns an
    error payload listing the teams so the caller can pick.
    """
    api = _api(league_id)
    teams = api.team_lookup   # property
    if not team_id:
        if len(teams) != 1:
            return {
                "error": "team_id required (multiple teams in league)",
                "teams": list_teams(league_id),
            }
        team_id = next(iter(teams))
    if team_id not in teams:
        return {"error": f"team_id {team_id} not in this league", "teams": list_teams(league_id)}

    team = teams[team_id]
    slot_counts: dict[str, int] = {}
    players: list[dict] = []
    roster = None
    try:
        roster = api.team_roster(team_id)
        # Build the player list (de-dupe across periods by player_id alone —
        # we want each player ONCE, in their CURRENT slot. Take the first
        # period's slot for each player.)
        seen_pids: set[str] = set()
        for row in roster.rows:
            if not row.position or row.player is None:
                continue
            p = row.player
            if p.id in seen_pids:
                continue
            seen_pids.add(p.id)
            sn = row.position.short_name
            players.append({
                "name": p.name,
                "fantrax_id": p.id,
                "position": p.pos_short_name,
                "team": p.team_short_name,
                "slot": sn,
                "is_bench": sn in ("BN", "Res", "Reserve"),
                "is_ir": sn in ("IR", "InjRes", "Inj Res"),
                "injured": bool(p.injured),
                "day_to_day": bool(p.day_to_day),
                "out": bool(p.out),
            })
        # Slot counts come from position_counts.max / period_days. Don't trust
        # row counts (multi-period dup over-counts).
        # `pc.max` is games-per-period budget (e.g. 4×OF over a 7-day weekly
        # period = max 28). Convert to slot count by dividing by the period
        # length. Find period length from any single-slot position with a
        # filled row to calibrate (typically C or 1B with count=1, where
        # pc.max = period_days).
        # MLB H2H Categories standard active-roster shape — most leagues use
        # exactly this. Hard-coded fallback because position_counts.max units
        # are inconsistent across leagues (sometimes per-period, sometimes
        # times-2, sometimes max-allowed).
        slot_counts = {
            "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1, "CI": 1, "MI": 1,
            "OF": 4, "UT": 2, "SP": 4, "RP": 3, "P": 2,
        }
    except Exception as roster_err:
        # fantraxapi's Roster init is fragile — for some teams it raises
        # IndexError when one of the two methods (GAMES_PER_POS / STATS) returns
        # an empty response. Fall back to fetching the raw response and
        # parsing whatever stat-table comes back.
        from fantraxapi.api import Method, _request
        sess = _session()
        try:
            raw = _request(api.league_id, [
                Method("getTeamRosterInfo", view="STATS", teamId=team_id),
            ], session=sess)
        except Exception:
            raise roster_err
        # raw is a single dict (one method) — extract player rows from it.
        if not isinstance(raw, dict):
            raise roster_err
        for table in (raw.get("tables") or []):
            for row in (table.get("rows") or []):
                if "posId" not in row:
                    continue
                pos_id = row.get("posId")
                pos = (api.positions or {}).get(pos_id)
                slot = pos.short_name if pos else ""
                slot_counts[slot] = slot_counts.get(slot, 0) + 1
                scorer = row.get("scorer")
                if not scorer:
                    continue
                pos_short = scorer.get("posShortNames", "")
                players.append({
                    "name": scorer.get("name"),
                    "fantrax_id": scorer.get("scorerId"),
                    "position": pos_short,
                    "team": scorer.get("teamShortName") or scorer.get("teamName"),
                    "slot": slot,
                    "is_bench": slot in ("BN", "Res", "Reserve"),
                    "is_ir": slot in ("IR", "InjRes", "Inj Res"),
                    "injured": False,
                    "day_to_day": False,
                    "out": False,
                })
    return {
        "team_id": team_id,
        "team_name": team.name,
        "team_short": team.short,
        "active": getattr(roster, "active", None),
        "active_max": getattr(roster, "active_max", None),
        "reserve": getattr(roster, "reserve", None),
        "reserve_max": getattr(roster, "reserve_max", None),
        "slot_counts": slot_counts,    # {position_short_name: count of rows}
        "players": players,
    }


# ---- internal -------------------------------------------------------------


def _api(league_id: str) -> FantraxAPI:
    """Build the FantraxAPI client. Raises a useful error if auth is wrong."""
    try:
        return FantraxAPI(league_id, session=_session())
    except NotLoggedIn:
        raise FantraxAuthError(
            "Fantrax says you're not logged in. The cookie may have expired — "
            "re-paste a fresh Cookie header from a logged-in fantrax.com session."
        )
    except NotMemberOfLeague:
        raise FantraxAuthError(
            "Fantrax says you're not a member of this league. Check the league_id, "
            "or re-paste a cookie from an account that's actually in the league."
        )


class FantraxAuthError(Exception):
    pass
