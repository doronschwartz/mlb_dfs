"""
features.py — Pro-grade public Stuff+ feature engine
====================================================

Goal:
Model intrinsic pitch quality ("Stuff+") using public Statcast data.

Design philosophy:
- Use pitch characteristics only
- Exclude command/count/location intentionally
- Build robust physical shape + deception features
- Build clean binary outcome labels

No plate_x / plate_z / count variables included.

Compatible with model.py
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------
# Pitch Types
# ---------------------------------------------------------------------

PITCH_TYPES_TO_MODEL = {
    "FF": "4-Seam Fastball",
    "SI": "Sinker",
    "FC": "Cutter",
    "SL": "Slider",
    "ST": "Sweeper",
    "CU": "Curveball",
    "KC": "Knuckle Curve",
    "CH": "Changeup",
    "FS": "Splitter",
}

MIN_PITCHES_PER_PITCHER_TYPE = 50


# ---------------------------------------------------------------------
# Model Features
# ---------------------------------------------------------------------

FEATURE_COLS = [
    # Raw traits
    "release_speed",
    "release_spin_rate",
    "release_extension",
    "release_pos_x",
    "release_pos_z",

    # Shape
    "pfx_x_in",
    "pfx_z_in",

    # Flight
    "vaa",
    "haa",
    "speed_drop",

    # Deception / public proxies
    "release_height_adj",
    "release_side_adj",
    "ivb_per_mph",
    "hb_per_mph",
    "spin_eff_proxy",
# -------------------------
    # Derived velocity traits
    # -------------------------
    "speed_drop",

    # -------------------------
    # Pitcher identity / arsenal context
    # -------------------------
    "velo_diff_fb",
    "hb_diff_fb",
    "ivb_diff_fb",
    "vaa_diff_fb",

    # -------------------------
    # Context interaction
    # -------------------------
    "same_hand",
]


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    # -------------------------------------------------------------
    # Keep modeled pitch types
    # -------------------------------------------------------------
    df = df[df["pitch_type"].isin(PITCH_TYPES_TO_MODEL)].copy()

    # -------------------------------------------------------------
    # Keep relevant pitch outcomes
    # -------------------------------------------------------------
    valid_events = {
        "swinging_strike",
        "swinging_strike_blocked",
        "foul",
        "foul_tip",
        "hit_into_play",
        "called_strike",
    }

    df = df[df["description"].isin(valid_events)].copy()

    # -------------------------------------------------------------
    # Required columns
    # -------------------------------------------------------------
    req = [
        "release_speed",
        "release_spin_rate",
        "release_extension",
        "release_pos_x",
        "release_pos_z",
        "pfx_x",
        "pfx_z",
        "vx0",
        "vy0",
        "vz0",
        "ax",
        "ay",
        "az",
    ]

    keep = [c for c in req if c in df.columns]
    df = df.dropna(subset=keep)

    # -----------------------------
    # Platoon / handedness feature
    # -----------------------------
    if "p_throws" in df.columns and "stand" in df.columns:
        df["same_hand"] = (
                df["p_throws"].fillna("") == df["stand"].fillna("")
        ).astype(int)
    else:
        df["same_hand"] = 0

    # -------------------------------------------------------------
    # Convert movement to inches
    # -------------------------------------------------------------
    df["pfx_x_in"] = df["pfx_x"] * 12
    df["pfx_z_in"] = df["pfx_z"] * 12

    # -------------------------------------------------------------
    # Kinematic calculations
    # -------------------------------------------------------------
    _calc_approach_angles(df)

    # -------------------------------------------------------------
    # Public Stuff+ engineered features
    # -------------------------------------------------------------
    _calc_shape_proxies(df)

    # -------------------------------------------------------------
    # Labels
    # -------------------------------------------------------------
    _build_labels(df)

    # -------------------------------------------------------------
    # Filter pitcher/pitch_type minimums
    # -------------------------------------------------------------
    df = _filter_minimums(df)

    # -----------------------------------------
    # Pitcher-level arsenal context features
    # -----------------------------------------

    if "pitcher" in df.columns:

        # Fastball reference per pitcher. FIX: was FF-only, which made every
        # sinker-primary pitcher with no 4-seam (Cristopher Sánchez, Framber
        # Valdez, Bassitt, ~half the league) get NaN *_fb features → dropped
        # entirely by the dropna in train_models. Fall back to the sinker (SI),
        # then any fastest fastball-family pitch, so they aren't lost.
        def _fb_agg(d):
            return d.groupby("pitcher").agg({
                "release_speed": "mean", "pfx_x_in": "mean",
                "pfx_z_in": "mean", "vaa": "mean",
            }).rename(columns={"release_speed": "fb_vel", "pfx_x_in": "fb_hb",
                               "pfx_z_in": "fb_ivb", "vaa": "fb_vaa"})
        ff = _fb_agg(df[df["pitch_type"] == "FF"])
        si = _fb_agg(df[df["pitch_type"] == "SI"])
        fc = _fb_agg(df[df["pitch_type"] == "FC"])
        # prefer FF, else SI, else cutter — pitchers' velocity/shape anchor
        fb = ff.combine_first(si).combine_first(fc)

        df = df.merge(fb, on="pitcher", how="left")

        # velocity differential from fastball
        df["velo_diff_fb"] = df["release_speed"] - df["fb_vel"]

        # movement differential from fastball
        df["hb_diff_fb"] = df["pfx_x_in"] - df["fb_hb"]
        df["ivb_diff_fb"] = df["pfx_z_in"] - df["fb_ivb"]

        # angle separation
        df["vaa_diff_fb"] = df["vaa"] - df["fb_vaa"]

    else:
        df["velo_diff_fb"] = 0
        df["hb_diff_fb"] = 0
        df["ivb_diff_fb"] = 0
        df["vaa_diff_fb"] = 0

    # -------------------------------------------------------------
    # Feature list
    # -------------------------------------------------------------
    available = [c for c in FEATURE_COLS if c in df.columns]
    df.attrs["feature_cols"] = available

    print("      Pitch type counts:")
    for pt, n in df["pitch_type"].value_counts().items():
        print(f"        {pt} ({PITCH_TYPES_TO_MODEL.get(pt, pt)}): {n:,}")

    return df


def get_feature_cols(df: pd.DataFrame) -> list[str]:
    return df.attrs.get(
        "feature_cols",
        [c for c in FEATURE_COLS if c in df.columns]
    )


# ---------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------

def _calc_approach_angles(df):

    # Approximate travel from release to plate
    # using public statcast acceleration vectors

    disc = df["vy0"]**2 - 2 * df["ay"] * (-55)

    disc = np.maximum(disc, 0)

    t = (-df["vy0"] - np.sqrt(disc)) / df["ay"]

    df["t_plate"] = t

    df["vx_plate"] = df["vx0"] + df["ax"] * t
    df["vy_plate"] = df["vy0"] + df["ay"] * t
    df["vz_plate"] = df["vz0"] + df["az"] * t

    df["vaa"] = np.degrees(
        np.arctan(df["vz_plate"] / np.abs(df["vy_plate"]))
    )

    df["haa"] = np.degrees(
        np.arctan(df["vx_plate"] / np.abs(df["vy_plate"]))
    )

    speed_plate = np.sqrt(
        df["vx_plate"]**2 +
        df["vy_plate"]**2 +
        df["vz_plate"]**2
    ) * (3600 / 5280)

    df["speed_drop"] = df["release_speed"] - speed_plate


# ---------------------------------------------------------------------
# Shape / deception proxies
# ---------------------------------------------------------------------

def _calc_shape_proxies(df):

    # Normalize release traits
    df["release_height_adj"] = df["release_pos_z"] - df["release_pos_z"].median()

    df["release_side_adj"] = df["release_pos_x"] - df["release_pos_x"].median()

    # Ride per velo
    df["ivb_per_mph"] = df["pfx_z_in"] / df["release_speed"]

    # Run / sweep per velo
    df["hb_per_mph"] = np.abs(df["pfx_x_in"]) / df["release_speed"]

    # crude spin efficiency proxy
    df["spin_eff_proxy"] = (
        np.sqrt(df["pfx_x_in"]**2 + df["pfx_z_in"]**2)
        / df["release_spin_rate"].replace(0, np.nan)
    )

    df["spin_eff_proxy"] = df["spin_eff_proxy"].fillna(0)


# ---------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------

def _build_labels(df):

    df["whiff"] = df["description"].isin(
        {"swinging_strike", "swinging_strike_blocked", "foul_tip"}
    ).fillna(False).astype(int)

    df["called_strike"] = (
        df["description"].eq("called_strike")
    ).fillna(False).astype(int)

    df["in_play"] = (
        df["description"].eq("hit_into_play")
    ).fillna(False).astype(int)

    # Weak contact
    if "launch_speed" in df.columns and "bb_type" in df.columns:
        mask = (
            df["in_play"].eq(1) &
            (
                df["launch_speed"].fillna(999).lt(80) |
                df["bb_type"].fillna("").eq("popup")
            )
        )
        df["weak_contact"] = mask.fillna(False).astype(int)
    else:
        df["weak_contact"] = 0

    # Groundball
    if "bb_type" in df.columns:
        mask = (
            df["in_play"].eq(1) &
            df["bb_type"].fillna("").eq("ground_ball")
        )
        df["ground_ball"] = mask.fillna(False).astype(int)
    else:
        df["ground_ball"] = 0

    # Hard hit
    if "launch_speed" in df.columns:
        mask = (
            df["in_play"].eq(1) &
            df["launch_speed"].fillna(0).ge(95)
        )
        df["hard_hit"] = mask.fillna(False).astype(int)
    else:
        df["hard_hit"] = 0

    # Barrel proxy
    if "launch_speed" in df.columns and "launch_angle" in df.columns:
        mask = (
            df["in_play"].eq(1) &
            df["launch_speed"].fillna(0).ge(98) &
            df["launch_angle"].fillna(-999).between(26, 30)
        )
        df["barrel"] = mask.fillna(False).astype(int)
    else:
        df["barrel"] = 0

    # xwOBA
    if "estimated_woba_using_speedangle" in df.columns:
        df["xwoba"] = df["estimated_woba_using_speedangle"].fillna(0)
    else:
        df["xwoba"] = (
            0.00 * df["whiff"] +
            0.10 * df["weak_contact"] +
            0.32 * df["in_play"] +
            0.70 * df["hard_hit"] +
            0.95 * df["barrel"]
        )


# ---------------------------------------------------------------------
# Minimums
# ---------------------------------------------------------------------

def _filter_minimums(df):

    if "pitcher" not in df.columns:
        return df

    counts = (
        df.groupby(["pitcher", "pitch_type"])
        .size()
        .reset_index(name="n")
    )

    valid = counts[
        counts["n"] >= MIN_PITCHES_PER_PITCHER_TYPE
    ][["pitcher", "pitch_type"]]

    df = df.merge(
        valid,
        on=["pitcher", "pitch_type"],
        how="inner"
    )

    return df