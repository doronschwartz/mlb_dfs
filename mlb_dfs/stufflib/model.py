"""
model.py — Pro-grade Stuff+ model
================================

Per-pitch-type modeling pipeline.

Models trained per pitch type:
1. Whiff probability
2. Called strike probability
3. Weak contact probability (BIP only)
4. Groundball probability (BIP only)
5. Hard-hit probability (BIP only)

Final Stuff+ raw score:
    +1.40 * whiff_prob
    +0.60 * called_strike_prob
    +0.50 * weak_contact_prob
    +0.40 * gb_prob
    -0.80 * hard_hit_prob

Then normalized separately by pitch type:
    mean = 100
    std  = 10
"""

import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.model_selection import GroupKFold, cross_val_score
from sklearn.ensemble import GradientBoostingClassifier

warnings.filterwarnings("ignore")

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False

from .features import get_feature_cols, PITCH_TYPES_TO_MODEL


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

MIN_TRAIN_PITCHES = 500
MIN_BIP_PITCHES = 150

# FAST mode (live serving): skip the 5-fold cross-validation (which trains each
# model 6× just to report a diagnostic AUC we don't use for the leaderboard)
# and trim the tree count. ~6× faster on a shared single core. Set by stuff_live.
FAST = False
FAST_ESTIMATORS = 250

WHIFF_W = 1.40
CSW_W = 0.60
WEAK_W = 0.50
GB_W = 0.40
HH_W = 0.80


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def train_models(
    df: pd.DataFrame,
    pitch_type_filter: str | None = None,
    model_dir: Path = Path("results/models"),
) -> pd.DataFrame:

    model_dir.mkdir(parents=True, exist_ok=True)

    df = df.copy()
    feature_cols = get_feature_cols(df)

    pitch_types = (
        [pitch_type_filter]
        if pitch_type_filter
        else list(PITCH_TYPES_TO_MODEL.keys())
    )

    all_frames = []

    for pt in pitch_types:

        pt_df = df[df["pitch_type"] == pt].copy()

        if len(pt_df) < MIN_TRAIN_PITCHES:
            print(f"Skipping {pt}: only {len(pt_df)} pitches")
            continue

        pt_name = PITCH_TYPES_TO_MODEL.get(pt, pt)
        print(f"\nTraining {pt} ({pt_name}) — {len(pt_df):,} pitches")

        pt_df = pt_df.dropna(subset=feature_cols)

        if len(pt_df) < MIN_TRAIN_PITCHES:
            continue

        X = pt_df[feature_cols].values
        groups = _get_groups(pt_df)

        # ---------------------------------------------------------
        # 1. Whiff model
        # ---------------------------------------------------------
        y = pt_df["whiff"].fillna(0).astype(int).values

        whiff_model, auc = _train_classifier(X, y, groups)
        whiff_prob = whiff_model.predict_proba(X)[:, 1]

        print(f"  Whiff model AUC: {auc:.3f}")

        # ---------------------------------------------------------
        # 2. Called strike model
        # ---------------------------------------------------------
        if "called_strike" in pt_df.columns:
            y = pt_df["called_strike"].fillna(0).astype(int).values
        else:
            y = np.zeros(len(pt_df))

        cs_model, auc = _train_classifier(X, y, groups)
        cs_prob = cs_model.predict_proba(X)[:, 1]

        print(f"  Called Strike AUC: {auc:.3f}")

        # ---------------------------------------------------------
        # BIP subset
        # ---------------------------------------------------------
        bip_df = pt_df[pt_df["in_play"] == 1].copy()

        if len(bip_df) >= MIN_BIP_PITCHES:

            X_bip = bip_df[feature_cols].values
            groups_bip = _get_groups(bip_df)

            # weak contact
            y = bip_df["weak_contact"].fillna(0).astype(int).values
            weak_model, auc = _train_classifier(X_bip, y, groups_bip)
            weak_prob_full = weak_model.predict_proba(X)[:, 1]

            # GB
            if "ground_ball" in bip_df.columns:
                y = bip_df["ground_ball"].fillna(0).astype(int).values
            else:
                y = np.zeros(len(bip_df))

            gb_model, auc2 = _train_classifier(X_bip, y, groups_bip)
            gb_prob_full = gb_model.predict_proba(X)[:, 1]

            # hard hit
            if "hard_hit" in bip_df.columns:
                y = bip_df["hard_hit"].fillna(0).astype(int).values
            else:
                y = np.zeros(len(bip_df))

            hh_model, auc3 = _train_classifier(X_bip, y, groups_bip)
            hh_prob_full = hh_model.predict_proba(X)[:, 1]

            print(f"  Weak Contact AUC: {auc:.3f}")
            print(f"  Groundball   AUC: {auc2:.3f}")
            print(f"  Hard Hit     AUC: {auc3:.3f}")

        else:
            weak_prob_full = np.zeros(len(pt_df))
            gb_prob_full = np.zeros(len(pt_df))
            hh_prob_full = np.zeros(len(pt_df))

        # ---------------------------------------------------------
        # Final Stuff+ raw score
        # ---------------------------------------------------------
        raw_score = (
            WHIFF_W * whiff_prob
            + CSW_W * cs_prob
            + WEAK_W * weak_prob_full
            + GB_W * gb_prob_full
            - HH_W * hh_prob_full
        )

        pt_df["raw_score"] = raw_score
        pt_df["whiff_prob"] = whiff_prob
        pt_df["called_strike_prob"] = cs_prob
        pt_df["weak_contact_prob"] = weak_prob_full
        pt_df["gb_prob"] = gb_prob_full
        pt_df["hard_hit_prob"] = hh_prob_full

        # Save core models
        _save_model(whiff_model, model_dir / f"{pt}_whiff.pkl")
        _save_model(cs_model, model_dir / f"{pt}_called_strike.pkl")

        _print_feature_importance(whiff_model, feature_cols, f"{pt} whiff")

        all_frames.append(pt_df)

    if not all_frames:
        raise ValueError("No pitch types trained.")

    return pd.concat(all_frames, ignore_index=True)


