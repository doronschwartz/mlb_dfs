"""True rolling xwOBA from event-level Statcast — the honest recent-form
signal (critique #1, 2026-06-30).

Why: the chain's 'rolling form' proxy has been K%-shift, adopted only because
Savant's aggregate leaderboards silently ignore date filters. Event-level
statcast_search DOES honor dates, so we can build real rolling xwOBA — and
because each pull is a single historical date, the signal is LEAK-FREE for
backtests (unlike every season-cumulative leaderboard input).

Design: one CSV pull per completed date (league-wide, PA-result rows kept),
reduced to tiny per-batter daily aggregates cached on disk forever
(~25KB/date). Rolling windows sum the dailies. First touch of a cold date
costs one ~10-15MB download; every later use is instant.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import date as Date, timedelta

import requests

from .disk_cache import CACHE_DIR

XWOBA_DIR = os.path.join(
    os.path.dirname(CACHE_DIR), "xwoba_daily"
)  # sits next to the disk cache (on /data in prod via MLB_DFS_ODDS_DIR's parent? no —
# CACHE_DIR already lives under data/, which is the volume in prod)

_UA = {"User-Agent": "Mozilla/5.0 (mlb-dfs rolling-xwoba)"}
_SEARCH = (
    "https://baseballsavant.mlb.com/statcast_search/csv?all=true&hfGT=R%7C"
    "&player_type=batter&game_date_gt={d}&game_date_lt={d}"
    "&min_pitches=0&min_results=0&min_pas=0&type=details"
)

_MEM: dict[str, dict] = {}


def _path(d: str) -> str:
    return os.path.join(XWOBA_DIR, f"{d}.json")


def daily_batter_xwoba(d: str) -> dict[int, dict]:
    """{batter_id: {"xw": Σ per-PA xwOBA value, "pa": wOBA-denominator PAs}}
    for one completed date. Cached to disk permanently (historical Statcast
    data never changes). Empty dict when the pull fails — callers fall back."""
    if d in _MEM:
        return _MEM[d]
    p = _path(d)
    if os.path.exists(p):
        try:
            raw = json.load(open(p))
            out = {int(k): v for k, v in raw.items()}
            _MEM[d] = out
            return out
        except Exception:
            pass
    try:
        r = requests.get(_SEARCH.format(d=d), headers=_UA, timeout=120)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        logging.warning("statcast pull failed for %s: %s", d, e)
        return {}
    agg: dict[int, dict] = {}
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if not row.get("events"):
                continue  # mid-PA pitch rows
            try:
                denom = float(row.get("woba_denom") or 0)
            except ValueError:
                denom = 0
            if denom < 1:
                continue  # non-wOBA events (sac bunts, catcher interference…)
            try:
                bid = int(row["batter"])
            except (KeyError, ValueError):
                continue
            est = row.get("estimated_woba_using_speedangle")
            try:
                val = float(est) if est not in (None, "", "null") else float(row.get("woba_value") or 0)
            except ValueError:
                val = 0.0
            a = agg.setdefault(bid, {"xw": 0.0, "pa": 0})
            a["xw"] += val
            a["pa"] += 1
    except Exception as e:
        logging.warning("statcast parse failed for %s: %s", d, e)
        return {}
    if agg:
        try:
            os.makedirs(XWOBA_DIR, exist_ok=True)
            tmp = p + ".tmp"
            json.dump({str(k): v for k, v in agg.items()}, open(tmp, "w"))
            os.replace(tmp, p)
        except Exception:
            pass
    _MEM[d] = agg
    return agg


def window_xwoba(bid: int, as_of: Date, days: int) -> tuple[float | None, int]:
    """(xwOBA, PA) over the `days` COMPLETED days before as_of (exclusive —
    same convention as the L14 stat windows). None when no cached data."""
    xw = 0.0
    pa = 0
    for i in range(1, days + 1):
        d = (as_of - timedelta(days=i)).isoformat()
        day = daily_batter_xwoba(d)
        rec = day.get(bid)
        if rec:
            xw += rec["xw"]
            pa += rec["pa"]
    if pa == 0:
        return None, 0
    return xw / pa, pa


def form_ratio(bid: int, as_of: Date, *, short: int = 14, long: int = 60,
               min_short_pa: int = 30, min_long_pa: int = 120) -> tuple[float | None, dict]:
    """Rolling-form signal: 14-day xwOBA vs 60-day baseline xwOBA ratio.
    Both windows from the same event-level source, so park/opponent noise
    largely cancels. Returns (ratio, detail) or (None, {}) when samples are
    too thin — caller falls back to the K%-shift proxy."""
    s_xw, s_pa = window_xwoba(bid, as_of, short)
    l_xw, l_pa = window_xwoba(bid, as_of, long)
    if s_xw is None or l_xw is None or s_pa < min_short_pa or l_pa < min_long_pa or l_xw <= 0.150:
        return None, {}
    return s_xw / l_xw, {"xw14": round(s_xw, 3), "pa14": s_pa,
                         "xw60": round(l_xw, 3), "pa60": l_pa}


def warm(as_of: Date, days: int = 61) -> int:
    """Ensure the trailing `days` daily files exist (one slow pull each on
    first touch). Returns how many dates have data. Used by the nightly
    warmer and backfills."""
    ok = 0
    for i in range(1, days + 1):
        d = (as_of - timedelta(days=i)).isoformat()
        if daily_batter_xwoba(d):
            ok += 1
        time.sleep(0)  # yield
    return ok
