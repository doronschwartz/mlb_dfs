"""Strikeout prop predictor — ported from Yaakov Bienstock's K Prop Tester
Colab notebook. Combines pitcher K%, lineup K% (weighted by batting order),
and park K factor into a predicted strikeout total per start.

The notebook also pulled Baseball Savant whiff-rate via pybaseball; we drop
that component here to stay on a single upstream (statsapi.mlb.com).
"""

from __future__ import annotations

from datetime import date as Date
from typing import Iterable

from . import mlb_api, savant

# Tuneable knobs from the notebook.
ROOKIE_DEFAULT_K_PCT = 0.25
ROOKIE_PITCHER_K_PCT = 0.22
LEAGUE_AVG_BF_PER_START = 23.5

# Fantasy points per K under the spreadsheet rules.
POINTS_PER_K = 1.5

# Without the whiff-rate component the active weights are pitcher_season +
# batter_k_rate; we re-normalize so they sum to 1 (was 0.31 + 0.31 = 0.62).
WEIGHTS = {
    "pitcher_season": 0.5,
    "batter_k_rate":  0.5,
}

# Lineup-position weights — the top of the order swings more, generates
# more BFs against the SP, hence higher contribution.
LINEUP_WEIGHTS = {
    1: 1.15, 2: 1.12, 3: 1.10, 4: 1.08, 5: 1.05,
    6: 1.02, 7: 1.00, 8: 0.98, 9: 0.95,
}

# Park-level K factor (>1.0 = pitcher-friendly for Ks). Verbatim from the
# notebook's PARK_K_FACTORS dict, with WSH/OAK aliases harmonized to MLB
# Stats API canonical abbreviations.
PARK_K_FACTORS = {
    "AZ":  1.02, "ATL": 0.98, "BAL": 1.01, "BOS": 0.96,
    "CHC": 1.00, "CWS": 1.03, "CIN": 0.97, "CLE": 1.02,
    "COL": 0.92, "DET": 1.01, "HOU": 1.00, "KC":  1.01,
    "LAA": 1.04, "LAD": 1.03, "MIA": 1.05, "MIL": 0.99,
    "MIN": 1.02, "NYM": 1.01, "NYY": 0.98, "ATH": 1.06,
    "PHI": 0.99, "PIT": 1.00, "SD":  1.04, "SF":  1.02,
    "SEA": 1.03, "STL": 0.98, "TB":  1.05, "TEX": 0.97,
    "TOR": 1.01, "WSH": 1.00,
}


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def pitcher_k_profile(pid: int, season: int) -> dict | None:
    """Returns {k_pct, avg_bf_per_start, gs, xwoba} or None if not enough data."""
    s = mlb_api.player_stats(pid, group="pitching", season=season)
    if not s:
        return None
    bf = _safe_float(s.get("battersFaced"))
    so = _safe_float(s.get("strikeOuts"))
    gs = _safe_float(s.get("gamesStarted"))
    sav = savant.lookup_pitcher(pid, season) or {}
    return {
        "k_pct": (so / bf) if bf >= 50 else ROOKIE_PITCHER_K_PCT,
        "avg_bf_per_start": (bf / gs) if gs >= 1 else LEAGUE_AVG_BF_PER_START,
        "gs": int(gs),
        "season_so": int(so),
        "season_bf": int(bf),
        "xera": _safe_float(sav.get("xera"), default=0.0) or None,
        "xwoba_against": _safe_float(sav.get("est_woba"), default=0.0) or None,
    }


def batter_k_profile(pid: int, season: int) -> dict | None:
    """Returns {k_pct, pa} or None if no data."""
    s = mlb_api.player_stats(pid, group="hitting", season=season)
    if not s:
        return None
    pa = _safe_float(s.get("plateAppearances"))
    so = _safe_float(s.get("strikeOuts"))
    return {
        "k_pct": (so / pa) if pa >= 30 else ROOKIE_DEFAULT_K_PCT,
        "pa": int(pa),
        "so": int(so),
    }


