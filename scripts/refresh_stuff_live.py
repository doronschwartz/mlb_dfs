"""Regenerate the precomputed live Stuff+ window leaderboards.

Run OFFLINE (a real multi-core machine), NOT on the serving box — XGBoost
training is CPU-bound and would starve the single-vCPU web server. Writes the
committed JSON the /api/stuff/live endpoint serves. Re-run + redeploy to refresh.

    python scripts/refresh_stuff_live.py
"""
import datetime
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlb_dfs import stuff_live as sl
from mlb_dfs.stufflib import model as M

M.FAST = True  # skip CV, fewer trees

OUT = os.path.join(os.path.dirname(sl.__file__), "data", "stuff_live")
os.makedirs(OUT, exist_ok=True)

today = datetime.date.today()
TD = today.isoformat()
WINDOWS = {
    "season": (sl.SEASON_START, TD),
    "30d": ((today - datetime.timedelta(days=30)).isoformat(), TD),
    "14d": ((today - datetime.timedelta(days=14)).isoformat(), TD),
}

for name, (start, end) in WINDOWS.items():
    try:
        res = sl.compute(start, end)
        res["window_name"] = name
        with open(os.path.join(OUT, name + ".json"), "w") as f:
            json.dump(res, f)
        print(f"{name}: {res['n_pitchers']} pitchers in {res['compute_secs']:.0f}s ({start}..{end})")
    except Exception as e:
        print(f"{name} FAILED: {e}")
print("done — commit mlb_dfs/data/stuff_live/*.json and redeploy")
