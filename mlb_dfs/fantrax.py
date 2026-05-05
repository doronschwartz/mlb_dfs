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


def list_teams(league_id: str) -> list[dict]:
    """Returns [{team_id, name, short}] for every team in the league."""
    api = _api(league_id)
    return [
        {"team_id": t.id, "name": t.name, "short": t.short}
        for t in api.teams
    ]


def get_roster(league_id: str, team_id: str | None = None) -> dict:
    """Returns {team_id, team_name, players: [...]}.

    When team_id is None and the league has more than one team, returns an
    error payload listing the teams so the caller can pick.
    """
    api = _api(league_id)
    teams = api.team_lookup()
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
