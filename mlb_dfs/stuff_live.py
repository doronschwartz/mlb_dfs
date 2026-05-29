"""Live, date-adjustable Stuff+ — runs JL's pipeline (features + model) on
freshly-pulled Statcast for an arbitrary date window, instead of a static CSV.

Why train-and-score live (not load JL's saved pickles): the saved models were
trained on an older 10-feature set that no longer matches features.py (21
cols), so loading them gives wrong predictions. Re-running the SAME feature +
model code on the pulled data is self-consistent and genuinely live.

This is HEAVY (a Statcast pull is ~2.5 min / 2 weeks + per-pitch-type training),
so every window is disk-cached and computed in the background — never in a
request path. Shares the n/(n+k) shrinkage + usage-weighting with stuff.py.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import disk_cache

_CACHE_DIR = os.environ.get("MLB_DFS_CACHE_DIR",
                            os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache"))
_K_STUFF = 80.0            # shrink prior (pitches), same as stuff.py
_LEAGUE_MEAN = 100.0
_MIN_PITCHES = 150         # pitcher-level qualifier for the board
SEASON_START = "2026-03-18"  # 2026 opening week — "from first game" season view

# In-flight guard so two requests don't both compute the same window.
_computing: set[str] = set()
_lock = threading.Lock()


def _key(start: str, end: str) -> str:
    return "stufflive_%s_%s" % (start, end)


def _path(start: str, end: str) -> str:
    return os.path.join(_CACHE_DIR, _key(start, end) + ".json")


def cached(start: str, end: str) -> dict | None:
    """Return the cached leaderboard for a window, or None if not computed yet."""
    try:
        with open(_path(start, end)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_computing(start: str, end: str) -> bool:
    return _key(start, end) in _computing


def _shrink(value: float, pitches: float) -> float:
    w = pitches / (pitches + _K_STUFF) if pitches > 0 else 0.0
    return _LEAGUE_MEAN + (value - _LEAGUE_MEAN) * w


def _flip(name: str) -> str:
    if "," in name:
        last, first = [s.strip() for s in name.split(",", 1)]
        return f"{first} {last}"
    return name


def compute(start: str, end: str) -> dict:
    """Pull Statcast for [start, end], engineer features, train per-pitch-type
    models, score, aggregate to a shrunk pitcher-level leaderboard. Writes the
    result to disk cache. Returns the leaderboard dict."""
    import warnings
    warnings.filterwarnings("ignore")
    from pybaseball import statcast
    from .stufflib import features as F, model as M
    M.FAST = True   # skip CV + fewer trees — ~6× faster on the shared core

    t0 = time.time()
    df = statcast(start_dt=start, end_dt=end)
    eng = F.engineer_features(df)
    scored = M.train_models(eng)          # all pitch types
    out = M.score_pitches(scored)         # adds stuff_plus per pitch, 100±10

    # Aggregate to pitcher × pitch type, then usage-weight + shrink to a
    # pitcher-level number (mirrors stuff.pitcher_leaderboard).
    by_pitcher: dict[int, dict] = {}
    grp = out.groupby(["pitcher", "player_name", "pitch_type"])
    for (pid, name, pt), g in grp:
        n = len(g)
        raw = float(g["stuff_plus"].mean())
        whiff = float(g["whiff"].mean() * 100) if "whiff" in g else None
        pid = int(pid)
        agg = by_pitcher.setdefault(pid, {
            "pitcher_id": pid, "name": _flip(str(name)),
            "_num": 0.0, "_den": 0.0, "total_pitches": 0, "arsenal": [],
        })
        shrunk = _shrink(raw, n)
        agg["_num"] += shrunk * n
        agg["_den"] += n
        agg["total_pitches"] += n
        agg["arsenal"].append({
            "pitch_type": pt, "pitches": n,
            "stuff": round(shrunk, 1), "stuff_raw": round(raw, 1),
            "whiff_pct": round(whiff, 1) if whiff is not None else None,
        })
    board = []
    for a in by_pitcher.values():
        if a["_den"] <= 0:
            continue
        a["stuff"] = round(a["_num"] / a["_den"], 1)
        a["arsenal"].sort(key=lambda x: -x["pitches"])
        del a["_num"], a["_den"]
        board.append(a)
    board.sort(key=lambda x: -x["stuff"])

    res = {
        "start": start, "end": end, "metric": "Stuff+", "league_mean": 100,
        "shrink_k": _K_STUFF, "pitchers": board,
        "n_pitchers": len(board), "computed_at": int(time.time()),
        "compute_secs": round(time.time() - t0, 1),
        "live": True,
    }
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_path(start, end), "w") as f:
            json.dump(res, f)
    except OSError as e:
        logging.warning("stuff_live cache write failed: %s", e)
    return res


def compute_bg(start: str, end: str):
    """Kick a background compute for a window if not already cached/running."""
    k = _key(start, end)
    with _lock:
        if k in _computing or cached(start, end):
            return
        _computing.add(k)

    def _work():
        try:
            compute(start, end)
        except Exception as e:
            logging.warning("stuff_live compute failed %s..%s: %s", start, end, e)
        finally:
            _computing.discard(k)
    threading.Thread(target=_work, daemon=True).start()
