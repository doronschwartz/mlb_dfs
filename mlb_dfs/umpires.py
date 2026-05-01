"""Home plate umpire data via UmpScorecards (umpscorecards.com).

Pulls historical accuracy + 'favor' (positive favors home pitcher) per ump.
For each scheduled game today, returns the assigned HP ump (when announced)
along with their season averages — wide strike zone = more Ks expected.
"""
from __future__ import annotations

import time
from collections import defaultdict
from statistics import mean

import requests

UA = {"User-Agent": "mlb_dfs/0.1"}
_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 21600  # 6h — historical data, doesn't move much intra-day


def _get(url: str, params: dict | None = None):
    key = f"{url}?{params}"
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    r = requests.get(url, headers=UA, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    _CACHE[key] = (now, data)
    return data


def _season_averages(season: int = 2026) -> dict[str, dict]:
    """{ump_name: {accuracy_above_x, favor, called_pitches, games}} from
    games this season."""
    try:
        rows = _get(
            "https://umpscorecards.com/api/games",
            params={"start_date": f"{season}-01-01", "end_date": f"{season}-12-31"},
        ).get("rows", [])
    except Exception:
        return {}
    by_ump: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        u = r.get("umpire")
        if u:
            by_ump[u].append(r)
    out: dict[str, dict] = {}
    for ump, games in by_ump.items():
        if not games:
            continue
        out[ump] = {
            "accuracy_above_x": round(mean(g.get("accuracy_above_x") or 0 for g in games), 3),
            "favor": round(mean(g.get("favor") or 0 for g in games), 2),
            "consistency": round(mean(g.get("consistency") or 0 for g in games), 2),
            "games": len(games),
        }
    return out


def umpires_for_date(date_iso: str) -> list[dict]:
    """For each game on date_iso (per UmpScorecards), return assigned HP ump
    + their season-average tendency. UmpScorecards posts this once the
    crew is announced (usually 1-2 days out)."""
    try:
        rows = _get(
            "https://umpscorecards.com/api/games",
            params={"start_date": date_iso, "end_date": date_iso},
        ).get("rows", [])
    except Exception:
        return []
    season = int(date_iso.split("-")[0])
    avgs = _season_averages(season)
    out = []
    for r in rows:
        ump = r.get("umpire")
        avg = avgs.get(ump) if ump else None
        out.append({
            "game_pk": r.get("game_pk"),
            "matchup": f"{r.get('away_team')}@{r.get('home_team')}",
            "ump": ump,
            "season": avg,
            # Pitcher-friendly umps (positive favor toward pitcher) inflate Ks.
            "k_factor": round(1.0 + (avg["favor"] / 50.0), 3) if avg else 1.0,
        })
    return out
