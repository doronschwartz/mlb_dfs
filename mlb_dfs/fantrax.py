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


def get_league_info(league_id: str) -> dict:
    """Probe several Fantrax API methods to find the league's scoring config.
    fantasySettings only has presentation metadata; scoring rules live in a
    different endpoint. We try the likely candidates and surface whatever
    each returns so we can identify the right one."""
    from fantraxapi.api import Method, _request
    sess = _session()

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
    roster = api.team_roster(team_id)
    players = []
    for row in roster.rows:
        if row.player is None:
            continue   # empty slot
        p = row.player
        slot = row.position.short_name if row.position else ""
        players.append({
            "name": p.name,
            "fantrax_id": p.id,
            "position": p.pos_short_name,            # natural pos eligibility (e.g., "OF,UTIL")
            "team": p.team_short_name,
            "slot": slot,                            # current lineup slot ("C", "1B", "BN", "IR", etc.)
            "is_bench": slot in ("BN", "Res", "Reserve"),
            "is_ir": slot in ("IR", "InjRes", "Inj Res"),
            "injured": bool(p.injured),
            "day_to_day": bool(p.day_to_day),
            "out": bool(p.out),
        })
    return {
        "team_id": team_id,
        "team_name": team.name,
        "team_short": team.short,
        "active": roster.active,
        "active_max": roster.active_max,
        "reserve": roster.reserve,
        "reserve_max": roster.reserve_max,
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
