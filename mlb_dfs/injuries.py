"""ESPN league-wide injury feed — free, no auth, ~330 entries covering both
formal IL placements and Day-To-Day status.

Provides the Yahoo-style 'Injury Report' signal we don't get from MLB Stats
API. MLB exposes transactions (IL placements) but not Day-To-Day flags for
banged-up players who are still active. ESPN's editorial team curates both.

Cached 30min in-memory + 30min on disk. The data updates throughout the day
as beat writers file injury reports, so we don't want stale flags hanging
around — but 30min is plenty fresh for projection use.

Matching: ESPN doesn't expose MLB-AM player IDs in this feed, so we match
on normalized name. Most names are unique league-wide; accent-stripping
('Yandy Díaz' → 'Yandy Diaz') handles ESPN's plain-ASCII spelling.
"""
from __future__ import annotations

import logging
import time
import unicodedata
import urllib.request
import json

from . import disk_cache

ESPN_URL = "https://site.web.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries"

_MEM_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_SEC = 1800  # 30 min


def _norm(name: str) -> str:
    """Strip accents + lowercase + trim. ESPN uses plain ASCII (Díaz → Diaz),
    so MLB-source names need the same normalization for the lookup to hit."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFD", name)
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    return ascii_name.strip().lower()


@disk_cache.cached_disk(_TTL_SEC, namespace="espn_injuries")
def _fetch_raw() -> dict:
    req = urllib.request.Request(ESPN_URL, headers={"User-Agent": "mlb_dfs/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def league_injuries() -> dict[str, list[dict]]:
    """Returns {normalized_player_name: [records]}. A LIST per name so two
    players who share a name (e.g. Max Muncy — Dodgers 3B, healthy, vs the A's
    infielder on the IL) don't overwrite each other; lookup() disambiguates by
    team. Empty dict on failure (don't break projections if ESPN is down)."""
    cached = _MEM_CACHE.get("all")
    now = time.time()
    if cached and (now - cached[0]) < _TTL_SEC:
        return cached[1]
    out: dict[str, list[dict]] = {}
    try:
        data = _fetch_raw()
    except Exception as e:
        logging.warning("ESPN injury feed fetch failed: %s", e)
        return out
    for team in data.get("injuries", []) or []:
        team_name = team.get("displayName", "")
        for inj in team.get("injuries", []) or []:
            ath = inj.get("athlete", {}) or {}
            name = ath.get("displayName", "")
            if not name:
                continue
            details = inj.get("details", {}) or {}
            out.setdefault(_norm(name), []).append({
                "status": inj.get("status", ""),         # "Day-To-Day" | "10-Day-IL" | "15-Day-IL" | "60-Day-IL" | ...
                "type": details.get("type", ""),          # "Hand" / "Quadriceps" / "Oblique" / ...
                "side": details.get("side", ""),
                "return_date": details.get("returnDate", ""),
                "comment": (inj.get("shortComment") or "")[:280],
                "team_name": team_name,
                "as_of": inj.get("date", ""),
            })
    _MEM_CACHE["all"] = (now, out)
    return out


# Team abbreviation → ESPN full display name, so a caller that only has the
# abbr (dynasty) can still disambiguate a name collision.
_ABBR2FULL = {
    "LAA": "Los Angeles Angels", "AZ": "Arizona Diamondbacks", "ARI": "Arizona Diamondbacks",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox", "CHC": "Chicago Cubs",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians", "COL": "Colorado Rockies",
    "DET": "Detroit Tigers", "HOU": "Houston Astros", "KC": "Kansas City Royals",
    "LAD": "Los Angeles Dodgers", "WSH": "Washington Nationals", "WSN": "Washington Nationals",
    "NYM": "New York Mets", "ATH": "Athletics", "OAK": "Athletics", "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres", "SDP": "San Diego Padres", "SEA": "Seattle Mariners",
    "SF": "San Francisco Giants", "SFG": "San Francisco Giants", "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays", "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays", "MIN": "Minnesota Twins", "PHI": "Philadelphia Phillies",
    "ATL": "Atlanta Braves", "CWS": "Chicago White Sox", "CHW": "Chicago White Sox",
    "MIA": "Miami Marlins", "NYY": "New York Yankees", "MIL": "Milwaukee Brewers",
}


def _team_matches(want: str, record_team: str) -> bool:
    """Loose team match: accepts a full name OR an abbreviation."""
    if not want:
        return False
    w = want.strip()
    full = _ABBR2FULL.get(w.upper(), w)          # abbr → full if known
    rt = (record_team or "").lower()
    return full.lower() == rt or full.lower() in rt or rt in full.lower()


def lookup(name: str, team: str | None = None) -> dict | None:
    """Injury record for a player, or None if healthy/unknown. When a name maps
    to MULTIPLE injured players (Max Muncy), `team` (full name or abbr)
    disambiguates; without a team match we return None rather than risk pinning
    the wrong player's IL stint on someone (the Dodgers' Max Muncy is healthy)."""
    if not name:
        return None
    recs = league_injuries().get(_norm(name))
    if not recs:
        return None
    # When we know the team, REQUIRE a match — even for a single record. This is
    # the Max Muncy case: only the A's Muncy is on the IL (one record), so a
    # Dodgers-Muncy lookup must NOT inherit it. A non-match → healthy/unknown.
    if team:
        for r in recs:
            if _team_matches(team, r.get("team_name", "")):
                return r
        return None
    # No team to disambiguate: safe only if the name is unique in the feed.
    return recs[0] if len(recs) == 1 else None


# Status → short badge label used in the UI.
def short_badge(status: str) -> str | None:
    """Map ESPN status to a compact badge label. Returns None when we don't
    want to surface the status (e.g. 60-Day-IL: player is long-gone from
    the pool anyway, no badge needed)."""
    s = (status or "").lower()
    if "day-to-day" in s:
        return "D2D"
    if "10-day" in s:
        return "10-IL"
    if "15-day" in s:
        return "15-IL"
    if "60-day" in s:
        return "60-IL"
    return None
