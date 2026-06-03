"""Fast head-to-head: pull leak-free components from /api/projections (computed
as_of each date) + actuals from /api/calibration, join on player_id, time-split,
train a GBM on (features -> actual), compare GBM vs the factor-chain on held-out
dates. Server-cached endpoints => minutes, not an hour of local re-compute."""
import json, urllib.request, time, datetime, warnings
warnings.filterwarnings("ignore")
import numpy as np

BASE = "https://mlb-dfs-doron.fly.dev"
END = datetime.date(2026, 5, 30)
NDAYS = 14
DATES = [(END - datetime.timedelta(days=i)).isoformat() for i in range(NDAYS)][::-1]
TEST_FRAC = 0.30

NUM_FEATS = ["base_pg","pg_l3","pg_l7","pg_l14","games_l3","games_l7","games_l14",
    "sample_games_14d","sp_factor","sp_factor_raw","qoc_factor","park_factor",
    "order_factor","batting_order","vegas_factor","implied_team_total","bullpen_factor",
    "platoon_factor","rolling_factor","iso_factor","sb_factor","hot_cold_factor",
    "barrel_pct","hardhit_pct","sweet_spot_pct","chain_product","base_per_start",
    "k9_season","ip_per_start","xera","xwoba_against","barrel_pct_allowed",
    "opp_implied_total","k_prop_adj","tto_factor"]
CAT_FEATS = ["form_tag","qoc_tier"]


def _get(path):
    for _ in range(4):
        try:
            return json.load(urllib.request.urlopen(BASE + path, timeout=240))
        except Exception:
            time.sleep(6)
    return None


def build():
    rows = []
    for d in DATES:
        proj = _get("/api/projections?date=%s" % d)
        cal = _get("/api/calibration?date=%s" % d)
        if not proj or not cal:
            print("  %s: skip (fetch failed)" % d, flush=True); continue
        actual = {r["player_id"]: r["actual"] for r in cal.get("rows", [])}
        n = 0
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
            rows.append(r); n += 1
        print("  %s: %d player-games" % (d, n), flush=True)
    return rows


def evaluate(rows, role):
    import pandas as pd, xgboost as xgb
    sub = [r for r in rows if r["role"] == role]
    if len(sub) < 200:
        print("  %s: too few rows (%d)" % (role, len(sub))); return
    df = pd.DataFrame(sub)
    # JSON Nones make some columns object dtype; coerce to numeric for XGBoost.
    df[NUM_FEATS] = df[NUM_FEATS].apply(pd.to_numeric, errors="coerce")
    df["chain_proj"] = pd.to_numeric(df["chain_proj"], errors="coerce")
    df["actual"] = pd.to_numeric(df["actual"], errors="coerce")
    df = df.dropna(subset=["chain_proj", "actual"]).reset_index(drop=True)
    cut = int(len(df) * (1 - TEST_FRAC))
    feat_cols = NUM_FEATS + ["cat_" + c for c in CAT_FEATS]
    X = pd.get_dummies(df[feat_cols], columns=["cat_" + c for c in CAT_FEATS], dummy_na=False)
    Xtr, Xte = X.iloc[:cut], X.iloc[cut:]
    ytr, yte = df["actual"].values[:cut], df["actual"].values[cut:]
    m = xgb.XGBRegressor(n_estimators=400, max_depth=4, learning_rate=0.03,
                         subsample=0.8, colsample_bytree=0.8, reg_alpha=1.0,
                         reg_lambda=2.0, min_child_weight=5, random_state=42, n_jobs=-1)
    m.fit(Xtr, ytr)
    gbm = m.predict(Xte)
    chain = df["chain_proj"].values[cut:].astype(float)
    def stat(pred):
        diff = yte - pred
        return float(np.mean(diff)), float(np.mean(np.abs(diff)))
    cb, cm = stat(chain); gb, gm = stat(gbm)
    # residual GBM: predict on top of the chain (learn a correction)
    res_m = xgb.XGBRegressor(n_estimators=300, max_depth=3, learning_rate=0.03,
                             subsample=0.8, reg_lambda=2.0, min_child_weight=5,
                             random_state=42, n_jobs=-1)
    res_m.fit(Xtr, ytr - df["chain_proj"].values[:cut].astype(float))
    hybrid = chain + res_m.predict(Xte)
    hb, hm = stat(hybrid)
    print("\n=== %s (train %d / test %d) ===" % (role.upper(), cut, len(df) - cut))
    print("  CHAIN        bias=%+.3f mae=%.3f" % (cb, cm))
    print("  GBM (raw)    bias=%+.3f mae=%.3f  (%+.1f%% MAE)" % (gb, gm, 100*(cm-gm)/cm))
    print("  HYBRID chain+GBM resid  bias=%+.3f mae=%.3f  (%+.1f%% MAE)" % (hb, hm, 100*(cm-hm)/cm))
    imp = sorted(zip(X.columns, m.feature_importances_), key=lambda x: -x[1])[:8]
    print("  top GBM features:", ", ".join("%s=%.2f" % (a, b) for a, b in imp))


if __name__ == "__main__":
    print("Pulling %d dates from server endpoints..." % NDAYS, flush=True)
    rows = build()
    print("\nTotal player-games: %d" % len(rows))
    json.dump(rows, open("/tmp/ml_rows.json", "w"))  # save so re-train skips re-fetch
    for role in ("hitter", "pitcher"):
        evaluate(rows, role)
    print("\nDONE")
