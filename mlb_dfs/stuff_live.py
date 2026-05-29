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
# Precomputed window leaderboards (season / 30d / 14d), generated OFFLINE and
# committed. The serving box must NOT train — XGBoost is CPU-bound and pegs the
# single shared vCPU, starving the web server. So we precompute on a real
# machine + ship the JSON; the endpoint only ever reads these.
_PRECOMPUTED_DIR = os.path.join(os.path.dirname(__file__), "data", "stuff_live")


# League-average whiff% by pitch type (whiffs / competitive pitches). Used by
# the artifact guard below.
_WHIFF_BASE = {"FF": 16.1, "SI": 8.9, "FC": 16.6, "SL": 25.6, "ST": 23.4,
               "CU": 21.0, "KC": 26.2, "CH": 26.5, "FS": 30.1}


def _apply_whiff_guard(board: list[dict]) -> list[dict]:
    """Fix the extreme-arm-slot artifact: Stuff+ models extrapolate "elite" for
    submariners / low-slot arms (Tyler Rogers' 83mph sinker rated 116 on a 7.3%
    whiff) because their release geometry is out-of-distribution. A pitch can't
    be ELITE stuff while missing few bats — so cap any pitch's Stuff+ at 103
    when its whiff% is below league average for that type. High-whiff power
    pitches (Misiorowski, Skenes) are untouched. Recomputes the usage-weighted
    overall. Applied at serve time so it works on existing cached boards."""
    ELITE_CAP = 103.0
    out = []
    for p in board:
        num = den = 0.0
        ars = []
        for a in p.get("arsenal", []):
            s = a["stuff"]; w = a.get("whiff_pct")
            base = _WHIFF_BASE.get(a["pitch_type"], 14.0)
            if s > ELITE_CAP and w is not None and w < base:
                s = ELITE_CAP  # elite claim not backed by whiffs → cap
            ars.append({**a, "stuff": round(s, 1)})
            num += s * a["pitches"]; den += a["pitches"]
        q = dict(p)
        q["arsenal"] = ars
        q["stuff"] = round(num / den, 1) if den else p["stuff"]
        out.append(q)
    out.sort(key=lambda x: -x["stuff"])
    return out


def load_window(name: str) -> dict | None:
    """Serve a precomputed window leaderboard by name (season / 30d / 14d),
    with the extreme-arm-slot artifact guard applied."""
    if name not in ("season", "30d", "14d"):
        return None
    try:
        with open(os.path.join(_PRECOMPUTED_DIR, name + ".json")) as f:
            res = json.load(f)
    except (OSError, ValueError):
        return None
    res["pitchers"] = _apply_whiff_guard(res["pitchers"])
    return res
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
    # TOTAL pitches thrown per (pitcher, pitch type) — for the displayed count.
    # engineer_features keeps only competitive outcomes (swings/called strikes/
    # balls in play), which is the SCORED sample but ~60% of total, so reporting
    # that as "pitches" undercounts. We display total, shrink on scored.
    tot = (df[df["pitch_type"].isin(F.PITCH_TYPES_TO_MODEL)]
           .groupby(["pitcher", "pitch_type"]).size().to_dict())
    eng = F.engineer_features(df)
    scored = M.train_models(eng)          # all pitch types
    out = M.score_pitches(scored)         # adds stuff_plus per pitch, 100±10

    # Aggregate to pitcher × pitch type, then usage-weight + shrink to a
    # pitcher-level number (mirrors stuff.pitcher_leaderboard).
    by_pitcher: dict[int, dict] = {}
    grp = out.groupby(["pitcher", "player_name", "pitch_type"])
    for (pid, name, pt), g in grp:
        scored_n = len(g)                       # modeled pitches (for shrink)
        pid = int(pid)
        total_n = int(tot.get((pid, pt), scored_n))  # all pitches of this type (display)
        raw = float(g["stuff_plus"].mean())
        whiff = float(g["whiff"].mean() * 100) if "whiff" in g else None
        agg = by_pitcher.setdefault(pid, {
            "pitcher_id": pid, "name": _flip(str(name)),
            "_num": 0.0, "_den": 0.0, "total_pitches": 0, "arsenal": [],
        })
        shrunk = _shrink(raw, scored_n)
        agg["_num"] += shrunk * total_n         # usage-weight by total thrown
        agg["_den"] += total_n
        agg["total_pitches"] += total_n
        agg["arsenal"].append({
            "pitch_type": pt, "pitches": total_n, "scored": scored_n,
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
