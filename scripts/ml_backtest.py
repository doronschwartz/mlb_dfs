"""POC: does a GBM combine the factor-chain's own features better than the
hand-built multiplicative chain? Fair head-to-head — same features, same
leak-free pipeline (project_slate uses as_of=date), time-split backtest.

Stage 1: build a table of (chain components as features, chain projection,
actual points) for every player-game over a date window — leak-free because
project_slate(d) only sees data through d. Stage 2: time-split (train early,
test late), train XGBoost on features→actual, compare GBM vs chain MAE/bias
on the HELD-OUT test dates, per role.

    python scripts/ml_backtest.py
"""
import sys, os, json, warnings, datetime
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date as D, timedelta
from mlb_dfs import projections as P, live
from mlb_dfs.draft import Pick
import numpy as np

# ---- window ----
END = D(2026, 5, 30)
NDAYS = 15
DATES = [(END - timedelta(days=i)).isoformat() for i in range(NDAYS)][::-1]
TEST_FRAC = 0.30  # last 30% of dates held out

# numeric component features (missing → NaN, GBM handles it)
NUM_FEATS = [
    "base_pg", "pg_l3", "pg_l7", "pg_l14", "games_l3", "games_l7", "games_l14",
    "sample_games_14d", "sp_factor", "sp_factor_raw", "qoc_factor", "park_factor",
    "order_factor", "batting_order", "vegas_factor", "implied_team_total",
    "bullpen_factor", "platoon_factor", "rolling_factor", "iso_factor", "sb_factor",
    "hot_cold_factor", "barrel_pct", "hardhit_pct", "sweet_spot_pct", "chain_product",
    # pitcher-side
    "base_per_start", "k9_season", "ip_per_start", "xera", "xwoba_against",
    "barrel_pct_allowed", "opp_implied_total", "k_prop_adj", "tto_factor",
]
CAT_FEATS = ["form_tag", "qoc_tier"]


def _row(p, actual):
    c = p.components or {}
    r = {f: c.get(f) for f in NUM_FEATS}
    for cf in CAT_FEATS:
        r["cat_" + cf] = c.get(cf) or ""
    r["chain_proj"] = p.projected_points
    r["actual"] = actual
    r["role"] = p.role
    return r


def build_table():
    rows = []
    for d in DATES:
        try:
            projs = P.project_slate(D.fromisoformat(d))
            box = live._index_boxscores(D.fromisoformat(d))
        except Exception as e:
            print("  date %s failed: %s" % (d, str(e)[:50]), flush=True); continue
        n = 0
        for p in projs:
            lines = box.get(p.player_id) or []
            if not lines:
                continue
            fake = Pick(drafter="-", slot=("SP" if p.role == "pitcher" else "UTIL"),
                        player_id=p.player_id, name=p.name, position=p.position or "-",
                        role=p.role, projected_points=p.projected_points, pick_number=0, game_pk=None)
            ps = live._score_player(fake, lines)
            if ps.game_state in ("Pre-Game", "Warmup", "Scheduled", ""):
                continue
            rows.append(_row(p, ps.points)); n += 1
        print("  %s: %d player-games" % (d, n), flush=True)
    return rows


def evaluate(rows, role):
    import pandas as pd, xgboost as xgb
    sub = [r for r in rows if r["role"] == role]
    if len(sub) < 200:
        print("  %s: too few rows (%d)" % (role, len(sub))); return
    df = pd.DataFrame(sub)
    # date-based split: assign each row its date index via order isn't kept, so
    # we split on a held-out set of player-games chronologically by re-deriving.
    # Simpler: we appended in date order, so a tail split ≈ time split.
    cut = int(len(df) * (1 - TEST_FRAC))
    train, test = df.iloc[:cut], df.iloc[cut:]
    feat_cols = NUM_FEATS + ["cat_" + c for c in CAT_FEATS]
    X = pd.get_dummies(df[feat_cols], columns=["cat_" + c for c in CAT_FEATS], dummy_na=False)
    Xtr, Xte = X.iloc[:cut], X.iloc[cut:]
    ytr, yte = train["actual"].values, test["actual"].values
    m = xgb.XGBRegressor(n_estimators=400, max_depth=4, learning_rate=0.03,
                         subsample=0.8, colsample_bytree=0.8, reg_alpha=1.0,
                         reg_lambda=2.0, min_child_weight=5, random_state=42, n_jobs=-1)
    m.fit(Xtr, ytr)
    gbm = m.predict(Xte)
    chain = test["chain_proj"].values
    def stat(pred):
        diff = yte - pred
        return float(np.mean(diff)), float(np.mean(np.abs(diff)))
    cb, cm = stat(chain); gb, gm = stat(gbm)
    print("\n=== %s (train %d / test %d) ===" % (role.upper(), cut, len(test)))
    print("  CHAIN  bias=%+.3f mae=%.3f" % (cb, cm))
    print("  GBM    bias=%+.3f mae=%.3f" % (gb, gm))
    print("  MAE improvement: %+.1f%%" % (100 * (cm - gm) / cm))
    # top features
    imp = sorted(zip(X.columns, m.feature_importances_), key=lambda x: -x[1])[:8]
    print("  top GBM features:", ", ".join("%s=%.2f" % (a, b) for a, b in imp))


if __name__ == "__main__":
    print("Building leak-free table over %d dates (%s..%s)..." % (NDAYS, DATES[0], DATES[-1]), flush=True)
    rows = build_table()
    print("\nTotal player-games: %d" % len(rows))
    # persist so we can re-train without re-pulling
    with open("/tmp/ml_rows.json", "w") as f:
        json.dump(rows, f)
    for role in ("hitter", "pitcher"):
        evaluate(rows, role)
    print("\nDONE")