def predict_strikeouts(
    pitcher_stats: dict,
    lineup_profiles: list[dict | None],
    home_team_abbr: str,
) -> tuple[float, dict]:
    """Returns (predicted_ks, components dict for tooltip).

    pitcher_stats: from pitcher_k_profile()
    lineup_profiles: list of up to 9 batter_k_profile() entries (in batting
                     order). Missing entries fall back to the league rookie K%.
    home_team_abbr: for park K factor.
    """
    if not pitcher_stats:
        return 0.0, {}

    k_pct = pitcher_stats["k_pct"]
    avg_bf = pitcher_stats["avg_bf_per_start"]

    pitcher_comp = avg_bf * k_pct

    # Weighted batter K% across the (up to) 9 lineup spots.
    weighted_k = 0.0
    total_w = 0.0
    for i in range(9):
        slot = i + 1
        w = LINEUP_WEIGHTS.get(slot, 1.0)
        b = lineup_profiles[i] if i < len(lineup_profiles) else None
        bk = b["k_pct"] if b else ROOKIE_DEFAULT_K_PCT
        weighted_k += bk * w
        total_w += w
    batter_comp = avg_bf * (weighted_k / total_w)

    park = PARK_K_FACTORS.get(home_team_abbr, 1.0)

    base = (
        pitcher_comp * WEIGHTS["pitcher_season"]
        + batter_comp * WEIGHTS["batter_k_rate"]
    )
    predicted = base * park
    components = {
        "pitcher_comp": round(pitcher_comp, 2),
        "batter_comp": round(batter_comp, 2),
        "park_factor": park,
        "weighted_lineup_k_pct": round(weighted_k / total_w, 4),
        "pitcher_k_pct": round(k_pct, 4),
        "avg_bf_per_start": round(avg_bf, 2),
    }
    return round(predicted, 2), components


def k_props_for_date(d: Date) -> list[dict]:
    """One row per probable SP on the slate, with predicted Ks + breakdown."""
    season = d.year
    games = mlb_api.schedule(d)
    rows: list[dict] = []

    for g in games:
        teams = g.get("teams") or {}
        for side, opposing in (("away", "home"), ("home", "away")):
            side_data = teams.get(side) or {}
            opp_data = teams.get(opposing) or {}
            sp = side_data.get("probablePitcher") or {}
            sp_id = sp.get("id")
            if not sp_id:
                continue
            sp_name = sp.get("fullName") or "?"
            sp_team_abbr = ((side_data.get("team") or {}).get("abbreviation"))
            opp_team_abbr = ((opp_data.get("team") or {}).get("abbreviation"))
            home_team_abbr = ((teams.get("home", {}).get("team") or {}).get("abbreviation"))
            away_team_abbr = ((teams.get("away", {}).get("team") or {}).get("abbreviation"))
            is_home = sp_team_abbr == home_team_abbr

            pp = pitcher_k_profile(sp_id, season)
            if not pp:
                continue

            # Opposing lineup, if posted.
            lineup_ids: list[int] = []
            lineups = g.get("lineups") or {}
            if opposing == "home":
                lineup_ids = [
                    p.get("id") for p in (lineups.get("homePlayers") or [])
                    if p.get("id")
                ]
            else:
                lineup_ids = [
                    p.get("id") for p in (lineups.get("awayPlayers") or [])
                    if p.get("id")
                ]
            lineup_profiles: list[dict | None] = []
            for bid in lineup_ids[:9]:
                lineup_profiles.append(batter_k_profile(bid, season))

            predicted, components = predict_strikeouts(
                pp, lineup_profiles, home_team_abbr or "",
            )
            rows.append({
                "pitcher_id": sp_id,
                "pitcher_name": sp_name,
                "pitcher_team": sp_team_abbr,
                "opp_team": opp_team_abbr,
                "home_team": home_team_abbr,
                "away_team": away_team_abbr,
                "is_home": is_home,
                "matchup": f"{away_team_abbr}@{home_team_abbr}",
                "lineup_posted": bool(lineup_profiles),
                "predicted_ks": predicted,
                "predicted_k_pts": round(predicted * POINTS_PER_K, 2),
                "components": components,
                "pitcher_k_pct": components.get("pitcher_k_pct", 0),
                "avg_bf_per_start": components.get("avg_bf_per_start", 0),
                "park_factor": components.get("park_factor", 1.0),
                "game_pk": g.get("gamePk"),
                "game_status": (g.get("status") or {}).get("detailedState"),
            })

    rows.sort(key=lambda r: r["predicted_ks"], reverse=True)
    return rows
