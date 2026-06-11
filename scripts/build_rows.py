"""Build the calibration/backtest row table from the live server endpoints
(leak-free: /api/projections?date=X computes as_of=X) and persist it to
data/backtest_rows.json. Re-runnable; warm server cache makes it fast."""
import json, os, time, urllib.request, datetime, sys
from concurrent.futures import ThreadPoolExecutor

BASE = "https://mlb-dfs-doron.fly.dev"
START = datetime.date(2026, 5, 17)
END = datetime.date(2026, 6, 10)  # through yesterday — 6/11 is still in progress
DATES = []
d = START
while d <= END:
    DATES.append(d.isoformat())
    d += datetime.timedelta(days=1)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "backtest_rows.json")

NUM_FEATS = ["base_pg","pg_l3","pg_l7","pg_l14","games_l3","games_l7","games_l14",
    "sample_games_14d","sp_factor","sp_factor_raw","qoc_factor","park_factor",
    "order_factor","batting_order","vegas_factor","implied_team_total","bullpen_factor",
    "platoon_factor","rolling_factor","iso_factor","sb_factor","hot_cold_factor",
    "barrel_pct","hardhit_pct","sweet_spot_pct","chain_product","base_per_start",
    "k9_season","ip_per_start","xera","xwoba_against","barrel_pct_allowed",
    "opp_implied_total","k_prop_adj","tto_factor",
    "tb_prop_z","tb_prop_factor","arsenal_factor"]
CAT_FEATS = ["form_tag", "qoc_tier"]


def _get(path):
    for _ in range(5):
        try:
            return json.load(urllib.request.urlopen(BASE + path, timeout=300))
        except Exception:
            time.sleep(8)
    return None


def build_date(d):
    proj = _get("/api/projections?date=%s" % d)
    cal = _get("/api/calibration?date=%s" % d)
    if not proj or not cal:
        print("  %s: skip (fetch failed)" % d, flush=True)
        return []
    actual = {r["player_id"]: r["actual"] for r in cal.get("rows", [])}
    out = []
    for p in proj.get("projections", []):
        pid = p.get("player_id")
        if pid not in actual:
            continue
        c = p.get("components") or {}
        r = {f: c.get(f) for f in NUM_FEATS}
        for cf in CAT_FEATS:
            r["cat_" + cf] = c.get(cf) or ""
        r["chain_proj"] = p.get("projected_points")
        r["actual"] = actual[pid]
        r["role"] = p.get("role")
        r["date"] = d
        # identity + counterfactual-inversion fields (v9.40 factor A/B)
        r["player_id"] = pid
        r["name"] = p.get("name")
        r["bats"] = c.get("bats")
        r["vs_throws"] = c.get("vs_throws")
        out.append(r)
    print("  %s: %d player-games" % (d, len(out)), flush=True)
    return out


if __name__ == "__main__":
    print("Building %d dates (%s..%s) from %s" % (len(DATES), DATES[0], DATES[-1], BASE), flush=True)
    rows = []
    # Sequential on purpose: 2 concurrent cold project_slate computes OOM'd
    # the 2GB prod box (machine restart observed 2026-06-11 19:43Z).
    for d in DATES:
        rows.extend(build_date(d))
    rows.sort(key=lambda r: r["date"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(rows, open(OUT, "w"))
    print("\nTotal: %d player-games -> %s" % (len(rows), OUT), flush=True)