def score_pitches(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize raw_score separately by pitch type.
    """
    df = df.copy()
    df["stuff_plus"] = np.nan

    for pt in df["pitch_type"].unique():

        mask = df["pitch_type"] == pt
        vals = df.loc[mask, "raw_score"]

        mu = vals.mean()
        sd = vals.std()

        if sd == 0 or np.isnan(sd):
            df.loc[mask, "stuff_plus"] = 100
        else:
            z = (vals - mu) / sd
            df.loc[mask, "stuff_plus"] = 100 + 10 * z

    df["stuff_plus"] = df["stuff_plus"].round(1)
    return df


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _train_classifier(X, y, groups):

    if len(np.unique(y)) < 2:
        dummy = _dummy_classifier(y.mean())
        dummy.fit(X, y)
        return dummy, 0.500

    if XGB_AVAILABLE:
        model = xgb.XGBClassifier(
            n_estimators=FAST_ESTIMATORS if FAST else 400,
            max_depth=5,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=1.0,
            reg_lambda=2.0,
            min_child_weight=5,
            eval_metric="logloss",
            random_state=42,
            n_jobs=-1,
        )
    else:
        model = GradientBoostingClassifier(
            n_estimators=250,
            learning_rate=0.05,
            max_depth=4,
            random_state=42,
        )

    if FAST:
        # Skip CV — the AUC is diagnostic only; the leaderboard just needs the
        # fitted model. One fit instead of six.
        model.fit(X, y)
        return model, 0.0

    cv = GroupKFold(n_splits=5)
    scores = cross_val_score(
        model, X, y, cv=cv, groups=groups, scoring="roc_auc", n_jobs=-1,
    )
    model.fit(X, y)
    return model, scores.mean()


def _get_groups(df):

    for col in ["pitcher", "pitcher_id", "pitcher_name"]:
        if col in df.columns:
            return df[col].values

    return np.arange(len(df))


def _save_model(model, path):

    with open(path, "wb") as f:
        pickle.dump(model, f)


def _print_feature_importance(model, cols, label):

    if hasattr(model, "feature_importances_"):

        vals = model.feature_importances_
        pairs = sorted(zip(cols, vals), key=lambda x: -x[1])[:5]

        txt = ", ".join([f"{a}={b:.3f}" for a, b in pairs])
        print(f"  Top features ({label}): {txt}")


# ---------------------------------------------------------------------
# Dummy classifier
# ---------------------------------------------------------------------

class _dummy_classifier:

    def __init__(self, p=0.5):
        self.p = p

    def fit(self, X, y):
        return self

    def predict_proba(self, X):
        n = len(X)
        return np.column_stack([
            np.full(n, 1 - self.p),
            np.full(n, self.p)
        ])