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


def batter_percentiles(season: int) -> dict[int, dict]:
    """{batter_id: {sprint_speed, baserunning}} from the batter percentile
    leaderboard. Captures the 'run' tool our contact-quality metrics miss —
    speed/baserunning is real dynasty value (SB + extra bases) that xwOBA
    ignores. Values are 0-100 percentiles (higher = faster/better)."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/percentile-rankings"
        f"?year={season}&csv=true&min=q&type=batter"
    )
    out: dict[int, dict] = {}
    for r in rows:
        try:
            pid = int(r.get("player_id", "") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        def _pct(field: str) -> float | None:
            try:
                v = r.get(field, "")
                return float(v) if v not in (None, "", "null") else None
            except (TypeError, ValueError):
                return None
        out[pid] = {
            "sprint_speed": _pct("sprint_speed"),
            "baserunning": _pct("r_run_value") if _pct("r_run_value") is not None else _pct("baserunning_run_value"),
        }
    return out


def pitcher_percentiles(season: int) -> dict[int, dict]:
    """{pitcher_id: {whiff, chase, k, bb, ...}} — Savant percentile rankings.

    Values are 0-100 percentiles (higher = better for the pitcher). The
    whiff_percent column is whiff-rate-on-swings percentile — a true skill
    signal that's more forward-looking than rolling K/9 (which is event-
    counted and polluted by lineup/park/luck). Used as a K-rate skill
    component in sp_factor (vs hitter) and as a short-sample skill anchor
    for the pitcher's own projection.
    """
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/percentile-rankings"
        f"?year={season}&csv=true&min=q&type=pitcher"
    )
    out: dict[int, dict] = {}
    for r in rows:
        try:
            pid = int(r.get("player_id", "") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        def _pct(field: str) -> float | None:
            try:
                v = r.get(field, "")
                return float(v) if v not in (None, "", "null") else None
            except (TypeError, ValueError):
                return None
        out[pid] = {
            "whiff": _pct("whiff_percent"),
            "chase": _pct("chase_percent"),
            "k": _pct("k_percent"),
            "bb": _pct("bb_percent"),
            "xera_pct": _pct("xera"),
        }
    return out


def lookup_pitcher_percentiles(pid: int, season: int) -> dict | None:
    return pitcher_percentiles(season).get(int(pid))


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


# NB: batter_expected_range / pitcher_expected_range used to live here but
# were RETIRED 2026-05-18 after we discovered Savant's /expected_statistics
# endpoint silently ignores start_date/end_date params — every window returned
# season-wide data, making the downstream rolling_factor a no-op for every
# player. Probed 11 alternate param names (game_date_gt, since, month, splits,
# etc.) — none filter. The replacement uses MLB Stats API K%-rate shift from
# byDateRange (which DOES honor the dates) and lives in projections.py.


# -------- pitch-arsenal matchup (v9.40) --------
# Two leaderboards power the arsenal-vs-hitter matchup factor:
#   type=pitcher → each pitcher's pitch MIX (usage% per pitch type)
#   type=batter  → each hitter's run value per 100 pitches BY pitch type
# Both are season-cumulative (same point-in-time caveat as every other
# Savant leaderboard we use — fine for live projections, leaks for backtests).

def pitcher_arsenal(season: int) -> dict[int, list[dict]]:
    """{pitcher_id: [{pitch_type, usage, rv100, pitches}, ...]} sorted by usage."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
        f"?type=pitcher&year={season}&min=10&csv=true"
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        try:
            pid = int(r.get("player_id", ""))
            usage = float(r.get("pitch_usage", ""))
        except (TypeError, ValueError):
            continue
        try:
            rv100 = float(r.get("run_value_per_100", ""))
        except (TypeError, ValueError):
            rv100 = None
        try:
            n = int(float(r.get("pitches", "0") or 0))
        except (TypeError, ValueError):
            n = 0
        out.setdefault(pid, []).append({
            "pitch_type": r.get("pitch_type", ""),
            "usage": usage, "rv100": rv100, "pitches": n,
        })
    for pid in out:
        out[pid].sort(key=lambda x: -x["usage"])
    return out


def batter_pitch_rv(season: int) -> dict[int, dict[str, dict]]:
    """{batter_id: {pitch_type: {rv100, pitches}}} — how the hitter performs
    against each pitch type, in run value per 100 pitches seen."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
        f"?type=batter&year={season}&min=10&csv=true"
    )
    out: dict[int, dict[str, dict]] = {}
    for r in rows:
        try:
            pid = int(r.get("player_id", ""))
            rv100 = float(r.get("run_value_per_100", ""))
        except (TypeError, ValueError):
            continue
        try:
            n = int(float(r.get("pitches", "0") or 0))
        except (TypeError, ValueError):
            n = 0
        pt = r.get("pitch_type", "")
        if pt:
            out.setdefault(pid, {})[pt] = {"rv100": rv100, "pitches": n}
    return out
