"""Fantrax integration via the unofficial fantraxapi Python library.

Auth: private leagues need a session cookie. Set
    fly secrets set FANTRAX_COOKIE='<paste from browser DevTools>'
The cookie should be the full Cookie header from a logged-in fantrax.com
request — copy as cURL from DevTools, take the -H 'Cookie: ...' value.

Public leagues work without auth.
"""
from __future__ import annotations

import os

import requests
from fantraxapi import FantraxAPI


def _session() -> requests.Session:
    s = requests.Session()
    cookie = os.environ.get("FANTRAX_COOKIE")
    if cookie:
        s.headers.update({"Cookie": cookie})
    s.headers.update({"User-Agent": "Mozilla/5.0 mlb_dfs/0.1"})
    return s


def list_teams(league_id: str) -> list[dict]:
    api = FantraxAPI(league_id, session=_session())
    teams = api.team_lookup()
    out = []
    for tid, team in teams.items():
        out.append({"team_id": tid, "name": getattr(team, "name", str(team))})
    return out


def get_roster(league_id: str, team_id: str | None = None) -> dict:
    """Returns {team_id, team_name, players: [{name, position, team}]}.

    If team_id is None and there's only one team in the league, returns that.
    Otherwise the caller should pick a team_id from list_teams().
    """
    api = FantraxAPI(league_id, session=_session())
    teams = api.team_lookup()
    if not team_id:
        if len(teams) != 1:
            return {"error": "team_id required (multiple teams in league)", "teams": list_teams(league_id)}
        team_id = list(teams.keys())[0]
    team_obj = teams.get(team_id)
    roster = api.team_roster(team_id)
    players = []
    # fantraxapi roster shape varies; fall back to best-effort attribute access.
    raw_players = getattr(roster, "players", None) or getattr(roster, "rows", None) or []
    for p in raw_players:
        players.append({
            "name": getattr(p, "name", None) or getattr(p, "player_name", None) or str(p),
            "position": getattr(p, "position", None) or getattr(p, "pos", None),
            "team": getattr(p, "team", None) or getattr(p, "mlb_team", None),
            "status": getattr(p, "status", None),
        })
    return {
        "team_id": team_id,
        "team_name": getattr(team_obj, "name", "") if team_obj else "",
        "players": players,
    }
