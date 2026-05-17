"""Baseball Savant 'expected stats' (Statcast) — pulls CSV leaderboards.

Returns per-player {xwoba, xera, etc.} indexed by MLB player_id. Cached 6h.
We avoid pybaseball (heavy dep + slow scrape); the CSV endpoints are free
and stable.
"""
from __future__ import annotations

import csv
import io
import time

import requests

UA = {"User-Agent": "mlb_dfs/0.1"}
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 21600  # 6h in-memory

from . import disk_cache


@disk_cache.cached_disk(86400, namespace="savant_csv")  # 24h on disk
def _csv_disk(url: str) -> list[dict]:
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    text = r.text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def _csv(url: str) -> list[dict]:
    now = time.time()
    cached = _CACHE.get(url)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    try:
        rows = _csv_disk(url)
        _CACHE[url] = (now, rows)
        return rows
    except Exception:
        return []


def _idx(rows: list[dict], id_field: str = "player_id") -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in rows:
        try:
            pid = int(r.get(id_field, ""))
        except (TypeError, ValueError):
            continue
        out[pid] = r
    return out


def pitcher_expected(season: int) -> dict[int, dict]:
    """{pitcher_id: {xera, est_woba, est_ba, ...}}"""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=pitcher&year={season}&min=10&csv=true"
    )
    return _idx(rows)


def batter_expected(season: int) -> dict[int, dict]:
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter&year={season}&min=10&csv=true"
    )
    return _idx(rows)


def lookup_pitcher(pid: int, season: int) -> dict | None:
    return pitcher_expected(season).get(int(pid))


def lookup_batter(pid: int, season: int) -> dict | None:
    return batter_expected(season).get(int(pid))


def batter_statcast(season: int) -> dict[int, dict]:
    """Quality-of-contact metrics: barrel %, hard-hit %, sweet-spot %, EV."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/statcast"
        f"?type=batter&year={season}&min=q&csv=true"
    )
    return _idx(rows)


def pitcher_statcast(season: int) -> dict[int, dict]:
    """Pitcher quality-of-contact ALLOWED."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/statcast"
        f"?type=pitcher&year={season}&min=q&csv=true"
    )
    return _idx(rows)


def lookup_batter_qoc(pid: int, season: int) -> dict | None:
    return batter_statcast(season).get(int(pid))


def catcher_framing(season: int) -> dict[int, float]:
    """{catcher_player_id: rv_tot (framing run value)}.

    Pulls Savant's catcher-framing leaderboard. rv_tot is the total run value
    that catcher gained/lost via pitch framing over the season — elite framers
    (Realmuto, Kelly, Heim) hit +5 to +10; anti-framers (Salvy) hit -5 to -10.
    Used to give a small K-rate boost to pitchers whose team's starting
    catcher is an elite framer (and a shrink for anti-framers). 24h cached.

    Note the URL omits the 'type' param — including type=Cat returns the
    rows without player IDs (an empty 'id' column for every row). The plain
    leaderboard URL returns id+name.
    """
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/catcher-framing"
        f"?year={season}&min=10&csv=true"
    )
    out: dict[int, float] = {}
    for r in rows:
        try:
            pid = int(r.get("id", "") or 0)
            rv = float(r.get("rv_tot", "") or 0)
        except (TypeError, ValueError):
            continue
        if pid:
            out[pid] = rv
    return out


def lookup_catcher_framing(pid: int, season: int) -> float | None:
    return catcher_framing(season).get(int(pid))


def lookup_pitcher_qoc(pid: int, season: int) -> dict | None:
    return pitcher_statcast(season).get(int(pid))


# ----- dynamic league averages -----
# Cached for 24h on disk so projections always anchor to the CURRENT league
# baselines rather than constants that drift through the season. No cron job
# needed — first request of the day computes; the rest hit cache.

_LG_CACHE: dict[int, tuple[float, dict]] = {}
_LG_TTL = 24 * 3600

# Fallback values if Statcast is unreachable. Match 2026 mid-season actuals so
# a fetch failure doesn't introduce known-bad numbers.
_LG_FALLBACK = {
    "brl_pct_hitter": 8.75,
    "hh_pct_hitter": 40.5,
    "sweetspot_pct": 33.5,
    "xwoba_hitter": 0.310,
    "brl_pct_allowed": 8.0,
    "hh_pct_allowed": 39.0,
    "xera": 4.20,
    "xwoba_against": 0.320,
}


def _mean(rows: list[dict], field: str) -> float | None:
    vals = []
    for r in rows:
        v = r.get(field)
        try:
            v = float(v) if v not in (None, "", "null") else None
        except Exception:
            v = None
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return sum(vals) / len(vals)


def league_averages(season: int) -> dict:
    """Pull current Statcast leaderboards and compute league means for the
    metrics projections.py uses as baselines. 24h cached. Falls back to a
    static mid-season 2026 snapshot if the API is unreachable so projections
    never collapse to wildly-wrong constants.
    """
    import logging
    now = time.time()
    cached = _LG_CACHE.get(season)
    if cached and now - cached[0] < _LG_TTL:
        return cached[1]

    out = dict(_LG_FALLBACK)
    try:
        bs = batter_statcast(season)        # min=q qualified hitters
        ps = pitcher_statcast(season)       # min=q qualified pitchers (allowed contact)
        be = batter_expected(season)        # all batters with PAs (xwOBA)
        pe = pitcher_expected(season)       # all pitchers (xERA, xwOBA-against)
        if bs:
            brl_h = _mean(bs.values(), "brl_percent")
            hh_h  = _mean(bs.values(), "ev95percent")
            ss    = _mean(bs.values(), "anglesweetspotpercent")
            if brl_h: out["brl_pct_hitter"] = round(brl_h, 2)
            if hh_h:  out["hh_pct_hitter"]  = round(hh_h, 2)
            if ss:    out["sweetspot_pct"]  = round(ss, 2)
        if ps:
            brl_a = _mean(ps.values(), "brl_percent")
            hh_a  = _mean(ps.values(), "ev95percent")
            if brl_a: out["brl_pct_allowed"] = round(brl_a, 2)
            if hh_a:  out["hh_pct_allowed"]  = round(hh_a, 2)
        if be:
            xwh = _mean(be.values(), "est_woba")
            if xwh: out["xwoba_hitter"] = round(xwh, 4)
        if pe:
            xe  = _mean(pe.values(), "xera")
            xwa = _mean(pe.values(), "est_woba")
            if xe:  out["xera"] = round(xe, 2)
            if xwa: out["xwoba_against"] = round(xwa, 4)
    except Exception as e:
        logging.warning("league_averages fetch failed for season %s: %s — using fallback", season, e)

    _LG_CACHE[season] = (now, out)
    return out


def batter_expected_range(season: int, start_date: str, end_date: str) -> dict[int, dict]:
    """Rolling expected stats over a date window. Returns {pid: {est_woba, ...}}.
    Cached on disk for 24h via the same _csv pipeline."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter&year={season}&min=5&csv=true"
        f"&start_date={start_date}&end_date={end_date}"
    )
    return _idx(rows)


def pitcher_expected_range(season: int, start_date: str, end_date: str) -> dict[int, dict]:
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=pitcher&year={season}&min=5&csv=true"
        f"&start_date={start_date}&end_date={end_date}"
    )
    return _idx(rows)
