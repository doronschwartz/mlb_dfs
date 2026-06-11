"""Smart projections.

For each player on the slate we estimate expected fantasy points using:

Hitters:
    14-day rate stats (per game) -> base projection
    + park & opposing-pitcher adjustments
    - if no recent data, fall back to season rates, then league avg

Pitchers (starters only):
    14-day pitching rates -> base projection
    + opposing-team K% / wRC+ proxy via opponent recent runs/game

The point is to be useful, not Vegas-grade. Everything is transparent so the
draft assistant can show *why* it likes a player.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Iterable

from . import injuries, mlb_api, odds_api, savant, weather as weather_mod
from .scoring import HITTER_POINTS, PITCHER_POINTS

# Team ID → abbr (small static map; MLB IDs are stable)
_TEAM_ABBR = {
    108: "LAA", 109: "AZ", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "ATH", 134: "PIT", 135: "SD", 136: "SEA",
    137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}

# Team ID → full name (for joining with odds_api which keys by full name).
_TEAM_FULLNAME = {
    108: "Los Angeles Angels", 109: "Arizona Diamondbacks", 110: "Baltimore Orioles",
    111: "Boston Red Sox", 112: "Chicago Cubs", 113: "Cincinnati Reds",
    114: "Cleveland Guardians", 115: "Colorado Rockies", 116: "Detroit Tigers",
    117: "Houston Astros", 118: "Kansas City Royals", 119: "Los Angeles Dodgers",
    120: "Washington Nationals", 121: "New York Mets", 133: "Athletics",
    134: "Pittsburgh Pirates", 135: "San Diego Padres", 136: "Seattle Mariners",
    137: "San Francisco Giants", 138: "St. Louis Cardinals", 139: "Tampa Bay Rays",
    140: "Texas Rangers", 141: "Toronto Blue Jays", 142: "Minnesota Twins",
    143: "Philadelphia Phillies", 144: "Atlanta Braves", 145: "Chicago White Sox",
    146: "Miami Marlins", 147: "New York Yankees", 158: "Milwaukee Brewers",
}

LEAGUE_AVG_HITTER_POINTS_PER_GAME = 7.0   # bumped from 6.5 — "—" qoc tier (no Statcast
                                            # sample, mostly callups) showed posterior bias
                                            # +0.74 ± 0.11 (P=100% under-projecting) across
                                            # 18 days. Most affected by this constant via
                                            # the ghost prior in the bucket-weighted base.
LEAGUE_AVG_SP_POINTS_PER_START = 11.0

# Pitcher projection de-compression (v9.29). Projections were over-shrunk
# toward the league prior, over-projecting bad starts and under-projecting good
# ones. Expand the spread around a pivot: proj = pivot + (proj - pivot) * k.
# A/B-tuned (n=172): pivot 9 / k 1.25 → overall MAE 6.44→6.24, bucket biases
# roughly halved. Applied before the opener clamp.
_PIT_SPREAD_PIVOT = 9.0
_PIT_SPREAD_K = 1.25

# HOT-hitter post-matchup boost. Recurring multi-audit signal: HOT bats keep
# beating their projection (+1.11 → +1.22 → +2.13 over successive windows),
# ~3.3σ — past the documented 0.7σ ratchet threshold. 3-day A/B (n=113 HOT)
# was monotonic and overshoot-free: 1.07→1.13 cuts HOT bias +2.11→+1.47 and
# HOT MAE 6.98→6.89 (overall MAE 3.98→3.97). Ratcheted one notch to 1.13;
# re-audit before going further (HOT is high-variance).
_HOT_HITTER_BOOST = 1.13

# v9.35 hitter compression — projections too spread (studs over, scrubs under).
# proj = pivot + (proj - pivot) * k, k<1 pulls extremes toward the mean. Pivot ~
# league-avg hitter pts/G. A/B-tunable.
_HIT_COMPRESS_PIVOT = 5.6
# A/B (6-day, n=1662): k=0.85 closes scrubs (+0.27→-0.05) and more than halves
# studs (-1.92→-0.75) with flat overall MAE; 0.78 overshoots scrubs + hurts MAE.
_HIT_COMPRESS_K = 0.85

# COLD-pitcher post-matchup shrink. Recurring high-σ over-projection (cold
# starters implode worse than the chain implies) — progressively tightened
# 0.80→0.70→0.65→0.55; 6-day audit still shows COLD pitcher bias -4.04 (4.2σ).
# A/B-tunable here. v9.33: 0.55→0.38 after a 5-day A/B (monotonic, no overshoot)
# — COLD bias -3.79→-2.24, COLD MAE 5.13→4.60, overall pitcher bias -1.42→-1.10.
# (0.30 tested even better; left margin on this high-variance bucket.)
_COLD_PITCHER_SHRINK = 0.38

# v9.34 ELITE/SOLID-QoC pitcher trim (good-stuff tiers over-projected; 6-day
# incl Sun: SOLID -1.53/4.5σ, ELITE -0.67/2.3σ). A/B (n=159): trim improves
# overall pitcher bias -0.76→ and MAE monotonically, no overshoot. ELITE light
# (0.97 → -0.12, harder overshoots); SOLID firmer (stubborn). A/B-tunable.
_PIT_QOC_TRIM = True
_PIT_QOC_TRIM_SOLID = 0.93
_PIT_QOC_TRIM_ELITE = 0.97

# Streak-override recency weight (HOT/COLD hitters): base = w*L3 + (1-w)*weighted.
# JL flagged daily projections may over-weight recent form — A/B-tunable here.
_STREAK_W = 0.85

# v9.39 batter total-bases prop factor. The hitter-side mirror of the pitcher
# K-prop blend: batter_total_bases is the sharpest per-HITTER market signal
# available (TB ≈ the 3/5/8/10 hit-scoring core, ~2.7 pts/TB), and it prices
# pitch-type matchup, recent news, and lineup context the chain can't see.
# Most books quote the same 1.5 line for everyone and vary the JUICE, so the
# signal is the devigged P(over) — converted to a market-expected-TB scalar,
# z-scored ACROSS THE SLATE (self-normalizing: no absolute anchor to tune),
# clamped and damped to ±5% max. Hitters without a posted prop get 1.0.
# Cannot be backtested (no historical prop archive) — shipped damped behind
# this flag; forward-validate via the calibration audit (components stored).
_BAT_TB_PROP = True
_BAT_TB_PROP_WEIGHT = 0.02   # factor = 1 + clamp(z, ±2.5) * this → max ±5%

# v9.20 tier-targeted lift for AVERAGE/POOR-QoC startable pitchers (see
# project_pitcher). A/B-confirmed on the 6-day window (n=157): all-pitcher
# bias +1.00→+0.72 AND MAE 5.87→5.78; targeted AVERAGE/POOR subset (n=100)
# +1.65→+1.20 / MAE 5.71→5.57 — improves both. Flag kept so future audits
# can re-score it on/off.
_PIT_QOC_LIFT = True

# League-median Statcast benchmarks for the multiplier (rough 2024-25 medians).
# League baselines are now COMPUTED dynamically from current Statcast
# leaderboards via savant.league_averages(season), 24h disk-cached. Avoids
# hardcoded values going stale through the season. The constants below are
# accessor functions wrapping the cached dict — call with the season int.
# If Statcast is unreachable, savant uses a 2026 mid-season fallback.

def _lg(season: int) -> dict:
    return savant.league_averages(season)

def LG_BARREL_PCT_HITTER(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["brl_pct_hitter"]

def LG_HARDHIT_PCT_HITTER(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["hh_pct_hitter"]

def LG_BARREL_PCT_ALLOWED(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["brl_pct_allowed"]

def LG_HARDHIT_PCT_ALLOWED(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["hh_pct_allowed"]

def LG_XERA(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["xera"]

def LG_XWOBA_HITTER(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["xwoba_hitter"]

def LG_XWOBA_AGAINST(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["xwoba_against"]

def LG_SWEETSPOT_PCT(season: int = None) -> float:
    s = season or Date.today().year
    return _lg(s)["sweetspot_pct"]


def _statcast_implied_pg_hitter(brl: float, hh: float) -> float | None:
    """True-talent pts/G estimate from Statcast quality-of-contact metrics.
    Blended with the rolling base in project_hitter.

    Coefficients tuned against 3 days of calibration data. Original formula
    (1.5 + 0.50*brl + 0.045*hh, cap 11) over-projected ELITE-tier hitters by
    ~1.3 pts. Compressed the upper end: lower cap and slope on barrel%."""
    if not brl and not hh:
        return None
    val = 2.0 + (brl or LG_BARREL_PCT_HITTER()) * 0.42 + (hh or LG_HARDHIT_PCT_HITTER()) * 0.040
    return max(3.5, min(val, 9.5))


def _statcast_implied_ps_pitcher(xera, xwoba, brl_a) -> float | None:
    """True-talent pts/start estimate from xERA + xwOBA-against + barrel-allowed.
    Better Statcast → higher pts/start. League-average 4.20 xERA / 0.310 xwOBA /
    6.5 brl-allowed → 11 pts/start."""
    if xera is None and xwoba is None and brl_a is None:
        return None
    base = LEAGUE_AVG_SP_POINTS_PER_START
    if xera is not None:
        base += (4.20 - xera) * 2.5     # 1.0 xERA edge → +2.5 pts/start
    if xwoba is not None:
        base += (0.310 - xwoba) * 30    # 0.030 xwOBA edge → +0.9 pts/start
    if brl_a is not None:
        base += (LG_BARREL_PCT_ALLOWED() - brl_a) * 0.4
    return max(5.0, min(base, 22.0))


def _qoc_residual_hitter(qoc: dict | None) -> tuple[float, list[str]]:
    """Narrow residual factor for signals not in the implied baseline (sweet
    spot %). Capped ±5% — most QoC signal is now in the blended base."""
    if not qoc:
        return 1.0, []
    notes = []
    factor = 1.0
    ss = _safe_float(qoc.get("anglesweetspotpercent"))
    if ss:
        delta = (ss - 33.0) / 33.0  # league avg ~33
        factor *= 1.0 + max(-0.04, min(delta * 0.10, 0.04))
        notes.append(f"sweet-spot {ss:.0f}%")
    return max(0.95, min(factor, 1.05)), notes


def _qoc_tier_hitter(brl: float, hh: float) -> str:
    if brl and brl >= 11: return "ELITE"
    if brl and brl >= 8: return "SOLID"
    if brl and brl >= 5: return "AVERAGE"
    if brl: return "POOR"
    return "—"


def _qoc_tier_pitcher(brl_a: float, xera: float) -> str:
    if (brl_a and brl_a <= 4) or (xera and xera <= 2.75): return "ELITE"
    if (brl_a and brl_a <= 6) or (xera and xera <= 3.50): return "SOLID"
    if (brl_a and brl_a <= 8) or (xera and xera <= 4.50): return "AVERAGE"
    if brl_a or xera: return "POOR"
    return "—"


def _qoc_residual_pitcher(qoc: dict | None, expected: dict | None) -> tuple[float, list[str]]:
    """Narrow residual factor for pitcher signals not in the implied baseline
    (hard-hit% allowed). Capped ±5%."""
    factor = 1.0
    notes = []
    if qoc:
        hh_a = _safe_float(qoc.get("ev95percent"))
        if hh_a:
            lg_hh = LG_HARDHIT_PCT_ALLOWED()   # pitcher-side context, was using hitter constant
            delta = (hh_a - lg_hh) / lg_hh
            factor *= 1.0 - max(-0.04, min(delta * 0.08, 0.04))
            notes.append(f"hh-allowed {hh_a:.0f}%")
    return max(0.95, min(factor, 1.05)), notes


@dataclass
class Projection:
    player_id: int
    name: str
    team_id: int | None
    position: str | None
    role: str  # "hitter" or "pitcher"
    projected_points: float
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _bullpen_era_by_team(season: int) -> dict[int, float]:
    """Season bullpen ERA per team. Computed from team pitching - SP pitching.
    Returns team_id -> ERA. Empty dict on failure."""
    out: dict[int, float] = {}
    try:
        # MLB Stats API bulk team pitching stats split by starter/reliever.
        data = mlb_api._get(
            "/teams/stats",
            params={"sportId": 1, "stats": "season", "group": "pitching",
                    "season": season, "gameType": "R", "playerPool": "all"},
        )
    except Exception:
        return out
    # Standard pitching split is per team; we don't get reliever-only here without
    # a different endpoint. Easier: fetch per-team via season stats with split.
    # Fall back to plain season ERA (whole staff) — bullpen-only would require
    # 30 separate calls, too expensive. Whole-staff ERA still reflects pen
    # quality reasonably (since 35-40% of innings are bullpen).
    for split in (data.get("stats") or [{}])[0].get("splits", []):
        team = (split.get("team") or {})
        tid = team.get("id")
        era = _safe_float((split.get("stat") or {}).get("era"))
        if tid and era:
            out[tid] = era
    return out


# -------- v9.3 advanced factors --------

def _team_defense_factor(team_id: int | None, season: int) -> tuple[float, str | None]:
    """Team defense impact on pitcher BABIP. Returns (factor, note).
    Better defense → fewer hits allowed → higher pitcher projection.
    Uses team fielding percentage as a coarse proxy for DRS/OAA (which MLB
    Stats API doesn't expose). Capped ±3%. Cached implicitly via mlb_api._get.
    """
    if not team_id:
        return 1.0, None
    try:
        data = mlb_api._get(
            f"/teams/{team_id}/stats",
            params={"stats": "season", "group": "fielding", "season": season},
        )
        splits = []
        for s in data.get("stats", []):
            splits.extend(s.get("splits", []))
        if not splits:
            return 1.0, None
        fpct = _safe_float(splits[0].get("stat", {}).get("fielding"))
        if fpct <= 0:
            return 1.0, None
        # League avg fielding pct ~0.984. Each 0.005 → ~1 unit of impact.
        delta_units = (fpct - 0.984) / 0.005
        factor = 1.0 + delta_units * 0.015  # ±1.5% per 0.005 deviation
        factor = max(0.97, min(factor, 1.03))
        return factor, f"team D fpct {fpct:.3f} x{factor:.2f}"
    except Exception as e:
        logging.debug("team_defense_factor failed for team %s: %s", team_id, e)
        return 1.0, None


def _pitcher_tto_factor(ip: float, gs: int) -> tuple[float, str | None]:
    """Times-through-order penalty. Well-documented ~30-point wOBA jump on
    3rd time through the lineup. Estimated by avg IP/start:
       <4.5 IP/start → doesn't reach TTO3, no penalty
       4.5–5.5     → light penalty (×0.99)
       >5.5         → deeper penalty (×0.975) — most starters who routinely
                      face the order 3x get hit harder on the late turn
    """
    if gs <= 0 or ip <= 0:
        return 1.0, None
    ip_per = ip / gs
    if ip_per >= 5.5:
        return 0.975, f"TTO3 penalty x0.975 ({ip_per:.1f} IP/start)"
    if ip_per >= 4.5:
        return 0.99, f"TTO2 penalty x0.99 ({ip_per:.1f} IP/start)"
    return 1.0, None


def _pitcher_opener_check(ip: float, gs: int) -> tuple[bool, str | None]:
    """If rolling avg IP/start <2.5, this pitcher is an opener. Their pts/start
    naturally caps at ~6-8 pts and shouldn't be projected like a real starter.
    Returns (is_opener, note). Caller can flag it and clamp the projection ceiling.
    """
    if gs <= 0 or ip <= 0:
        return False, None
    ip_per = ip / gs
    if ip_per < 2.5:
        return True, f"OPENER role ({ip_per:.1f} IP/start avg)"
    return False, None


def _hitter_iso_form(stats_l14: dict, stats_season: dict) -> tuple[float, str | None]:
    """Recent ISO (SLG−AVG) vs season ISO. ISO is a pure power signal —
    separates HR-streak hot from BIP-luck hot. A hitter pacing well above
    season ISO has real HR upside that smoothed pts/G under-weights, since
    HRs are 10 DK pts each (large variance contributor).
    Capped ±4%.
    """
    def _iso(d: dict) -> float | None:
        slg = _safe_float(d.get("slg"))
        avg = _safe_float(d.get("avg"))
        if slg <= 0 or avg <= 0:
            return None
        return slg - avg
    iso_14 = _iso(stats_l14)
    iso_sz = _iso(stats_season)
    if not iso_14 or not iso_sz or iso_sz < 0.080:
        return 1.0, None
    if _safe_float(stats_l14.get("gamesPlayed")) < 7:
        return 1.0, None
    ratio = iso_14 / iso_sz
    if ratio >= 1.20:
        factor = 1.0 + min((ratio - 1.0) * 0.10, 0.04)
        return factor, f"ISO surge {iso_14:.3f} vs szn {iso_sz:.3f} x{factor:.2f}"
    if ratio <= 0.80:
        factor = max(1.0 + (ratio - 1.0) * 0.10, 0.96)
        return factor, f"ISO slump {iso_14:.3f} vs szn {iso_sz:.3f} x{factor:.2f}"
    return 1.0, None


def _hitter_sb_bonus(stats_l14: dict, stats_season: dict, opposing_sp: dict | None) -> tuple[float, str | None]:
    """SB modeling — coarse v1. Hitters with established SB threat (>10 SB
    per 100G pace) get a small bonus, amplified vs poor-pickoff pitchers
    (high SB-allowed rate). SBs are 2 DK pts each.

    TODO: incorporate sprint_speed from Savant + catcher pop time for
    higher-fidelity modeling. For now we proxy via rolling SB rates.
    """
    sb_14 = _safe_float(stats_l14.get("stolenBases"))
    g14 = _safe_float(stats_l14.get("gamesPlayed"))
    sb_szn = _safe_float(stats_season.get("stolenBases"))
    g_szn = _safe_float(stats_season.get("gamesPlayed"))
    sb_rate = 0.0
    if g14 >= 5:
        sb_rate = sb_14 / g14
    elif g_szn >= 20:
        sb_rate = sb_szn / g_szn
    if sb_rate < 0.10:  # <10 SB per 100G — not a threat
        return 1.0, None
    bonus = sb_rate * 0.035  # ~3.5% per SB/G
    if opposing_sp:
        sb_allowed = _safe_float(opposing_sp.get("stolenBases"))
        ip_pitcher = _safe_float(opposing_sp.get("inningsPitched"))
        if ip_pitcher >= 20:
            sb_per_9 = (sb_allowed / ip_pitcher) * 9
            if sb_per_9 > 0.8:   # weak battery (league avg ~0.5)
                bonus *= 1.5
            elif sb_per_9 < 0.3: # tight battery
                bonus *= 0.5
    factor = 1.0 + min(bonus, 0.04)
    return factor, f"SB threat {sb_rate*100:.0f}/100G x{factor:.2f}"


def project_hitter(
    pid: int,
    name: str,
    *,
    team_id: int | None,
    position: str | None,
    season: int,
    opposing_sp_id: int | None,
    park: dict | None = None,
    batting_order: int | None = None,
    implied_team_total: float | None = None,
    opp_bullpen_era: float | None = None,
    bats: str | None = None,
    opp_throws: str | None = None,
    opp_abbr: str | None = None,
    opp_sp_name: str | None = None,
    is_home: bool | None = None,
    lineup_status: str | None = None,
    tb_prop: dict | None = None,
    as_of: Date | None = None,
) -> Projection:
    last3 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=3, as_of=as_of)
    last7 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=7, as_of=as_of)
    last14 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=14, as_of=as_of)
    seasn = mlb_api.player_stats(pid, group="hitting", season=season, as_of=as_of)

    games_14 = _safe_float(last14.get("gamesPlayed"))
    games_7 = _safe_float(last7.get("gamesPlayed"))
    games_3 = _safe_float(last3.get("gamesPlayed"))
    base_pg = LEAGUE_AVG_HITTER_POINTS_PER_GAME
    notes: list[str] = []

    games_season = _safe_float(seasn.get("gamesPlayed"))
    pg_3 = _per_game_hitter_points(last3) if games_3 >= 1 else None
    pg_7 = _per_game_hitter_points(last7) if games_7 >= 2 else None
    pg_14 = _per_game_hitter_points(last14) if games_14 >= 5 else None
    pg_season = _per_game_hitter_points(seasn) if games_season >= 10 else None
    # Per-game category rates for H2H Cat valuation. Prefer L14 if sample
    # is reasonable; else season; else league average.
    if games_14 >= 5:
        rolling_cats = _per_game_hitter_cats(last14)
        rolling_events = _per_game_hitter_events(last14)
    elif games_season >= 10:
        rolling_cats = _per_game_hitter_cats(seasn)
        rolling_events = _per_game_hitter_events(seasn)
    else:
        rolling_cats = dict(LG_HITTER_RATES)
        rolling_events = None

    weighted = _weighted_pg_hitter(
        pts_g_3=(pg_3, int(games_3)),
        pts_g_7=(pg_7, int(games_7)),
        pts_g_14=(pg_14, int(games_14)),
        pts_g_season=(pg_season, int(games_season)),
    )
    if weighted is not None:
        base_pg = weighted
        notes.append(f"weighted L3/L7/L14: {base_pg:.2f} pts/G")
    elif pg_season is not None:
        base_pg = pg_season
        notes.append(f"season fallback: {int(_safe_float(seasn.get('gamesPlayed')))} G, {base_pg:.2f} pts/G")
    else:
        notes.append("no sample, league average")
    form_tag, form_note = _form_tag_hitter(pg_3, pg_7, pg_14, games_3, games_14)
    if form_note:
        notes.append(form_note)

    # Streak-trust override RESTORED.
    # The whole "streak override is wrong" arc was based on a calibration
    # endpoint that secretly used today's stats for past-date L3/L7/L14
    # windows (mlb_api.player_stats date bug, fixed 2026-05-10). Once the
    # date bug + cache-rev bug were fixed and projections recomputed, the
    # 17-day audit (n=4,949) flipped sign:
    #   HOT  bias +4.79 (14σ, n=703) — under-projecting HOT by ~5 pts
    #   COLD bias -2.97 (27σ, n=1369) — over-projecting COLD by ~3 pts
    # Streaks DO continue at much higher rates than the base bucket-weight
    # mix implies. Restored at 0.70 weight (the original setting before all
    # the noise-driven flailing). The L3 sample carries 70% of the base
    # for HOT/COLD only; untagged players still get the natural bucket
    # blend without override.
    if pg_3 is not None and games_3 >= 2 and form_tag in ("HOT", "COLD"):
        pg_3_safe = max(pg_3, 0.0)  # K-heavy 3 games don't predict negative true talent
        # 0.85 weight after iterative audits on the bug-fixed pipeline:
        #   v5 0.70 → HOT +1.83 / COLD -1.31 (still under/over)
        #   v6 0.80 → HOT +1.31 / COLD -1.10 (still moving same direction)
        # Each +0.10 weight closes ~28% / 16% of residual. v7 at 0.85 should
        # bring HOT into ±0.7 range — getting close to single-day noise floor.
        streak_base = _STREAK_W * pg_3_safe + (1 - _STREAK_W) * base_pg
        notes.append(f"streak override ({form_tag}): {_STREAK_W}*L3 + {round(1-_STREAK_W,2)}*weighted → {streak_base:.2f}")
        base_pg = streak_base

    # Opposing SP adjustment: ERA + WHIP + K/9. K/9 added in v9.3 because K is the
    # single biggest fantasy event for hitters (a K is -1 pt vs +3 for a single).
    # ERA/WHIP alone underweighted strikeout pitchers like Skubal/Skenes who
    # have moderate ERA but elite K rates — they suppress hitter projections
    # more than ERA implies. Weighted 0.45/0.25/0.30.
    sp_factor = 1.0
    opposing_sp_stats: dict | None = None
    sp_factor_source = None  # "season" | "savant_fallback" | None
    if opposing_sp_id:
        sp_season = mlb_api.player_stats(opposing_sp_id, group="pitching", season=season, as_of=as_of)
        opposing_sp_stats = sp_season
        ip = _safe_float(sp_season.get("inningsPitched"))
        if ip >= 20:
            era = _safe_float(sp_season.get("era"), default=4.20)
            whip = _safe_float(sp_season.get("whip"), default=1.30)
            k9 = _safe_float(sp_season.get("strikeoutsPer9Inn"), default=8.5)
            k9_part = 8.5 / max(k9, 4.0)   # inverse: high K/9 → low hitter factor
            # v9.9: blend pitcher whiff% percentile (forward-looking K-skill,
            # more sample-efficient than rolling K/9). Savant 0-100 percentile;
            # convert to a multiplier roughly centered at 50th = 1.0. A 90th
            # percentile whiff pitcher (elite K stuff) gets ~0.85 (suppresses
            # hitter by 15%); 10th gets ~1.15.
            whiff_part = 1.0
            pp = savant.lookup_pitcher_percentiles(opposing_sp_id, season)
            if pp and pp.get("whiff") is not None:
                # percentile → inverse multiplier: 100 → 0.80, 50 → 1.0, 0 → 1.20
                whiff_part = 1.20 - (pp["whiff"] / 100.0) * 0.40
                sp_factor = (era / 4.20) * 0.35 + (whip / 1.30) * 0.20 + k9_part * 0.20 + whiff_part * 0.25
                detail = f"ERA {era:.2f} WHIP {whip:.2f} K/9 {k9:.1f} whiff%-tile {pp['whiff']:.0f}"
            else:
                sp_factor = (era / 4.20) * 0.45 + (whip / 1.30) * 0.25 + k9_part * 0.30
                detail = f"ERA {era:.2f} WHIP {whip:.2f} K/9 {k9:.1f}"
            sp_factor = max(0.6, min(sp_factor, 1.45))
            sp_factor_source = "season"
            notes.append(f"opposing SP adj x{sp_factor:.2f} ({detail})")
        else:
            # v9.15: short-sample fallback. Rookies, post-TJ returnees, openers
            # often have <20 IP — bailing to ×1.00 silently leaks signal we
            # actually have via Savant's descriptive metrics (xERA + xwOBA-
            # against stabilize MUCH faster than ERA on small samples; whiff%
            # percentile is forward-looking K-skill). Use these if available.
            sp_qoc = savant.lookup_pitcher_qoc(opposing_sp_id, season) or None
            sp_expected = savant.lookup_pitcher(opposing_sp_id, season) or None
            pp = savant.lookup_pitcher_percentiles(opposing_sp_id, season)
            xera = _safe_float((sp_expected or {}).get("xera")) if sp_expected else None
            xwoba_a = _safe_float((sp_expected or {}).get("est_woba")) if sp_expected else None
            whiff_part = 1.20 - (pp["whiff"] / 100.0) * 0.40 if pp and pp.get("whiff") is not None else 1.0
            if xera or xwoba_a or (pp and pp.get("whiff") is not None):
                # Tighter clamp than the season branch (±10% vs ±45%) — small-
                # sample data is noisier so don't let it swing the projection.
                xera_part = (xera / 4.20) if xera else 1.0
                xwoba_part = (xwoba_a / 0.320) if xwoba_a else 1.0
                sp_factor = xera_part * 0.40 + xwoba_part * 0.30 + whiff_part * 0.30
                sp_factor = max(0.90, min(sp_factor, 1.10))
                sp_factor_source = "savant_fallback"
                detail_bits = []
                if xera: detail_bits.append(f"xERA {xera:.2f}")
                if xwoba_a: detail_bits.append(f"xwOBA-agst {xwoba_a:.3f}")
                if pp and pp.get("whiff") is not None: detail_bits.append(f"whiff%-tile {pp['whiff']:.0f}")
                detail_bits.append(f"only {int(ip)} IP — Savant fallback")
                notes.append(f"opposing SP adj x{sp_factor:.2f} ({', '.join(detail_bits)})")

    qoc = savant.lookup_batter_qoc(pid, season) or None
    brl = _safe_float((qoc or {}).get("brl_percent"))
    hh = _safe_float((qoc or {}).get("ev95percent"))
    # Statcast-implied true-talent pts/G — blended into the rolling base.
    # Statcast reflects MUCH larger samples than 14 days of game logs, so it
    # acts as a strong prior on true talent. Hot streaks still help (rolling
    # base moves), but a Judge-tier batter on a 3-game cold streak doesn't get
    # punished as hard, and a backup catcher who happened to be on a hot 1-game
    # streak gets dragged toward his Statcast baseline.
    qoc_tier_pre = _qoc_tier_hitter(brl, hh) if (brl or hh) else "—"
    statcast_pg = _statcast_implied_pg_hitter(brl, hh) if (brl or hh) else None
    if statcast_pg is not None:
        # Per-tier adaptive Statcast weight (v9.7 re-tune from 14-day calibration):
        #   HOT/COLD form_tag: 0.15 — let streak signal carry, override anchors
        #   ELITE/POOR qoc tier: 0.25 — 14-day audit (n=804 ELITE) showed
        #     ELITE bias -1.02 still (6σ over-projecting) even after the 0.30
        #     drop. The Statcast prior is OVER-pulling elite-tier hitters
        #     toward true talent; backing off to 0.25 lets the rolling base
        #     carry more weight, closing about half the residual.
        #   SOLID/AVERAGE qoc tier: 0.40 — calibration shows these are
        #     within noise (SOLID -0.40, AVG -0.32 on n≈800 each); keep.
        if form_tag in ("HOT", "COLD"):
            STATCAST_WEIGHT = 0.15
        elif qoc_tier_pre in ("ELITE", "POOR"):
            # v9.10: 0.25 → 0.20 after 8-day audit (n=2389) showed ELITE still
            # over-projecting -0.92 and POOR under-projecting +0.93.
            # v9.14: 0.20 → 0.15 after 11-day audit (n=3357) showed both still
            # off in the same direction (ELITE -0.87, POOR +0.72). Same logic
            # — prior > rolling for ELITE, prior < rolling for POOR, so less
            # prior weight closes both. Keep stepping until residuals tighten.
            STATCAST_WEIGHT = 0.15
        else:
            STATCAST_WEIGHT = 0.40
        # NB (v9.18, REJECTED): tried sample-aware shrinkage — leaning harder on
        # the Statcast prior when the L14 window is thin (games_14 < 10, ~38% of
        # hitters). 5-day A/B replay (n=1355) showed it de-tunes: overall MAE
        # 4.040→4.045, and even on the thin subset it was meant to help, MAE
        # 3.438→3.458 / bias -0.040→-0.080. Thin-sample hitters are mostly
        # low-variance part-timers whose per-game rate is already well-estimated
        # by recent games — the Statcast prior over-pulls them. Fixed tier
        # weights win. Don't re-attempt without re-running the A/B.
        blended_base = (1 - STATCAST_WEIGHT) * base_pg + STATCAST_WEIGHT * statcast_pg
        notes.append(f"Statcast prior {statcast_pg:.2f} pts/G blended (w={STATCAST_WEIGHT}) → {blended_base:.2f}")
        base_pg = blended_base
    # Tiny residual QoC factor for sweet-spot or other secondary signals not
    # in the implied baseline. Kept narrow (±5%) since most signal is now in
    # the blend itself.
    qoc_factor, qoc_notes = _qoc_residual_hitter(qoc)
    if qoc_notes:
        notes.append(f"qoc residual x{qoc_factor:.2f} ({', '.join(qoc_notes)})")

    # Park factor — combine run env (overall offensive-friendliness) with HR
    # factor (which directly affects HR-heavy fantasy scoring). Apply
    # handedness bias to the HR component (e.g., NYY short porch boosts LHB
    # ~+18%, slightly hurts RHB).
    park_factor = 1.0
    park_breakdown = None
    if park:
        run_env = park.get("run_env", 1.0)
        hr_f = park.get("hr_factor", 1.0)
        venue = park.get("venue", "")
        hand_bias = weather_mod.park_hr_handedness(venue, bats)
        hr_f_adj = hr_f * hand_bias
        park_factor = (run_env * 0.65) + (hr_f_adj * 0.35)
        park_factor = max(0.82, min(park_factor, 1.22))
        hand_note = f", {bats}H bias x{hand_bias:.2f}" if hand_bias != 1.0 else ""
        notes.append(f"park {venue} x{park_factor:.2f} (run {run_env:.2f}, HR {hr_f:.2f}{hand_note})")
        # Surface the underlying components so the tooltip can show what's
        # driving even a near-neutral combined factor (e.g. WSH: 1.02 run
        # env + 0.97 weather-suppressed HR cancel to 1.003 — looks 'neutral'
        # but we DID use the data, not 'no signal').
        park_breakdown = {
            "run_env": round(run_env, 3),
            "hr_factor": round(hr_f, 3),     # already weather-blended upstream
            "hand_bias": round(hand_bias, 3),
        }

    # Batting-order PA factor (v9.17 corrected): leadoff hitters get ~4.65
    # PA/game, #9 gets ~3.85. BUT base_pg is points-per-GAME, which already
    # embeds the PAs this hitter typically gets (a career leadoff guy's pts/G
    # already reflects ~4.6 PA). So an ABSOLUTE per-spot multiplier double-
    # counts — it was over-boosting leadoff hitters batting in their normal
    # spot. Corrected: normalize today's expected PA against the player's own
    # season PA/game, so the factor only captures a CHANGE in role (a usual-#7
    # hitter slotted leadoff today gets a real boost; a leadoff regular batting
    # leadoff gets ~1.0). Clamped ±12% for small-sample sanity.
    order_factor = 1.0
    if batting_order and 1 <= batting_order <= 9:
        EXPECTED_PA_BY_SPOT = {1: 4.65, 2: 4.55, 3: 4.45, 4: 4.35, 5: 4.25,
                               6: 4.15, 7: 4.05, 8: 3.95, 9: 3.85}
        today_pa = EXPECTED_PA_BY_SPOT[batting_order]
        szn_pa = _safe_float(seasn.get("plateAppearances"))
        szn_g = _safe_float(seasn.get("gamesPlayed"))
        if szn_pa > 0 and szn_g >= 10:
            season_pa_per_g = szn_pa / szn_g
            order_factor = today_pa / max(season_pa_per_g, 2.5)
            order_factor = max(0.88, min(order_factor, 1.12))
            notes.append(
                f"batting #{batting_order} x{order_factor:.2f} "
                f"(today {today_pa:.2f} PA vs szn {season_pa_per_g:.2f}/G)"
            )
        else:
            # No reliable season PA baseline — fall back to a damped absolute
            # spread (half the old magnitude, since we can't normalize).
            ABS_FALLBACK = {1: 1.05, 2: 1.04, 3: 1.02, 4: 1.01, 5: 1.00,
                            6: 0.99, 7: 0.97, 8: 0.96, 9: 0.94}
            order_factor = ABS_FALLBACK[batting_order]
            notes.append(f"batting #{batting_order} x{order_factor:.2f} (PA adj, no szn baseline)")

    # Matchup adjustment: prefer Vegas implied team total when available — it's
    # the market's full pricing of opposing pitcher quality, lineup, park,
    # weather, umpire, etc. Falls back to SP-only factor when no odds posted.
    vegas_factor = 1.0
    sp_factor_raw = sp_factor   # preserve audit trail for tooltip even when Vegas overrides
    sp_absorbed_by_vegas = False
    if implied_team_total and implied_team_total > 0:
        vegas_factor = (implied_team_total / 4.5) ** 0.55
        vegas_factor = max(0.82, min(vegas_factor, 1.22))
        notes.append(f"Vegas implied {implied_team_total:.1f} R x{vegas_factor:.2f} (matchup signal)")
        sp_factor = 1.0   # Vegas supersedes — don't double-count
        sp_absorbed_by_vegas = True
    # If no Vegas, sp_factor stays as computed above and is the matchup signal.

    # Opposing bullpen factor (whole-staff ERA proxy — pen drives ~35% of innings).
    # ONLY applied to the chain when Vegas is unavailable — Vegas implied
    # total already prices in opposing bullpen quality. But we COMPUTE the
    # raw value either way so the tooltip can show what the bullpen signal
    # looks like (folded into Vegas vs the headline factor).
    bullpen_factor = 1.0
    bullpen_factor_raw = 1.0
    bullpen_absorbed_by_vegas = False
    if opp_bullpen_era and opp_bullpen_era > 0:
        bullpen_factor_raw = (opp_bullpen_era / 4.20) ** 0.30
        bullpen_factor_raw = max(0.93, min(bullpen_factor_raw, 1.08))
        if implied_team_total and implied_team_total > 0:
            bullpen_absorbed_by_vegas = True
        else:
            bullpen_factor = bullpen_factor_raw
            notes.append(f"opp bullpen ERA {opp_bullpen_era:.2f} x{bullpen_factor:.2f} (no Vegas)")

    # Handedness platoon factor (v9.40: personalized). The flat ±5% league
    # assumption ignores that real platoon splits vary 3× across hitters (and
    # some are REVERSED). Now: start from the league prior (±5% / 1.03 switch),
    # blend toward the hitter's OWN season vl/vr OPS ratio, weighted by the
    # relevant split's PA via n/(n+250) shrinkage — true platoon talent
    # stabilizes slowly, so a 70-PA split only moves ~22% of the way off the
    # prior. Personal ratio damped 0.7 (OPS ratio ≈ proportional to pts ratio,
    # kept conservative), result clamped [0.90, 1.10].
    platoon_factor = 1.0
    if bats and opp_throws and bats in ("L", "R", "S") and opp_throws in ("L", "R"):
        if bats == "S":
            static = 1.03   # switch hitters always have platoon advantage
        elif bats != opp_throws:
            static = 1.05   # opposite hand
        else:
            static = 0.95   # same hand
        platoon_factor = static
        detail = "league prior"
        try:
            splits = mlb_api.player_platoon_splits(pid, season)
        except Exception:
            splits = {}
        key = "vl" if opp_throws == "L" else "vr"
        rel, other = splits.get(key), splits.get("vl" if key == "vr" else "vr")
        if rel and other:
            pa_all = rel["pa"] + other["pa"]
            ops_overall = (rel["ops"] * rel["pa"] + other["ops"] * other["pa"]) / pa_all
            if ops_overall > 0.300:
                personal = 1.0 + (rel["ops"] / ops_overall - 1.0) * 0.7
                w = rel["pa"] / (rel["pa"] + 250.0)
                platoon_factor = (1 - w) * static + w * personal
                platoon_factor = max(0.90, min(platoon_factor, 1.10))
                detail = f"own split {rel['ops']:.3f} vs overall {ops_overall:.3f}, {rel['pa']} PA w={w:.2f}"
        notes.append(f"vs {opp_throws}HP ({bats}H) x{platoon_factor:.2f} ({detail})")

    # Arsenal × hitter pitch-type matchup (v9.40). The opposing SP's pitch MIX
    # crossed with THIS hitter's per-pitch-type run values (both from Savant's
    # pitch-arsenal-stats leaderboards). This is player-vs-arsenal signal that
    # team-level Vegas totals don't price: a breaking-ball-vulnerable hitter
    # facing a 60% slider guy, or an elite fastball hunter facing a four-seam
    # starter. Per-pitch-type rv/100 is noisy → shrunk n/(n+150) per type,
    # require ≥60% of the SP's arsenal covered by hitter data, damped 3% per
    # weighted rv100 unit, capped ±5%. Season-cumulative leaderboards (same
    # live-use caveat as every other Savant input); forward-validated like
    # the TB-prop via stored components.
    arsenal_factor = 1.0
    if opposing_sp_id:
        try:
            ars = savant.pitcher_arsenal(season).get(opposing_sp_id)
            brv = savant.batter_pitch_rv(season).get(pid)
        except Exception:
            ars = brv = None
        if ars and brv:
            cov = 0.0
            wsum = 0.0
            for ap in ars:
                pt = ap["pitch_type"]
                u = (ap["usage"] or 0) / 100.0
                d = brv.get(pt)
                if not d or u <= 0:
                    continue
                shrink = d["pitches"] / (d["pitches"] + 150.0)
                wsum += u * d["rv100"] * shrink
                cov += u
            if cov >= 0.60:
                mrv = wsum / cov  # weighted rv/100 vs the covered arsenal share
                arsenal_factor = 1.0 + max(-0.05, min(mrv * 0.03, 0.05))
                if abs(arsenal_factor - 1.0) >= 0.005:
                    notes.append(
                        f"arsenal matchup x{arsenal_factor:.3f} "
                        f"(wtd rv100 {mrv:+.2f} vs SP mix, {cov:.0%} covered)"
                    )

    # Rolling K-rate factor — process-skill shift signal (NOT outcome-based).
    # Why K% specifically: pts/G already encodes outcomes (HR/H/BB weighted), so
    # using OPS or rolling-wOBA would double-count the HOT/COLD signal. K% is
    # the cleanest LUCK-STRIPPED process metric available date-filtered from
    # MLB Stats API: it stabilizes in ~60 PAs (well within 14 days), reflects
    # contact-skill change, and is largely independent of pts/G (where strike-
    # outs only show as a -1 pt penalty per K — not the K rate trajectory).
    #
    # We originally tried rolling xwOBA from Savant /expected_statistics with
    # start_date/end_date — but those params are silently ignored by Savant
    # AND by MLB Stats API's expectedStatistics group. Rolling true xwOBA is
    # not retrievable without aggregating event-level data per player (which
    # the statcast_search endpoint supports but rate-limits aggressively).
    # K%-shift is the strongest reliable proxy: ~0.7 corr with rolling xwOBA
    # for hitters with >40 PAs in the window.
    #
    # Math: contact_rate = 1 - K%. Ratio of rolling vs season contact_rate,
    # damped ^0.45 to keep early-season swings tame, capped ±8%. Min 30 PA
    # in last14 for stability (Caminero/Witt had ~55, typical regular).
    rolling_factor = 1.0
    pa_l14 = _safe_float(last14.get("plateAppearances")) if last14 else 0
    pa_s = _safe_float(seasn.get("plateAppearances")) if seasn else 0
    k_l14 = _safe_float(last14.get("strikeOuts")) if last14 else 0
    k_s = _safe_float(seasn.get("strikeOuts")) if seasn else 0
    rolling_k_pct = (k_l14 / pa_l14) if pa_l14 >= 30 else None
    season_k_pct = (k_s / pa_s) if pa_s >= 50 else None
    if rolling_k_pct is not None and season_k_pct is not None and season_k_pct > 0.05:
        contact_rolling = 1.0 - rolling_k_pct
        contact_season = 1.0 - season_k_pct
        ratio = contact_rolling / contact_season
        rolling_factor = ratio ** 0.45
        rolling_factor = max(0.92, min(rolling_factor, 1.08))
        notes.append(
            f"rolling K% {rolling_k_pct:.1%} vs szn {season_k_pct:.1%} x{rolling_factor:.2f}"
        )

    # ISO form: separate HR-power signal from pts/G. Recent ISO surge or slump
    # captures HR variance that smoothed pts/G under-weights. Capped ±4%.
    iso_factor, iso_note = _hitter_iso_form(last14, seasn)
    if iso_note:
        notes.append(iso_note)

    # SB threat bonus: hitters with established SB pace get a boost vs poor-
    # pickoff pitchers. SBs are 2 DK pts each. Capped +4%.
    sb_factor, sb_note = _hitter_sb_bonus(last14, seasn, opposing_sp_stats)
    if sb_note:
        notes.append(sb_note)

    # Batter TB-prop market factor (v9.39): slate-z-scored devigged P(over)
    # from the batter_total_bases market — see _BAT_TB_PROP at module top.
    # Orthogonal to vegas_factor (team-level) — this is the market's PLAYER-
    # level pricing; damped hard so overlap with park/platoon can't compound.
    tb_prop_factor = 1.0
    if _BAT_TB_PROP and tb_prop and tb_prop.get("z") is not None:
        z = max(-2.5, min(float(tb_prop["z"]), 2.5))
        tb_prop_factor = 1.0 + z * _BAT_TB_PROP_WEIGHT
        notes.append(
            f"TB-prop market x{tb_prop_factor:.3f} (line {tb_prop.get('line')}, "
            f"P(over) {tb_prop.get('p_over', 0):.0%}, slate-z {z:+.1f})"
        )

    chain_product = sp_factor * qoc_factor * park_factor * order_factor * vegas_factor * bullpen_factor * platoon_factor * rolling_factor * iso_factor * sb_factor * tb_prop_factor * arsenal_factor
    proj = base_pg * chain_product
    # Post-matchup HOT/COLD residual correction. Bayesian day-level audit
    # (18 days, n=5,102) revealed two highly-significant biases that the
    # streak override at 0.85 weight COULD NOT close because the Statcast
    # blend afterward (w=0.15 for HOT/COLD) pulls projections back toward
    # season-long true talent:
    #   HOT  posterior bias +1.11 ± 0.35 (P=99.9%)  — under-projecting
    #   COLD posterior bias -1.08 ± 0.10 (P=100%)   — over-projecting
    # Symmetric multipliers close roughly half of each residual without
    # overshooting. If a HOT/COLD player has bias still > 0.7 σ from zero
    # in a future audit, ratchet these further (1.07→1.10 / 0.85→0.80).
    hot_cold_factor = 1.0
    if form_tag == "HOT":
        hot_cold_factor = _HOT_HITTER_BOOST
        proj *= hot_cold_factor
        notes.append(f"HOT post-matchup boost x{_HOT_HITTER_BOOST} (close persistent HOT under-projection)")
    elif form_tag == "COLD":
        # v9.7: tightened from 0.85 to 0.80 after 14-day audit (n=1195)
        # showed COLD still over-projecting by -0.67 (5σ). 0.80 closes
        # roughly half of the remaining residual without overshooting.
        hot_cold_factor = 0.80
        proj *= hot_cold_factor
        notes.append("COLD post-matchup shrink x0.80 (close residual -0.67)")
    elif form_tag == "ELITE":
        # v9.12: 9-day audit (n=23) showed ELITE form_tag (consistent across
        # L3/L7/L14 AND L14 ≥ 9 pts/G — Judge/Acuña/Ohtani-class always-on
        # hitters) under-projected by +4.87. They score 17.2 vs proj 12.4.
        # The chain pulls these toward season mean; their actual ceiling
        # is sticky-high. Small boost since n=23 is modest; revisit after
        # n>100 confirms the magnitude.
        hot_cold_factor = 1.10
        proj *= hot_cold_factor
        notes.append("ELITE form post-matchup boost x1.10 (always-on hitter)")
    elif form_tag == "STEADY":
        # v9.14: STEADY form_tag (consistent L3/L7/L14 AND L14 ≥ 7.5 pts/G but
        # below the ELITE 9.0 cutoff) was under-projected by +2.12 cum.
        # v9.16: ×1.05 barely moved it (+1.98 on n=65, proj 7.56 vs actual
        # 9.55, ratio 1.26). The ELITE ×1.10 landed because it was sized
        # right; STEADY needs more. Bump to ×1.12 to close ~half the gap.
        hot_cold_factor = 1.12
        proj *= hot_cold_factor
        notes.append("STEADY form post-matchup boost x1.12")

    # v9.35: hitter projections are too SPREAD OUT — magnitude audit (n>1000)
    # shows studs (proj 10+) over-projected -1.92 (4.3σ) and scrubs (proj 0-4)
    # under-projected +0.28 (3.1σ), while the middle is dead-on. Overall bias
    # hides it (the two cancel). Compress toward the hitter mean (a pivot
    # transform, the mirror of the v9.29 pitcher de-compression): pulls the
    # studs down and the scrubs up. A/B-tuned.
    if _HIT_COMPRESS_K != 1.0:
        proj = _HIT_COMPRESS_PIVOT + (proj - _HIT_COMPRESS_PIVOT) * _HIT_COMPRESS_K

    # v9.36: recent-form residual shrink. A leak-free GBM-vs-chain backtest
    # (n=3,615 point-in-time player-games, time-split) found the ONE signal a
    # gradient-boosted model could still extract from the chain's own features:
    # it is too FLAT on recent (L3) form. Held-out decomposition of the chain's
    # own error:
    #   COLD form_tag:   bias -0.75 (7.5σ, n=922)  — over-projected even after
    #                    the pre-compression x0.80 above
    #   L3<4 non-COLD:   bias -0.94 (11.2σ, n=1427) — weak-last-3 hitters
    #                    over-projected; most never tagged COLD
    # Applied HERE (post-compression) because that is where the A/B was measured
    # — sizing it pre-compression would ship an unvalidated magnitude. A/B grid
    # (n=3,286): overall MAE 4.234→4.205, overall bias -0.03→+0.07 (<0.7σ),
    # COLD residual -0.75→-0.53. One conservative ratchet; re-audit before more.
    if form_tag == "COLD":
        # v9.38: a fresh OUT-OF-SAMPLE audit (5/31–6/4, n=1,457, dates never in
        # the tuning window) confirmed COLD is STILL over-projected — -0.77
        # (4.5σ, n=288) on top of v9.36's 0.90, and -0.53 in-sample. A signal
        # that replicates across two independent windows at 4.5σ+ is real, not
        # a tuned-on-noise artifact. Tightened 0.90→0.81 (≈0.65 effective with
        # the pre-compression 0.80). Dual-window A/B: closes COLD to ~-0.40
        # (in-sample) / -0.58 (out-of-sample) — both improve, neither overshoots.
        # NB: the broader L3<4 bucket also read -0.69, but that was ENTIRELY the
        # COLD players inside it — non-COLD weak-L3 is +0.004 (perfectly
        # calibrated), so the v9.37 weak-L3 0.88 below is left untouched.
        proj *= 0.81  # on top of the pre-compression x0.80 (≈0.65 effective)
        notes.append("COLD recent-form residual shrink x0.81 (v9.38 OOS audit)")
    elif pg_3 is not None and pg_3 < 4 and games_3 >= 2:
        # v9.37: post-v9.36 re-audit showed this bucket STILL over-projected
        # -0.72 (8.7σ, n=1427) — the 0.92 was one conservative notch on a -0.94
        # signal. It's a LOW-variance bucket (MAE 2.46), so tightening helps
        # MAE monotonically (A/B: 0.92→0.88, overall MAE 4.205→4.200). The
        # symmetric hot-recent boost (l3≥7 under-projected +1.0) was REJECTED:
        # that bucket is high-variance (MAE 5.8), so correcting its mean bias
        # made MAE worse — a bias-fix that hurts accuracy is not a fix.
        proj *= 0.88
        notes.append("weak-L3 residual shrink x0.88 (v9.37 re-audit, 8.7σ)")

    # If MLB has confirmed this hitter is OUT of today's posted lineup,
    # zero out the projection (with a tiny tail in case the API is wrong).
    # Without this, scratched stars showed full projections in the pool —
    # misleading for users browsing rankings, and the "actual=0 vs proj=12"
    # contributed to MAE inflation in calibration when scratches happened.
    if lineup_status == "out":
        proj *= 0.05
        notes.append("MLB lineup OUT — projection zeroed")
    # NB: a COLD post-matchup x0.78 shrink lived here briefly, motivated by
    # 3 days of negative bias on COLD. Removed after a 9-day audit (n=788)
    # showed COLD is actually UNDER-projected by +1.80 on average (7.8σ) —
    # the shrink was making it worse. The recent 3-day window of negative
    # bias was variance, not signal.
    # No floor — strikeouts and GIDPs are negative-scoring events, so a
    # deeply slumping K-prone hitter facing an elite SP genuinely can be a
    # negative-EV play. The streak override above already protects against
    # noise-driven negatives by flooring the L3 input at 0.

    # Confidence interval — DYNAMIC sigma (v9.39). The old flat 5.5 was fiction
    # at both extremes: a 25-date residual study (n=6,372) shows single-game
    # stdev scales nearly linearly with the projection itself —
    #   proj 0-3 → σ 3.4,  5-7 → 6.2,  9-12 → 7.9,  12+ → 9.6
    # Weighted fit: σ ≈ 2.97 + 0.469·proj. Flat 5.5 overstated scrub risk and
    # badly understated stud upside (a 14-pt projection's real ceiling band is
    # ±9.6, not ±5.5). Floor/ceiling = ±1σ; floor clamped at 0.
    sigma = round(max(2.5, min(3.0 + 0.47 * max(proj, 0.0), 11.0)), 1)
    floor = max(0.0, proj - sigma)
    ceiling = proj + sigma
    pitfalls: list[str] = []
    if games_14 < 7:
        pitfalls.append(f"Small sample — only {int(games_14)} G in last 14d")
    if sp_factor < 0.85:
        pitfalls.append("Tough opposing SP (high K%, low ERA)")
    # Compare against LIVE league averages (24h-cached from Statcast leaderboard).
    # Threshold = 2/3 of league avg ≈ "meaningfully below" — softer than p25 but
    # avoids stale-percentile drift through the season.
    lg_brl, lg_hh = LG_BARREL_PCT_HITTER(), LG_HARDHIT_PCT_HITTER()
    if brl and brl < lg_brl * 0.60:
        pitfalls.append(f"Below-avg barrel rate ({brl:.1f}% vs lg {lg_brl:.1f}%)")
    if hh and hh < lg_hh * 0.88:
        pitfalls.append(f"Low hard-hit% ({hh:.0f}% vs lg {lg_hh:.0f}%) — quality of contact lagging")
    qoc_tier = _qoc_tier_hitter(brl, hh)
    return Projection(
        player_id=pid,
        name=name,
        team_id=team_id,
        position=position,
        role="hitter",
        projected_points=round(proj, 2),
        components={
            "base_pg": round(base_pg, 2),
            "sp_factor": round(sp_factor, 3),
            "sp_factor_raw": round(sp_factor_raw, 3),
            "sp_absorbed_by_vegas": sp_absorbed_by_vegas,
            "sp_factor_source": sp_factor_source,  # "season" | "savant_fallback" | None
            "qoc_factor": round(qoc_factor, 3),
            "qoc_tier": qoc_tier,
            "form_tag": form_tag,
            "pg_l3": round(pg_3, 2) if pg_3 is not None else None,
            "pg_l7": round(pg_7, 2) if pg_7 is not None else None,
            "pg_l14": round(pg_14, 2) if pg_14 is not None else None,
            "games_l3": int(games_3),
            "games_l7": int(games_7),
            "games_l14": int(games_14),
            "pitfalls": pitfalls,
            "sample_games_14d": int(games_14),
            "barrel_pct": _safe_float((qoc or {}).get("brl_percent")) or None,
            "hardhit_pct": _safe_float((qoc or {}).get("ev95percent")) or None,
            "sweet_spot_pct": _safe_float((qoc or {}).get("anglesweetspotpercent")) or None,
            "lg_barrel_pct": LG_BARREL_PCT_HITTER(season),
            "lg_hardhit_pct": LG_HARDHIT_PCT_HITTER(season),
            "park_factor": round(park_factor, 3),
            "park_breakdown": park_breakdown,
            "park_venue": (park or {}).get("venue") if park else None,
            "order_factor": round(order_factor, 3),
            "batting_order": batting_order,
            "vegas_factor": round(vegas_factor, 3),
            "implied_team_total": implied_team_total,
            "bullpen_factor": round(bullpen_factor, 3),
            "bullpen_factor_raw": round(bullpen_factor_raw, 3),
            "bullpen_absorbed_by_vegas": bullpen_absorbed_by_vegas,
            "opp_bullpen_era": opp_bullpen_era,
            "platoon_factor": round(platoon_factor, 3),
            "bats": bats,
            "vs_throws": opp_throws,
            "rolling_factor": round(rolling_factor, 3),
            "rolling_k_pct": round(rolling_k_pct, 4) if rolling_k_pct is not None else None,
            "season_k_pct": round(season_k_pct, 4) if season_k_pct is not None else None,
            "rolling_pa_l14": int(pa_l14) if pa_l14 else 0,
            "iso_factor": round(iso_factor, 3),
            "sb_factor": round(sb_factor, 3),
            "arsenal_factor": round(arsenal_factor, 3),
            "tb_prop_factor": round(tb_prop_factor, 3),
            "tb_prop_line": (tb_prop or {}).get("line"),
            "tb_prop_p_over": round((tb_prop or {}).get("p_over"), 3) if (tb_prop or {}).get("p_over") is not None else None,
            "tb_prop_z": round((tb_prop or {}).get("z"), 2) if (tb_prop or {}).get("z") is not None else None,
            "hot_cold_factor": round(hot_cold_factor, 3),
            "chain_product": round(chain_product * hot_cold_factor, 4),
            "floor": round(floor, 2),
            "ceiling": round(ceiling, 2),
            "sigma": sigma,
            "rolling_cats": rolling_cats,
            "point_decomp": _point_decomp_hitter(rolling_events, proj) if rolling_events else None,
            "opp_abbr": opp_abbr,
            "opp_sp_name": opp_sp_name,
            "is_home": is_home,
            "injury": injuries.lookup(name, _TEAM_FULLNAME.get(team_id)),
        },
        notes=notes,
    )


def project_pitcher(
    pid: int,
    name: str,
    *,
    team_id: int | None,
    season: int,
    opponent_team_id: int | None,
    park: dict | None = None,
    opp_implied_total: float | None = None,
    throws: str | None = None,
    opp_abbr: str | None = None,
    is_home: bool | None = None,
    ump_k_factor: float | None = None,
    opp_lineup_avg_pg: float | None = None,
    vegas_k_line: float | None = None,
    catcher_framing_runs: float | None = None,
    as_of: Date | None = None,
) -> Projection:
    last7 = mlb_api.player_stats(pid, group="pitching", season=season, last_n_days=7, as_of=as_of)
    last14 = mlb_api.player_stats(pid, group="pitching", season=season, last_n_days=14, as_of=as_of)
    seasn = mlb_api.player_stats(pid, group="pitching", season=season, as_of=as_of)

    base = LEAGUE_AVG_SP_POINTS_PER_START
    notes: list[str] = []

    starts_7 = _safe_float(last7.get("gamesStarted"))
    starts_14 = _safe_float(last14.get("gamesStarted"))
    starts_season = _safe_float(seasn.get("gamesStarted"))
    ps_l7 = _per_start_pitcher_points(last7) if starts_7 >= 1 else None
    ps_l14 = _per_start_pitcher_points(last14) if starts_14 >= 1 else None
    ps_season = _per_start_pitcher_points(seasn) if starts_season >= 3 else None
    # Per-start category rates for H2H Cat valuation.
    if starts_14 >= 1:
        rolling_cats = _per_start_pitcher_cats(last14)
        rolling_events = _per_start_pitcher_events(last14)
    elif starts_season >= 3:
        rolling_cats = _per_start_pitcher_cats(seasn)
        rolling_events = _per_start_pitcher_events(seasn)
    else:
        rolling_cats = dict(LG_PITCHER_RATES)
        rolling_events = None

    weighted_ps = _weighted_ps_pitcher(
        ps_g_7=(ps_l7, int(starts_7)),
        ps_g_14=(ps_l14, int(starts_14)),
        ps_g_season=(ps_season, int(starts_season)),
    )
    if weighted_ps is not None:
        base = weighted_ps
        notes.append(f"weighted L7/L14/season: {base:.2f} pts/start")
    elif ps_season is not None:
        base = ps_season
        notes.append(f"season fallback: {int(starts_season)} GS, {base:.2f} pts/start")
    else:
        notes.append("no sample, league average")
    form_tag, form_note = _form_tag_pitcher(ps_l7, ps_l14, ps_season, starts_7, starts_14)
    if form_note:
        notes.append(form_note)

    # Opponent quality factor — use opponent team runs/game as a proxy.
    opp_factor = 1.0
    if opponent_team_id:
        try:
            data = mlb_api._get(
                f"/teams/{opponent_team_id}/stats",
                params={"stats": "season", "group": "hitting", "season": season},
            )
            splits = []
            for s in data.get("stats", []):
                splits.extend(s.get("splits", []))
            if splits:
                stat = splits[0].get("stat", {})
                rpg = _safe_float(stat.get("runs")) / max(_safe_float(stat.get("gamesPlayed"), 1), 1)
                # league avg ~ 4.5 R/G; better offense -> tougher matchup -> lower projection
                opp_factor = (4.5 / max(rpg, 2.5)) ** 0.7
                opp_factor = max(0.7, min(opp_factor, 1.35))
                notes.append(f"opponent adj x{opp_factor:.2f} ({rpg:.2f} R/G)")
        except Exception as e:
            logging.warning("opp_factor lookup failed for team %s: %s", opponent_team_id, e)

    qoc = savant.lookup_pitcher_qoc(pid, season) or None
    expected = savant.lookup_pitcher(pid, season) or None
    brl_a = _safe_float((qoc or {}).get("brl_percent")) or None
    hh_a = _safe_float((qoc or {}).get("ev95percent")) or None
    xera = _safe_float((expected or {}).get("xera")) or None
    xwoba_a = _safe_float((expected or {}).get("est_woba")) or None
    # Statcast-implied true talent baseline (xERA + xwOBA-against + brl-allowed).
    # Same logic as hitters: large-sample true-talent prior blends into the
    # rolling base to dampen single-start spikes and protect against streaks.
    statcast_ps = _statcast_implied_ps_pitcher(xera, xwoba_a, brl_a)
    if statcast_ps is not None:
        # Unified Statcast weight for pitchers (same logic as hitters):
        # streaking pitchers need MORE pull toward true-talent xERA/xwOBA, not
        # less, because their rolling form is the volatile signal. Was 0.35/0.15
        # split; now 0.40 across the board — same as hitters.
        STATCAST_WEIGHT = 0.40
        blended_base = (1 - STATCAST_WEIGHT) * base + STATCAST_WEIGHT * statcast_ps
        notes.append(f"Statcast prior {statcast_ps:.2f} pts/start blended (w={STATCAST_WEIGHT}) → {blended_base:.2f}")
        base = blended_base
    qoc_factor, qoc_notes = _qoc_residual_pitcher(qoc, expected)
    if qoc_notes:
        notes.append(f"qoc residual x{qoc_factor:.2f} ({', '.join(qoc_notes)})")

    # Park factor — INVERSE for pitchers (hitter-friendly park hurts pitchers).
    # Smaller magnitude than for hitters since pitchers' fantasy value is more
    # K-driven (less park-dependent) than hit/run-driven.
    park_factor = 1.0
    if park:
        run_env = park.get("run_env", 1.0)
        hr_f = park.get("hr_factor", 1.0)
        park_blend = (run_env * 0.65) + (hr_f * 0.35)
        # Invert and damp: 1.10 hitter-friendly → ~0.95 for pitcher; 0.90 friendly → ~1.05.
        park_factor = 1.0 + (1.0 - park_blend) * 0.5
        park_factor = max(0.90, min(park_factor, 1.10))
        venue = park.get("venue", "")
        notes.append(f"park {venue} x{park_factor:.2f} (env {park_blend:.2f})")

    # Vegas implied total for the OPPONENT — low expected scoring = better for SP.
    # Same logic as hitters: if Vegas exists, it's the comprehensive market
    # signal. Drop opp_factor (opponent runs/game) which is just a noisier proxy.
    vegas_factor = 1.0
    opp_factor_raw = opp_factor   # preserve audit trail for tooltip
    opp_absorbed_by_vegas = False
    if opp_implied_total and opp_implied_total > 0:
        vegas_factor = (4.5 / opp_implied_total) ** 0.55
        vegas_factor = max(0.82, min(vegas_factor, 1.22))
        notes.append(f"opp Vegas {opp_implied_total:.1f} R x{vegas_factor:.2f} (matchup signal)")
        opp_factor = 1.0   # Vegas supersedes
        opp_absorbed_by_vegas = True

    # Rolling K-rate factor — pitcher's K% shift over last 14 days vs season.
    # Higher rolling K% = pitcher dealing → boost. Lower = slipping → shrink.
    # Same rationale as hitter rolling_factor: pts/start outcomes already feed
    # rolling form; K% adds the process-skill trajectory that pts/start can't
    # see (a pitcher can have ~same ERA but K rate could be climbing fast).
    # See hitter project_hitter for the full data-source audit (rolling xwOBA
    # is unavailable from any reliable endpoint; K% via byDateRange is the
    # strongest reliable proxy).
    #
    # Math: ratio = rolling_K_pct / season_K_pct (not inverted — high K% is
    # GOOD for pitcher). Damped ^0.45, capped ±8%. Min 30 BF in last14 for
    # stability (≈1 typical start of K-rate sample).
    rolling_factor = 1.0
    bf_l14 = _safe_float(last14.get("battersFaced")) if last14 else 0
    bf_s = _safe_float(seasn.get("battersFaced")) if seasn else 0
    k_l14 = _safe_float(last14.get("strikeOuts")) if last14 else 0
    k_s = _safe_float(seasn.get("strikeOuts")) if seasn else 0
    rolling_k_pct = (k_l14 / bf_l14) if bf_l14 >= 30 else None
    season_k_pct = (k_s / bf_s) if bf_s >= 50 else None
    if rolling_k_pct is not None and season_k_pct is not None and season_k_pct > 0.05:
        ratio = rolling_k_pct / season_k_pct
        rolling_factor = ratio ** 0.45
        rolling_factor = max(0.92, min(rolling_factor, 1.08))
        notes.append(
            f"rolling K% {rolling_k_pct:.1%} vs szn {season_k_pct:.1%} x{rolling_factor:.2f}"
        )

    # Home plate umpire factor — wider strike zone (positive 'favor' on
    # UmpScorecards) inflates K rate and lowers walks. k_factor is computed
    # upstream as 1.0 + favor/50 (clamped for sanity here). Damped to ~70%
    # of raw effect because ump_k_factor is K-rate-only and the SP fantasy
    # score is K-heavy but not exclusive.
    ump_factor = 1.0
    if ump_k_factor and 0.5 < ump_k_factor < 1.5:
        ump_factor = 1.0 + (ump_k_factor - 1.0) * 0.7
        ump_factor = max(0.92, min(ump_factor, 1.10))
        notes.append(f"HP ump x{ump_factor:.2f} (k_factor {ump_k_factor:.2f})")

    # TTO penalty (v9.3): pitchers who routinely go 5.5+ IP get hit harder on
    # the 3rd turn through the lineup. ~30-point wOBA jump documented for TTO3.
    # Penalty scales with avg IP/start. Bypassed for openers (their rolling
    # base already reflects short outings).
    ip_total_l14 = _safe_float(last14.get("inningsPitched"))
    tto_factor, tto_note = _pitcher_tto_factor(ip_total_l14, int(starts_14))
    if tto_note:
        notes.append(tto_note)

    # Opener detection (v9.3): pitchers averaging <2.5 IP/start are openers
    # and project very differently from real starters. Flag for UI clarity;
    # the rolling pts/start already reflects their actual role.
    is_opener, opener_note = _pitcher_opener_check(ip_total_l14, int(starts_14))
    if opener_note:
        notes.append(opener_note)

    # Team defense factor (v9.3): better team fielding → fewer BABIP hits →
    # higher pitcher projection. Coarse proxy via team fielding pct (DRS/OAA
    # not in MLB Stats API). Capped ±3%.
    defense_factor, defense_note = _team_defense_factor(team_id, season)
    if defense_note:
        notes.append(defense_note)

    # Opposing lineup quality — today's POSTED lineup avg pts/G vs league avg.
    # Captures rest-day / B-squad surprises that haven't been priced into Vegas
    # yet. The biggest overlap risk in the projection chain is with vegas_factor:
    # sharp books reprice within minutes of lineup posts. To avoid stacking on
    # top of Vegas's own adjustment, this factor is HEAVILY damped (^0.18,
    # clamped 0.94–1.07 → max ~±6%). If Vegas already captured the bulk of
    # the lineup effect, our additive contribution stays in noise range; if
    # Vegas hasn't moved, this contributes a meaningful but bounded signal.
    lineup_factor = 1.0
    lineup_factor_raw = 1.0
    lineup_absorbed_by_vegas = False
    if opp_lineup_avg_pg and opp_lineup_avg_pg > 0:
        ratio = LEAGUE_AVG_HITTER_POINTS_PER_GAME / opp_lineup_avg_pg
        lineup_factor_raw = ratio ** 0.18
        lineup_factor_raw = max(0.94, min(lineup_factor_raw, 1.07))
        # v9.15.1: Vegas implied total already prices in opposing lineup
        # quality. Letting lineup_factor multiply on top of vegas_factor was
        # a 1-5% double-count for pitchers facing strong-offense teams.
        # Suppress to 1.0 in the chain when Vegas is set; raw stays visible
        # in the tooltip so the user can audit what we'd have applied.
        if opp_implied_total and opp_implied_total > 0:
            lineup_absorbed_by_vegas = True
        else:
            lineup_factor = lineup_factor_raw
            notes.append(f"opp lineup x{lineup_factor:.2f} (posted {opp_lineup_avg_pg:.2f} vs lg {LEAGUE_AVG_HITTER_POINTS_PER_GAME:.2f})")

    # Catcher framing factor (v9.8): elite framing catchers steal extra
    # strikes for their pitcher, generating ~0.3-0.5 extra K per start.
    # Anti-framers cost the same. Convert season rv_tot (run value from
    # framing, typical ±10 range) to a small multiplier capped at ±3%.
    # Signal is only meaningful when lineup is posted AND the catcher
    # has a season sample on Savant.
    framing_factor = 1.0
    if catcher_framing_runs is not None and catcher_framing_runs != 0:
        # +5 rv → +2.5% K boost; clamped ±3% to keep this conservative
        # (catcher framing is real but a small component of total pitcher score).
        framing_factor = 1.0 + max(-0.03, min(catcher_framing_runs * 0.005, 0.03))
        notes.append(f"catcher framing x{framing_factor:.3f} (rv_tot {catcher_framing_runs:+.1f})")

    chain_product = opp_factor * qoc_factor * park_factor * vegas_factor * rolling_factor * ump_factor * lineup_factor * tto_factor * defense_factor * framing_factor
    proj = base * chain_product

    # Vegas K-prop adjustment (v9.5): pitcher_strikeouts market lines are the
    # sharpest single signal for the biggest fantasy event a pitcher has —
    # multiple US books + live betting flow. We don't replace the projection
    # (that would over-weight one market), we blend a damped delta: convert
    # the gap between Vegas K-line and our rolling-stats-implied Ks into pts
    # and apply at half weight, capped ±3 pts.
    k_prop_adj = 0.0
    if vegas_k_line is not None and vegas_k_line > 0:
        k9_now = _safe_float(seasn.get("strikeoutsPer9Inn"))
        ip_avg = ip_total_l14 / max(int(starts_14), 1) if starts_14 else 5.5
        expected_K = (k9_now * ip_avg / 9.0) if k9_now > 0 else (5.5 * 1.0)
        if expected_K > 0:
            delta_K = vegas_k_line - expected_K
            # 1.5 pts/K, damped to 0.5 so we don't overcommit to one market
            k_prop_adj = max(-3.0, min(delta_K * 1.5 * 0.5, 3.0))
            proj += k_prop_adj
            notes.append(
                f"K-prop adj {k_prop_adj:+.2f} pts (Vegas {vegas_k_line:.1f} K vs "
                f"rolling-implied {expected_K:.1f})"
            )

    # Post-matchup HOT/COLD residual correction (v9.10). Mirror of the hitter
    # rule shipped in v9.7. 8-day audit (n=43 cold pitchers) showed a -5.9
    # bias — projecting ~17, scoring ~11. The streak signal already feeds the
    # base via rolling form, but the Statcast/QoC chain still anchors these
    # toward true talent and the factor pile inflates the result. Applying a
    # multiplicative shrink/boost AFTER the chain closes ~half of the gap
    # without overshooting:
    #   COLD pitcher bias -5.9 (n=43, 6.2σ from zero) → x0.80
    #   HOT pitcher  bias modest (small n, ~+1 if any) → x1.05 (lighter than
    #     hitter HOT because pitcher form swings are noisier per start).
    hot_cold_factor = 1.0
    if form_tag == "COLD":
        # Progressive tightening: v9.10 ×0.80 → v9.12 ×0.70 → v9.14 ×0.65.
        # 15-day audit (n=80) still shows -3.96 cum / -4.65 recent —
        # projecting ~5.2, scoring ~1.1. Each multiplicative step closes
        # less because the chain below the multiplier dominates, but the
        # direction is unambiguous and the sample is solid. v9.16 → ×0.55.
        hot_cold_factor = _COLD_PITCHER_SHRINK
        proj *= hot_cold_factor
        notes.append(f"COLD post-matchup shrink x{_COLD_PITCHER_SHRINK} (recurring over-projection)")
    elif form_tag == "HOT":
        hot_cold_factor = 1.05
        proj *= hot_cold_factor
        notes.append("HOT post-matchup boost x1.05")
    elif form_tag == "ELITE":
        # Same rationale as hitter ELITE — always-on pitchers (consistent
        # L7/L14/season AND ps_l14 ≥ 18 pts/start) under-projected. Small
        # boost since pitcher single-start variance is high and n is tiny.
        hot_cold_factor = 1.07
        proj *= hot_cold_factor
        notes.append("ELITE form post-matchup boost x1.07 (always-on pitcher)")

    # v9.20: AVERAGE/POOR-QoC startable pitchers were under-projected. 6-day
    # audit (n=157): AVERAGE-QoC +1.99 (2.7σ, proj 10.6 → act 12.6), POOR
    # +1.15; ELITE/SOLID dead-on (≈0). Their mediocre xERA/barrel anchors trim
    # the matchup chain a touch too hard for mid/back-end starters who beat
    # their underlying. Tier-targeted lift; skip COLD (already shrunk above —
    # it's over-projected, not under) and leave the calibrated ELITE/SOLID
    # tiers alone. Gated for A/B replay before shipping.
    qoc_tier = _qoc_tier_pitcher(brl_a or 0, xera or 0)
    if _PIT_QOC_LIFT and form_tag != "COLD" and qoc_tier in ("AVERAGE", "POOR"):
        qoc_tier_lift = 1.06 if qoc_tier == "AVERAGE" else 1.04
        proj *= qoc_tier_lift
        notes.append(f"{qoc_tier}-QoC pitcher lift x{qoc_tier_lift} (v9.20 audit)")
    # v9.34: the mirror — ELITE/SOLID-QoC (good-stuff) pitchers are now OVER-
    # projected (6-day incl Sun: SOLID -1.53 / 4.5σ, ELITE -0.67 / 2.3σ). Their
    # elite xERA/barrel anchors run the chain a touch hot. Tier-targeted trim;
    # skip COLD (already shrunk). A/B-gated.
    elif _PIT_QOC_TRIM and form_tag != "COLD" and qoc_tier in ("ELITE", "SOLID"):
        qoc_tier_trim = _PIT_QOC_TRIM_SOLID if qoc_tier == "SOLID" else _PIT_QOC_TRIM_ELITE
        proj *= qoc_tier_trim
        notes.append(f"{qoc_tier}-QoC pitcher trim x{qoc_tier_trim} (v9.34 audit)")

    # v9.29: pitcher projections were COMPRESSED — over-shrunk toward the
    # league-average prior. 6-day audit (n=172): proj<8 over-projected −2.77
    # (3.0σ, bad starts crater worse), proj 8-13 under +2.39 (2.8σ). De-compress
    # around a pivot: a post-hoc A/B confirmed pivot 9 / k 1.25 cuts overall
    # MAE 6.44→6.24 and halves every bucket bias. Floor at 1.0 (a projection
    # shouldn't go negative even though a real bad start can).
    proj = max(1.0, _PIT_SPREAD_PIVOT + (proj - _PIT_SPREAD_PIVOT) * _PIT_SPREAD_K)

    # v9.39: SECOND de-compression notch. 25-date diagnostic (n=662) found the
    # optimal-linear-recal slope is still 1.11 — measured independently on each
    # time half it came out 1.111 / 1.106, remarkably stable — i.e. even after
    # the v9.29 k=1.25 spread, pitcher projections remain ~11% too compressed.
    # Grid A/B: pivot 11.5 (≈ sample mean, so the transform is BIAS-NEUTRAL —
    # lower pivots improved MAE but pushed late-window bias negative) with
    # k=1.12 improves MAE on BOTH halves (early 6.043→6.034, late 5.807→5.766)
    # with bias unchanged. Kept separate from the v9.29 line because this is
    # where the A/B was measured (on the final chain output).
    proj = max(1.0, 11.5 + (proj - 11.5) * 1.12)

    # Opener clamp: if this pitcher is averaging <2.5 IP/start, their fantasy
    # ceiling is structurally capped (3 IP max → ~8 pts max even with K-heavy
    # outing). Project no higher than 9 pts even if rolling form says more.
    if is_opener and proj > 9.0:
        notes.append(f"opener clamp: capping projection at 9.0 (was {proj:.1f})")
        proj = 9.0

    # Pitcher single-start stdev — DYNAMIC (v9.39). Same 25-date residual
    # study (n=662): σ scales with the projection (proj 0-6 → 6.4, 12-15 →
    # 7.7, 15+ → 8.4; weighted fit σ ≈ 5.94 + 0.126·proj). Flatter slope than
    # hitters — a bad start craters anyone — but aces still carry wider bands.
    sigma = round(max(5.5, min(5.9 + 0.13 * max(proj, 0.0), 9.5)), 1)
    floor = max(-5.0, proj - sigma)   # pitchers can score negative on bad starts
    ceiling = proj + sigma
    pitfalls: list[str] = []
    # SPs typically start every ~5 days, so 2-3 GS in 14d is the norm. Only
    # flag truly tiny samples (1 or 0 starts) — that's where projection noise
    # actually dominates.
    if starts_14 < 2 and _safe_float(seasn.get("gamesStarted")) < 4:
        pitfalls.append(f"Tiny sample — {int(starts_14)} 14d GS, {int(_safe_float(seasn.get('gamesStarted')))} season")
    if opp_factor > 1.15:
        pitfalls.append("Hot offensive opponent (high R/G)")
    lg_brl_allowed = LG_BARREL_PCT_ALLOWED()
    if brl_a and brl_a > lg_brl_allowed * 1.25:   # >25% above league avg = vulnerable
        pitfalls.append(f"Vulnerable to hard contact (brl-allowed {brl_a:.1f}% vs lg {lg_brl_allowed:.1f}%)")
    if xera and xera > 4.75:
        pitfalls.append(f"Underlying xERA {xera:.2f} — luck-adjusted line is rough")
    return Projection(
        player_id=pid,
        name=name,
        team_id=team_id,
        position="SP",
        role="pitcher",
        projected_points=round(proj, 2),
        components={
            "base_per_start": round(base, 2),
            "opp_factor": round(opp_factor, 3),
            "opp_factor_raw": round(opp_factor_raw, 3),
            "opp_absorbed_by_vegas": opp_absorbed_by_vegas,
            "qoc_factor": round(qoc_factor, 3),
            "qoc_tier": qoc_tier,
            "form_tag": form_tag,
            "ps_l7": round(ps_l7, 2) if ps_l7 is not None else None,
            "ps_l14": round(ps_l14, 2) if ps_l14 is not None else None,
            "ps_season": round(ps_season, 2) if ps_season is not None else None,
            "starts_l7": int(starts_7),
            "starts_l14": int(starts_14),
            "starts_season": int(starts_season),
            "pitfalls": pitfalls,
            "sample_starts_14d": int(starts_14),
            "xera": xera,
            "xwoba_against": xwoba_a,
            "barrel_pct_allowed": brl_a,
            "hardhit_pct_allowed": hh_a,
            "lg_barrel_pct_allowed": LG_BARREL_PCT_ALLOWED(season),
            "lg_hardhit_pct_allowed": LG_HARDHIT_PCT_ALLOWED(season),
            "park_factor": round(park_factor, 3),
            "park_venue": (park or {}).get("venue") if park else None,
            "vegas_factor": round(vegas_factor, 3),
            "opp_implied_total": opp_implied_total,
            "throws": throws,
            "rolling_factor": round(rolling_factor, 3),
            "rolling_k_pct": round(rolling_k_pct, 4) if rolling_k_pct is not None else None,
            "season_k_pct": round(season_k_pct, 4) if season_k_pct is not None else None,
            "rolling_bf_l14": int(bf_l14) if bf_l14 else 0,
            "tto_factor": round(tto_factor, 3),
            "defense_factor": round(defense_factor, 3),
            "framing_factor": round(framing_factor, 3),
            "catcher_framing_rv": catcher_framing_runs,
            "ump_factor": round(ump_factor, 3),
            "lineup_factor": round(lineup_factor, 3),
            "lineup_factor_raw": round(lineup_factor_raw, 3),
            "lineup_absorbed_by_vegas": lineup_absorbed_by_vegas,
            "hot_cold_factor": round(hot_cold_factor, 3),
            "chain_product": round(chain_product * hot_cold_factor, 4),
            "ip_per_start": round(ip_total_l14 / max(int(starts_14), 1), 2) if starts_14 else None,
            "is_opener": is_opener,
            "k9_season": round(_safe_float(seasn.get("strikeoutsPer9Inn")), 1) or None,
            "vegas_k_line": vegas_k_line,
            "k_prop_adj": round(k_prop_adj, 2) if vegas_k_line else None,
            "floor": round(floor, 2),
            "ceiling": round(ceiling, 2),
            "sigma": sigma,
            "rolling_cats": rolling_cats,
            "point_decomp": _point_decomp_pitcher(rolling_events, proj) if rolling_events else None,
            "opp_abbr": opp_abbr,
            "is_home": is_home,
            "injury": injuries.lookup(name, _TEAM_FULLNAME.get(team_id)),
        },
        notes=notes,
    )


def _weighted_pg_hitter(*, pts_g_3, pts_g_7, pts_g_14, pts_g_season=(None, 0)):
    """Sample-size × recency weighting on non-overlapping buckets, with a
    season-as-prior bucket so small recent samples don't blow up.

    The MLB API returns cumulative stats per window — L7 *includes* L3, L14
    *includes* L7, season *includes* L14. We back out four disjoint buckets
    by subtraction (recent / mid / old / season-prior), then weight each by
    (games × recency_multiplier). The season prior has low recency weight
    but high game count for a regular — for a backup catcher with 1 hot L3
    game and 30 season games of mediocrity, the season bucket dominates and
    pulls the projection back to reality.
    """
    pg3, g3 = pts_g_3
    pg7, g7 = pts_g_7
    pg14, g14 = pts_g_14
    pgs, gs = pts_g_season
    # Each bucket: (pts_per_g, games, recency_boost). Recency is scaled by
    # min(1, games/3) so a 1-game sample only gets a third of its recency
    # bonus — that's what stops backup catchers with 1 hot game from blowing up.
    def _add(buckets, pg, games, recency):
        if pg is None or games <= 0:
            return
        scaled = recency * min(1.0, games / 3.0)
        buckets.append((pg, games, scaled))

    buckets: list[tuple[float, int, float]] = []
    # Recency weights, post-17-day audit (n=4,949):
    # The earlier 5.0 boost on L3 was making L3 ~40% of base_pg from a 12-PA
    # sample. That over-shoot was the root of the persistent HOT/COLD bias —
    # streaks regress toward true talent harder than a 3-game window implies.
    # Lowered to 2.5 so L3 sits closer to ~22% of base; the weighted mix is
    # now: L3 ~22%, L7 ~22%, L14 ~28%, season ~28%. Balanced rather than
    # recency-dominant.
    _add(buckets, pg3, g3, 2.5)
    if pg7 is not None and g7 > 0:
        if pg3 is not None and g3 > 0 and g7 > g3:
            _add(buckets, (pg7 * g7 - pg3 * g3) / (g7 - g3), g7 - g3, 2.2)
        elif pg3 is None:
            _add(buckets, pg7, g7, 2.2)
    if pg14 is not None and g14 > 0:
        if pg7 is not None and g7 > 0 and g14 > g7:
            _add(buckets, (pg14 * g14 - pg7 * g7) / (g14 - g7), g14 - g7, 1.5)
        elif pg7 is None:
            _add(buckets, pg14, g14, 1.5)
    if pgs is not None and gs > 0:
        if pg14 is not None and g14 > 0 and gs > g14:
            prior_g = min(gs - g14, 14)   # cap so season doesn't drown recency
            _add(buckets, (pgs * gs - pg14 * g14) / (gs - g14), prior_g, 0.7)
        elif pg14 is None:
            _add(buckets, pgs, min(gs, 14), 0.7)

    # Dynamic league-average ghost prior. Only fills the gap when real sample
    # weight is small — well-sampled regulars are unaffected.
    real_w = sum(g * r for _, g, r in buckets)
    GHOST_TARGET = 5.0
    if real_w < GHOST_TARGET:
        ghost_w = GHOST_TARGET - real_w
        buckets.append((LEAGUE_AVG_HITTER_POINTS_PER_GAME, 1, ghost_w))

    if not buckets:
        return None
    total_w = sum(g * r for _, g, r in buckets)
    if total_w <= 0:
        return None
    return sum(pg * g * r for pg, g, r in buckets) / total_w


def _weighted_ps_pitcher(*, ps_g_7, ps_g_14, ps_g_season):
    """Pitcher version: starts are sparse so windows are L7 / L14 / season."""
    ps7, s7 = ps_g_7
    ps14, s14 = ps_g_14
    pss, ss = ps_g_season
    # Pitchers start every ~5d, so a single-start sample is even noisier than
    # a single-game hitter sample. Cap recency by min(1, starts/2).
    def _add(buckets, ps, starts, recency):
        if ps is None or starts <= 0:
            return
        scaled = recency * min(1.0, starts / 2.0)
        buckets.append((ps, starts, scaled))

    buckets: list[tuple[float, int, float]] = []
    _add(buckets, ps7, s7, 2.0)
    if ps14 is not None and s14 > 0:
        if ps7 is not None and s7 > 0 and s14 > s7:
            _add(buckets, (ps14 * s14 - ps7 * s7) / (s14 - s7), s14 - s7, 1.3)
        elif ps7 is None:
            _add(buckets, ps14, s14, 1.3)
    if pss is not None and ss > 0:
        if ps14 is not None and s14 > 0 and ss > s14:
            _add(buckets, (pss * ss - ps14 * s14) / (ss - s14), ss - s14, 0.7)
        elif ps14 is None:
            _add(buckets, pss, ss, 0.7)

    # Dynamic ghost prior — fills in only when real start weight is thin.
    real_w = sum(s * r for _, s, r in buckets)
    GHOST_TARGET = 4.0
    if real_w < GHOST_TARGET:
        ghost_w = GHOST_TARGET - real_w
        buckets.append((LEAGUE_AVG_SP_POINTS_PER_START, 1, ghost_w))

    if not buckets:
        return None
    total_w = sum(s * r for _, s, r in buckets)
    if total_w <= 0:
        return None
    return sum(ps * s * r for ps, s, r in buckets) / total_w


def _form_tag_hitter(pg_3, pg_7, pg_14, g3, g14):
    """Returns (tag, short_note). Tags: HOT / COLD / STEADY / ELITE / "" """
    if pg_14 is None or g14 < 5:
        return "", ""
    if pg_3 is not None and g3 >= 2:
        if pg_3 >= 1.30 * pg_14 and pg_3 >= 8.0:
            return "HOT", f"hot — L3 {pg_3:.1f} vs L14 {pg_14:.1f} pts/G"
        if pg_3 <= 0.65 * pg_14 and pg_3 <= 4.5:
            return "COLD", f"cold — L3 {pg_3:.1f} vs L14 {pg_14:.1f} pts/G"
    # STEADY: all three windows close to each other AND solidly above league avg
    if (
        pg_7 is not None and pg_3 is not None and pg_14 >= 7.5
        and abs(pg_3 - pg_14) / pg_14 <= 0.20
        and abs(pg_7 - pg_14) / pg_14 <= 0.15
    ):
        if pg_14 >= 9.0:
            return "ELITE", f"always-on — L3/L7/L14 all ~{pg_14:.1f} pts/G"
        return "STEADY", f"steady — L3/L7/L14 all ~{pg_14:.1f} pts/G"
    return "", ""


def _form_tag_pitcher(ps_7, ps_14, ps_season, s7, s14):
    """Pitchers start every ~5d so L3 isn't useful; compare L7/L14/season."""
    if ps_14 is None or s14 < 1:
        return "", ""
    if ps_7 is not None and s7 >= 1:
        if ps_7 >= 1.25 * ps_14 and ps_7 >= 14:
            return "HOT", f"hot — last start {ps_7:.1f} vs L14 {ps_14:.1f}"
        if ps_7 <= 0.70 * ps_14 and ps_7 <= 8:
            return "COLD", f"cold — last start {ps_7:.1f} vs L14 {ps_14:.1f}"
    if ps_season is not None and ps_14 >= 14 and abs(ps_14 - ps_season) / max(ps_season, 1) <= 0.15:
        if ps_14 >= 18:
            return "ELITE", f"always-on — L14 {ps_14:.1f} ~ season {ps_season:.1f}"
        return "STEADY", f"steady — L14 {ps_14:.1f} ~ season {ps_season:.1f}"
    return "", ""


def _per_game_hitter_points(stats: dict) -> float:
    g = max(_safe_float(stats.get("gamesPlayed")), 1.0)
    h = _safe_float(stats.get("hits"))
    d = _safe_float(stats.get("doubles"))
    t = _safe_float(stats.get("triples"))
    hr = _safe_float(stats.get("homeRuns"))
    singles = max(h - d - t - hr, 0)
    p = HITTER_POINTS
    pts = (
        singles * p["single"]
        + d * p["double"]
        + t * p["triple"]
        + hr * p["homeRun"]
        + _safe_float(stats.get("runs")) * p["run"]
        + _safe_float(stats.get("rbi")) * p["rbi"]
        + _safe_float(stats.get("baseOnBalls")) * p["baseOnBalls"]
        + _safe_float(stats.get("hitByPitch")) * p["hitByPitch"]
        + _safe_float(stats.get("stolenBases")) * p["stolenBase"]
        + _safe_float(stats.get("groundIntoDoublePlay")) * p["groundIntoDoublePlay"]
        + _safe_float(stats.get("strikeOuts")) * p["strikeOut"]
    )
    return pts / g


def _per_game_hitter_events(stats: dict) -> dict:
    """Per-game expected event counts (the raw stat line behind the points).
    Mirrors the events HITTER_POINTS scores, so summing n×pts reproduces
    _per_game_hitter_points exactly."""
    g = max(_safe_float(stats.get("gamesPlayed")), 1.0)
    h = _safe_float(stats.get("hits"))
    d = _safe_float(stats.get("doubles"))
    t = _safe_float(stats.get("triples"))
    hr = _safe_float(stats.get("homeRuns"))
    singles = max(h - d - t - hr, 0)
    return {
        "1B": singles / g,
        "2B": d / g,
        "3B": t / g,
        "HR": hr / g,
        "R": _safe_float(stats.get("runs")) / g,
        "RBI": _safe_float(stats.get("rbi")) / g,
        "BB": _safe_float(stats.get("baseOnBalls")) / g,
        "HBP": _safe_float(stats.get("hitByPitch")) / g,
        "SB": _safe_float(stats.get("stolenBases")) / g,
        "GIDP": _safe_float(stats.get("groundIntoDoublePlay")) / g,
        "K": _safe_float(stats.get("strikeOuts")) / g,
    }


# Map each decomposition event to the scoring weight + a friendly label, in
# the order we want to display them (positive events first, drags last).
_HITTER_DECOMP_KEYS = [
    ("1B", "single", "Singles"),
    ("2B", "double", "Doubles"),
    ("3B", "triple", "Triples"),
    ("HR", "homeRun", "Home runs"),
    ("R", "run", "Runs"),
    ("RBI", "rbi", "RBI"),
    ("BB", "baseOnBalls", "Walks"),
    ("HBP", "hitByPitch", "HBP"),
    ("SB", "stolenBase", "Stolen bases"),
    ("K", "strikeOut", "Strikeouts"),
    ("GIDP", "groundIntoDoublePlay", "GIDP"),
]


def _point_decomp_hitter(events: dict, proj_points: float) -> dict | None:
    """Turn a per-game event line into the "what makes up this number" view:
    each event × its scoring weight, scaled uniformly so the contributions
    sum to the actual projection (the projection is top-down pts/G × factors,
    so we scale the representative line to match). Returns None if the raw
    line is empty/degenerate."""
    raw_pts = sum(events.get(ek, 0.0) * HITTER_POINTS[sk] for ek, sk, _ in _HITTER_DECOMP_KEYS)
    if raw_pts <= 0.5:   # degenerate (league fallback, or net-negative line)
        return None
    scale = proj_points / raw_pts
    lines = []
    for ek, sk, label in _HITTER_DECOMP_KEYS:
        n = events.get(ek, 0.0) * scale
        if abs(n) < 0.005:
            continue
        w = HITTER_POINTS[sk]
        lines.append({"label": label, "key": ek, "n": round(n, 2),
                      "pts_each": w, "pts": round(n * w, 2)})
    return {"raw_pts": round(raw_pts, 2), "scale": round(scale, 3),
            "lines": lines, "total": round(proj_points, 2)}


# --- H2H Categories support ---------------------------------------------
# League-average per-game (hitter) and per-start (SP) baselines for the cats:
#   Hitters: R, HR, RBI, SB, OPS
#   Pitchers (SP): QS, K, ERA*, WHIP*, SVH (* lower=better)
LG_HITTER_RATES = {
    "R":   0.56,    # runs per game played
    "HR":  0.14,
    "RBI": 0.55,
    "SB":  0.06,
    "OPS": 0.730,
}
# Approx stdev of player-season per-game rates (used for z-scoring).
LG_HITTER_STDEV = {"R": 0.18, "HR": 0.07, "RBI": 0.20, "SB": 0.10, "OPS": 0.080}

LG_PITCHER_RATES = {
    "QS":  0.45,    # probability of QS per start
    "K":   5.5,     # K per start
    "ERA": 4.20,
    "WHIP": 1.30,
    "SVH": 0.0,     # starters don't get holds; this category is reliever-only
}
LG_PITCHER_STDEV = {"QS": 0.20, "K": 1.5, "ERA": 0.80, "WHIP": 0.13, "SVH": 0.30}


def _per_game_hitter_cats(stats: dict) -> dict:
    """Returns per-game R/HR/RBI/SB and OPS from a stats dict (league window)."""
    g = max(_safe_float(stats.get("gamesPlayed")), 1.0)
    h = _safe_float(stats.get("hits"))
    d = _safe_float(stats.get("doubles"))
    t = _safe_float(stats.get("triples"))
    hr = _safe_float(stats.get("homeRuns"))
    bb = _safe_float(stats.get("baseOnBalls"))
    hbp = _safe_float(stats.get("hitByPitch"))
    sf = _safe_float(stats.get("sacFlies"))
    ab = _safe_float(stats.get("atBats"))
    tb = _safe_float(stats.get("totalBases"))
    pa = ab + bb + hbp + sf
    obp = (h + bb + hbp) / pa if pa > 0 else 0.0
    slg = tb / ab if ab > 0 else 0.0
    return {
        "R":   _safe_float(stats.get("runs")) / g,
        "HR":  hr / g,
        "RBI": _safe_float(stats.get("rbi")) / g,
        "SB":  _safe_float(stats.get("stolenBases")) / g,
        "OPS": obp + slg,
    }


def _per_start_pitcher_cats(stats: dict) -> dict:
    """Returns per-start K, QS-prob, ERA, WHIP from a pitching stats dict.

    QS-prob: when raw `qualityStarts` is in the response (season splits) we
    use it directly. byDateRange responses often omit it, so we estimate from
    IP/start + ERA: P(QS) ≈ 0.5 + 0.10*(IP/start - 5.5) - 0.10*(ERA - 4.0),
    clamped to [0.05, 0.75]."""
    gs = max(_safe_float(stats.get("gamesStarted")), 1.0)
    qs_raw = _safe_float(stats.get("qualityStarts"))
    ip = _safe_float(stats.get("inningsPitched"))
    era = _safe_float(stats.get("era"), default=4.20)
    if qs_raw > 0:
        qs_rate = qs_raw / gs
    else:
        ip_per_start = ip / gs if gs else 5.0
        qs_rate = max(0.05, min(0.75,
            0.50 + 0.10 * (ip_per_start - 5.5) - 0.10 * (era - 4.0)
        ))
    return {
        "QS":   qs_rate,
        "K":    _safe_float(stats.get("strikeOuts")) / gs,
        "ERA":  era,
        "WHIP": _safe_float(stats.get("whip"), default=1.30),
        "SVH":  0.0,
    }


# Typical weekly category swings — used to gauge leverage. A close matchup in a
# given cat means each marginal contribution matters more.
TYPICAL_WEEKLY_SWING = {
    "R": 22, "HR": 5, "RBI": 18, "SB": 4, "OPS": 0.150,
    "QS": 2, "K": 28, "ERA": 1.8, "WHIP": 0.30, "SVH": 5,
}


def category_leverage(my_val: float, opp_val: float, cat: str, elapsed_fraction: float = 1.0) -> float:
    """Returns leverage multiplier in [0.5, 1.5] based on how close this
    category is — close matchups boost every contribution, locked cats get
    damped. `elapsed_fraction` (0..1) is the share of the scoring week that's
    already played: scales the delta from neutral, so early in the week
    nothing is treated as locked or close (you're just trying to play the
    best lineup). Defaults to 1.0 for callers that don't supply it."""
    swing = TYPICAL_WEEKLY_SWING.get(cat, 1.0)
    gap = abs(my_val - opp_val)
    ratio = gap / max(swing, 0.001)
    if ratio < 0.30: raw = 1.5     # very close, every point swings the cat
    elif ratio < 1.00: raw = 1.0   # competitive
    else: raw = 0.5                # essentially decided
    # Damp the deviation from neutral by how much of the week is in the books.
    # On Monday (elapsed≈0.14), the gap is mostly noise and leverage stays ~1.0.
    # On Sunday (elapsed≈1.0), full leverage applies.
    f = max(0.0, min(1.0, elapsed_fraction))
    return 1.0 + (raw - 1.0) * f


def category_value_hitter(p, vegas_factor: float, park_factor: float, platoon_factor: float, order_factor: float, leverage: dict | None = None) -> tuple[float, dict]:
    """Project the player's expected category contributions today, then sum
    z-scores. Returns (cat_value, per_category_dict)."""
    c = p.components or {}
    rates = c.get("rolling_cats") or {}
    if not rates:
        return 0.0, {}
    # Apply matchup factors:
    #  - Vegas/Park/Order/Platoon scale counting stats (R, HR, RBI, SB).
    #  - Park HR factor specifically lifts HR more.
    #  - OPS shifts by a smaller amount via the same multiplicative env, but
    #    OPS is closer to a "rate of true talent" so we damp the effect.
    counting_mult = vegas_factor * park_factor * platoon_factor * order_factor
    rate_mult = 1.0 + (counting_mult - 1.0) * 0.40   # damped
    proj = {}
    proj["R"]   = rates.get("R", 0)   * counting_mult
    proj["HR"]  = rates.get("HR", 0)  * counting_mult  # park already in counting_mult; no double-application
    proj["RBI"] = rates.get("RBI", 0) * counting_mult
    proj["SB"]  = rates.get("SB", 0)  * counting_mult
    proj["OPS"] = rates.get("OPS", 0) * rate_mult
    z = 0.0
    lev = leverage or {}
    for k, v in proj.items():
        raw_z = (v - LG_HITTER_RATES[k]) / LG_HITTER_STDEV[k]
        z += raw_z * lev.get(k, 1.0)
    return z, proj


_RP_CACHE: dict[tuple, tuple[float, dict | None]] = {}
_RP_TTL = 6 * 3600
_RP_CACHE_MAX = 5000  # cap so a long-running web process doesn't accumulate
                      # entries for every reliever ever projected — when we
                      # hit the cap, evict the oldest 20% by timestamp.


def _rp_cache_maybe_evict():
    if len(_RP_CACHE) <= _RP_CACHE_MAX:
        return
    # Sort keys by insertion time, drop the 20% oldest.
    items = sorted(_RP_CACHE.items(), key=lambda kv: kv[1][0])
    drop_n = max(1, _RP_CACHE_MAX // 5)
    for k, _ in items[:drop_n]:
        _RP_CACHE.pop(k, None)


def project_reliever_cats(pid: int, season: int) -> dict | None:
    """Project a reliever's per-day category contribution. 6h cache so a
    52-RP roster doesn't blow up the lineup endpoint."""
    key = (pid, season)
    now = time.time()
    cached = _RP_CACHE.get(key)
    if cached and (now - cached[0]) < _RP_TTL:
        return cached[1]
    try:
        seasn = mlb_api.player_stats(pid, group="pitching", season=season)
    except Exception:
        _RP_CACHE[key] = (now, None)
        return None
    g = _safe_float(seasn.get("gamesPlayed"))
    if g < 3:
        _RP_CACHE[key] = (now, None)
        return None
    ip = _safe_float(seasn.get("inningsPitched"))
    if ip <= 0:
        _RP_CACHE[key] = (now, None)
        return None
    # Skip starters — players whose appearances are mostly starts. They're
    # SPs not pitching today, NOT relievers.
    gs = _safe_float(seasn.get("gamesStarted"))
    if g > 0 and (gs / g) > 0.5:
        _RP_CACHE[key] = (now, None)
        return None
    k = _safe_float(seasn.get("strikeOuts"))
    sv = _safe_float(seasn.get("saves"))
    hld = _safe_float(seasn.get("holds"))
    era = _safe_float(seasn.get("era"), default=4.20)
    whip = _safe_float(seasn.get("whip"), default=1.30)
    # Per-appearance averages.
    ip_per_app = ip / g
    k_per_app = k / g
    svh_per_app = (sv + hld) / g
    # Daily usage probability — high-leverage closers pitch more often.
    # Closer signal: SV%; if a guy has many saves, he's likely the closer.
    sv_rate = sv / g if g else 0.0
    hld_rate = hld / g if g else 0.0
    if sv_rate >= 0.30:
        usage = 0.45        # closer
    elif (sv_rate + hld_rate) >= 0.30:
        usage = 0.40        # setup / late innings
    else:
        usage = 0.30        # middle relief
    out = {
        "QS": 0.0,
        "K": k_per_app * usage,
        "ERA": era,
        "WHIP": whip,
        "SVH": svh_per_app * usage,
        "_usage": usage,
        "_ip_per_app": ip_per_app,
    }
    _RP_CACHE[key] = (now, out)
    _rp_cache_maybe_evict()
    return out


def category_value_reliever(rates: dict, leverage: dict | None = None) -> tuple[float, dict]:
    """Z-score a reliever's projected day. ERA/WHIP only contribute when usage
    expects them to actually pitch — damp by usage so a guy who pitches 1/3
    days doesn't drag your full-week ERA the same way a starter does."""
    lev = leverage or {}
    usage = rates.get("_usage", 0.35)
    proj = {"QS": 0.0, "K": rates["K"], "ERA": rates["ERA"], "WHIP": rates["WHIP"], "SVH": rates["SVH"]}
    z = 0.0
    z += ((proj["K"]   - LG_PITCHER_RATES["K"] * usage)   / LG_PITCHER_STDEV["K"]) * lev.get("K", 1.0)
    # ERA/WHIP ratios — damp by usage since they only "happen" sometimes.
    z += ((LG_PITCHER_RATES["ERA"]  - proj["ERA"])  / LG_PITCHER_STDEV["ERA"])  * lev.get("ERA", 1.0) * usage
    z += ((LG_PITCHER_RATES["WHIP"] - proj["WHIP"]) / LG_PITCHER_STDEV["WHIP"]) * lev.get("WHIP", 1.0) * usage
    z += ((proj["SVH"]  - LG_PITCHER_RATES["SVH"])   / LG_PITCHER_STDEV["SVH"]) * lev.get("SVH", 1.0)
    return z, proj


def category_value_pitcher(p, vegas_factor: float, park_factor: float, leverage: dict | None = None) -> tuple[float, dict]:
    c = p.components or {}
    rates = c.get("rolling_cats") or {}
    if not rates:
        return 0.0, {}
    # Pitcher matchup: better Vegas (low opp implied total) helps K (more
    # outs by way of more PA chances) and lowers ERA/WHIP. Park factor
    # similar logic. Vegas factor here is from PITCHER's perspective — we
    # already compute it inverted for SPs, so >1.0 = good for SP.
    proj = {}
    proj["QS"]   = rates.get("QS", 0)   * vegas_factor * park_factor
    proj["K"]    = rates.get("K", 0)    * (1.0 + (vegas_factor - 1.0) * 0.50)
    # ERA and WHIP — INVERSE: a vegas_factor of 1.10 means -10% on ERA.
    proj["ERA"]  = rates.get("ERA", 4.20) / max(vegas_factor, 0.01) / max(park_factor, 0.01)
    proj["WHIP"] = rates.get("WHIP", 1.30) / max(vegas_factor, 0.01)
    proj["SVH"]  = 0.0   # SPs don't get holds; reliever projection is a separate concern
    lev = leverage or {}
    z = 0.0
    z += ((proj["QS"]   - LG_PITCHER_RATES["QS"])   / LG_PITCHER_STDEV["QS"])  * lev.get("QS", 1.0)
    z += ((proj["K"]    - LG_PITCHER_RATES["K"])    / LG_PITCHER_STDEV["K"])   * lev.get("K", 1.0)
    # Inverse — lower ERA/WHIP is BETTER, so flip the z sign.
    z += ((LG_PITCHER_RATES["ERA"]  - proj["ERA"])  / LG_PITCHER_STDEV["ERA"])  * lev.get("ERA", 1.0)
    z += ((LG_PITCHER_RATES["WHIP"] - proj["WHIP"]) / LG_PITCHER_STDEV["WHIP"]) * lev.get("WHIP", 1.0)
    return z, proj


def _per_start_pitcher_points(stats: dict) -> float:
    gs = max(_safe_float(stats.get("gamesStarted")), 1.0)
    ip = _safe_float(stats.get("inningsPitched"))
    # convert IP "x.y" already lost — use floor/decimal approximation
    outs = int(ip) * 3 + round((ip - int(ip)) * 10)
    p = PITCHER_POINTS
    pts = (
        outs * p["out"]
        + _safe_float(stats.get("strikeOuts")) * p["strikeOut"]
        + _safe_float(stats.get("earnedRuns")) * p["earnedRun"]
        + _safe_float(stats.get("hits")) * p["hitAllowed"]
        + _safe_float(stats.get("baseOnBalls")) * p["walkIssued"]
        + _safe_float(stats.get("hitBatsmen")) * p["hitBatsman"]
    )
    qs = _safe_float(stats.get("qualityStarts"))
    pts += qs * p["qualityStart"]
    pts += _safe_float(stats.get("completeGames")) * p["completeGame"]
    pts += _safe_float(stats.get("shutouts")) * p["shutout"]
    return pts / gs


def _per_start_pitcher_events(stats: dict) -> dict:
    """Per-start expected event counts behind the SP points (outs, K, ER,
    hits, BB, HBP) plus QS rate. Mirrors _per_start_pitcher_points."""
    gs = max(_safe_float(stats.get("gamesStarted")), 1.0)
    ip = _safe_float(stats.get("inningsPitched"))
    outs = int(ip) * 3 + round((ip - int(ip)) * 10)
    return {
        "outs": outs / gs,
        "K": _safe_float(stats.get("strikeOuts")) / gs,
        "ER": _safe_float(stats.get("earnedRuns")) / gs,
        "H": _safe_float(stats.get("hits")) / gs,
        "BB": _safe_float(stats.get("baseOnBalls")) / gs,
        "HBP": _safe_float(stats.get("hitBatsmen")) / gs,
        "QS": _safe_float(stats.get("qualityStarts")) / gs,
    }


_PITCHER_DECOMP_KEYS = [
    ("outs", "out", "Outs (IP×3)"),
    ("K", "strikeOut", "Strikeouts"),
    ("QS", "qualityStart", "Quality start"),
    ("ER", "earnedRun", "Earned runs"),
    ("H", "hitAllowed", "Hits allowed"),
    ("BB", "walkIssued", "Walks"),
    ("HBP", "hitBatsman", "HBP"),
]


def _point_decomp_pitcher(events: dict, proj_points: float) -> dict | None:
    """SP version of _point_decomp_hitter — scales the representative
    per-start line so its scored points sum to the projection."""
    raw_pts = sum(events.get(ek, 0.0) * PITCHER_POINTS[sk] for ek, sk, _ in _PITCHER_DECOMP_KEYS)
    if raw_pts <= 0.5:
        return None
    scale = proj_points / raw_pts
    lines = []
    for ek, sk, label in _PITCHER_DECOMP_KEYS:
        n = events.get(ek, 0.0) * scale
        if abs(n) < 0.005:
            continue
        w = PITCHER_POINTS[sk]
        dp = 1 if ek == "outs" else 2
        lines.append({"label": label, "key": ek, "n": round(n, dp),
                      "pts_each": w, "pts": round(n * w, 2)})
    return {"raw_pts": round(raw_pts, 2), "scale": round(scale, 3),
            "lines": lines, "total": round(proj_points, 2)}


# Two-tier cache: in-memory (instant) + disk (survives redeploys/restarts).
# Projections are based on rolling stat windows that only meaningfully change
# after games complete overnight, so we hold them for 6h.
_PROJ_CACHE: dict[tuple, tuple[float, list]] = {}
_PROJ_TTL_SEC = 6 * 3600

# Per-key lock to prevent cache stampedes — when N concurrent refresh requests
# land for the same date, only ONE actually does the project_slate compute;
# the rest wait on the lock and read the freshly-cached result. Without this,
# 5 concurrent /api/projections?refresh=true calls each spun up their own
# 30s compute, each loading ~200MB of player stats, OOMing the 512MB box
# (now 1GB, but the fix prevents the stampede from recurring at any size).
import threading
_PROJ_LOCKS: dict[tuple, threading.Lock] = {}
_PROJ_LOCKS_GUARD = threading.Lock()

def _proj_lock(key: tuple) -> threading.Lock:
    """Get/create the per-key compute lock atomically."""
    with _PROJ_LOCKS_GUARD:
        lock = _PROJ_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROJ_LOCKS[key] = lock
        return lock

# Bump this whenever the projection MATH changes (any factor weight, any new
# multiplier, any structural model change). Cached entries with a stale
# MODEL_REV are ignored and recomputed. This is the only reliable way to
# avoid 'calibration says HOT bias is X' when the cache was written under
# an older code version.
MODEL_REV = "2026-06-11-v9.40" # arsenal-vs-hitter pitch-type matchup + personalized platoon splits


def _proj_disk_path(key: tuple) -> str:
    import hashlib
    from .disk_cache import CACHE_DIR
    digest = hashlib.md5(repr(key).encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"projections_{digest}.json")


def _proj_to_dict(p: "Projection") -> dict:
    return {
        "player_id": p.player_id, "name": p.name, "team_id": p.team_id,
        "position": p.position, "role": p.role,
        "projected_points": p.projected_points,
        "components": p.components, "notes": p.notes,
    }


def _proj_from_dict(d: dict) -> "Projection":
    return Projection(
        player_id=d["player_id"], name=d["name"], team_id=d.get("team_id"),
        position=d.get("position"), role=d["role"],
        projected_points=d["projected_points"],
        components=d.get("components", {}), notes=d.get("notes", []),
    )


def _schedule_sp_fingerprint(d: Date) -> str:
    """Fingerprint of today's probable pitchers — used to invalidate the
    projection cache when MLB updates probableStarter mid-day. Without this,
    a 6h cache could hold a Matz-less projection for hours after MLB lists
    him as the Rays' starter, and TB hitters' opp_sp signal stays wrong.
    Cheap hash of (gamePk, home_sp_id, away_sp_id) tuples."""
    try:
        games = mlb_api.schedule(d)
    except Exception:
        return ""
    sigs = []
    for g in games:
        gpk = g.get("gamePk")
        if gpk is None:
            continue
        teams = g.get("teams") or {}
        hsp = ((teams.get("home") or {}).get("probablePitcher") or {}).get("id")
        asp = ((teams.get("away") or {}).get("probablePitcher") or {}).get("id")
        sigs.append((gpk, hsp, asp))
    sigs.sort()
    import hashlib
    return hashlib.md5(repr(sigs).encode()).hexdigest()[:16]


def project_slate_cached(
    d: Date, *, team_filter: set[int] | None = None, force_refresh: bool = False
) -> list["Projection"]:
    """Memoized per date (full slate). team_filter is applied downstream so
    projections-tab and draft-tab share one cache entry — first hit pays the
    ~50 MLB API calls, the rest are instant. 6h TTL, persisted to disk so
    redeploys don't force a recompute.

    The cache also tracks a probable-pitcher fingerprint: when MLB updates a
    probable starter (Matz announced 2h after our cache was built, e.g.),
    fingerprint flips and the cache is forced to recompute. Without this,
    the slate stays missing the new SP until the 6h TTL expires."""
    key = (d.isoformat(), None)
    now = time.time()
    full: list["Projection"] | None = None
    if not force_refresh:
        live_fp = _schedule_sp_fingerprint(d)
        cached = _PROJ_CACHE.get(key)
        if cached is not None and (now - cached[0]) < _PROJ_TTL_SEC:
            full = cached[1]
        if full is None:
            path = _proj_disk_path(key)
            try:
                if os.path.exists(path) and (now - os.path.getmtime(path)) < _PROJ_TTL_SEC:
                    with open(path) as f:
                        raw = json.load(f)
                    # Reject cache entries from older model versions —
                    # calibration was getting bogus numbers because we'd
                    # tune model_rev=v2 then read back projections written
                    # under v1 from disk. Force a recompute on stale rev.
                    if isinstance(raw, dict) and raw.get("model_rev") == MODEL_REV:
                        # Also reject when the probable-SP fingerprint has
                        # changed since the cache was written — a new
                        # probable pitcher should immediately appear.
                        if live_fp and raw.get("sched_fp") and raw.get("sched_fp") != live_fp:
                            logging.info(
                                "projection cache for %s busted: probable-SP fingerprint changed",
                                d.isoformat(),
                            )
                        else:
                            full = [_proj_from_dict(x) for x in raw.get("projections", [])]
                            _PROJ_CACHE[key] = (os.path.getmtime(path), full)
            except Exception:
                full = None
    if full is None:
        # Stampede protection — N concurrent refreshes for the same date
        # all try to compute. Each project_slate holds ~200MB. The first
        # one wins the lock; the rest wait and read the cached result it
        # produces. Prevents the OOM cascade we hit during the 5/12 v8
        # refresh storm.
        lock = _proj_lock(key)
        with lock:
            # Re-check the cache after acquiring — a peer compute may
            # have finished while we waited.
            cached = _PROJ_CACHE.get(key)
            if cached is not None and (now - cached[0]) < _PROJ_TTL_SEC and not force_refresh:
                full = cached[1]
            else:
                full = project_slate(d, team_filter=None)
                _PROJ_CACHE[key] = (now, full)
                try:
                    from .disk_cache import CACHE_DIR
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    path = _proj_disk_path(key)
                    tmp = path + ".tmp"
                    payload = {
                        "model_rev": MODEL_REV,
                        "sched_fp": _schedule_sp_fingerprint(d),
                        "projections": [_proj_to_dict(p) for p in full],
                    }
                    with open(tmp, "w") as f:
                        json.dump(payload, f)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, path)
                except Exception as e:
                    logging.warning("projection cache write failed for %s: %s", d.isoformat(), e)
    if team_filter:
        return [p for p in full if p.team_id in team_filter]
    return full


def project_slate(d: Date, *, team_filter: set[int] | None = None) -> list[Projection]:
    """Project everyone playing today. Handles probable SPs + position players.

    If `team_filter` is provided, only project players from those team IDs (used
    when a draft is restricted to a subset of the day's games).
    """
    season = d.year
    games = mlb_api.schedule(d)

    # Per-game HP umpire k_factor lookup (1.0 = neutral, >1 = wider zone /
    # more Ks). Backed by UmpScorecards; missing data falls back to neutral.
    try:
        from . import umpires as umpires_mod
        ump_rows = umpires_mod.umpires_for_date(d.isoformat()) or []
        ump_k_by_pk = {u["game_pk"]: u.get("k_factor") for u in ump_rows if u.get("game_pk")}
    except Exception as e:
        logging.warning("ump data unavailable for %s: %s", d.isoformat(), e)
        ump_k_by_pk = {}

    # Build matchup map: team_id -> {opp, opp_sp, park (run_env, hr_factor)}
    matchups: dict[int, dict] = {}
    probable_sps: dict[int, dict] = {}  # sp_id -> {team_id, opp_team_id, park}
    for g in games:
        # Skip postponed/cancelled/suspended games — players in them aren't
        # actually playing today.
        status_state = (g.get("status") or {}).get("detailedState", "") or ""
        if status_state in ("Postponed", "Cancelled", "Suspended", "Completed Early"):
            continue
        home = ((g.get("teams") or {}).get("home") or {})
        away = ((g.get("teams") or {}).get("away") or {})
        home_team = (home.get("team") or {}).get("id")
        away_team = (away.get("team") or {}).get("id")
        home_sp = (home.get("probablePitcher") or {}).get("id")
        away_sp = (away.get("probablePitcher") or {}).get("id")
        # Park factor for the venue (home team's park) — combine static park
        # constants with the day's weather-driven HR adjustment.
        home_abbr = _TEAM_ABBR.get(home_team or 0, "")
        run_env, hr_static = weather_mod.park_factor(home_abbr)
        try:
            wx = weather_mod.park_forecast(home_abbr, g.get("gameDate") or "")
            wx_hr = (wx or {}).get("hr_factor", 1.0)
        except Exception:
            wx_hr = 1.0
        # Combine static park HR factor with weather; weight static heavier.
        combined_hr = (hr_static * 0.7) + (wx_hr * 0.3)
        park = {"run_env": run_env, "hr_factor": combined_hr, "venue": home_abbr}
        away_abbr = _TEAM_ABBR.get(away_team or 0, "")
        home_sp_name = (home.get("probablePitcher") or {}).get("fullName")
        away_sp_name = (away.get("probablePitcher") or {}).get("fullName")
        if home_team and away_team:
            matchups[home_team] = {
                "opp": away_team, "opp_sp": away_sp, "park": park,
                "opp_abbr": away_abbr, "opp_sp_name": away_sp_name,
                "is_home": True,
            }
            matchups[away_team] = {
                "opp": home_team, "opp_sp": home_sp, "park": park,
                "opp_abbr": home_abbr, "opp_sp_name": home_sp_name,
                "is_home": False,
            }
        ump_k = ump_k_by_pk.get(g.get("gamePk"))
        if home_sp:
            probable_sps[home_sp] = {
                "team_id": home_team, "opp_team_id": away_team, "park": park,
                "name": home_sp_name,
                "opp_abbr": away_abbr, "is_home": True,
                "ump_k_factor": ump_k,
            }
        if away_sp:
            probable_sps[away_sp] = {
                "team_id": away_team, "opp_team_id": home_team, "park": park,
                "name": away_sp_name,
                "opp_abbr": home_abbr, "is_home": False,
                "ump_k_factor": ump_k,
            }

    # Inject manual pitcher adds (data/manual_pool_adds.json) into probable_sps
    # so they get a real project_pitcher projection. Without this, an
    # IL-activated SP shows in the pool but with no projection (since
    # probable_sps only sources from MLB API's probablePitcher field).
    try:
        for add in mlb_api._load_manual_pool_adds(d):
            pos = (add.get("position") or "").upper()
            if pos not in ("SP", "P"):
                continue  # RPs aren't projected here; skip
            pid = add.get("player_id")
            tid = add.get("team_id")
            if not pid or not tid or tid not in matchups:
                continue
            if pid in probable_sps:
                continue  # MLB API already had them as probable
            m = matchups[tid]
            probable_sps[pid] = {
                "team_id": tid, "opp_team_id": m["opp"], "park": m["park"],
                "name": add.get("name") or f"player_{pid}",
                "opp_abbr": m.get("opp_abbr"), "is_home": m.get("is_home"),
                "ump_k_factor": None,
            }
    except Exception as e:
        logging.warning("manual SP inject failed: %s", e)

    pool = mlb_api.players_in_slate(d)
    # Pull lineups for batting order info (None if lineup not yet posted).
    try:
        lineups = mlb_api.lineups_by_date(d)
    except Exception:
        lineups = {}

    # Catcher framing (v9.8): map each team's starting catcher (when lineup
    # posted) to their season framing run-value, then convert to a small
    # K-rate multiplier for that team's pitcher. Elite framers (rv ~+5 to
    # +10) generate ~0.3-0.5 extra K per start; anti-framers cost the same.
    # Pre-lineup-posted: no signal, multiplier defaults to 1.0 (neutral).
    catcher_framing_by_team: dict[int, float] = {}
    try:
        framing = savant.catcher_framing(season)
        for g in games:
            teams = g.get("teams") or {}
            for side_key, players_key in (("home", "homePlayers"), ("away", "awayPlayers")):
                team_id = ((teams.get(side_key) or {}).get("team") or {}).get("id")
                players = (g.get("lineups") or {}).get(players_key) or []
                if not team_id or not players:
                    continue
                # The catcher in a posted lineup is whichever player has
                # primaryPosition == "C". DH'd catchers don't apply (their
                # primaryPosition is C but someone else is behind the plate).
                # Heuristic: first player with primaryPosition=C is the
                # actual catcher for today's game in ~95% of cases.
                catcher_pid = next(
                    (p.get("id") for p in players
                     if (p.get("primaryPosition") or {}).get("abbreviation") == "C"),
                    None,
                )
                if catcher_pid and catcher_pid in framing:
                    catcher_framing_by_team[team_id] = framing[catcher_pid]
    except Exception as e:
        logging.warning("catcher framing fetch failed: %s", e)

    # Vegas implied team totals — keyed by full team name.
    try:
        team_totals_by_name = odds_api.get_team_totals(d.isoformat()) or {}
    except Exception:
        team_totals_by_name = {}
    team_totals: dict[int, float] = {}
    for tid, full in _TEAM_FULLNAME.items():
        v = team_totals_by_name.get(full)
        if v is not None:
            team_totals[tid] = v

    # Pitcher K-prop market lines (v9.5) — keyed by pitcher name, normalized
    # for matching. We use the actual betting market (not our internal K-prop
    # tester, which user flagged as garbage). Sharpest single signal for K
    # output. Cached on disk per-day by odds_api. Failure → empty dict and
    # project_pitcher just skips the adjustment.
    try:
        k_prop_lines_raw, _meta = odds_api.get_pitcher_strikeout_lines_cached(d.isoformat())
    except Exception as e:
        logging.warning("k-prop lines fetch failed: %s", e)
        k_prop_lines_raw = {}
    def _norm_pitcher_name(s: str) -> str:
        import unicodedata
        nfkd = unicodedata.normalize("NFKD", s or "")
        no_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
        return no_accent.lower().replace(".", "").replace("'", "").strip()
    k_prop_by_name: dict[str, float] = {}
    for nm, info in (k_prop_lines_raw or {}).items():
        line = info.get("line") if isinstance(info, dict) else None
        if line is None:
            continue
        k_prop_by_name[_norm_pitcher_name(nm)] = float(line)

    # Batter total-bases props (v9.39) — devig each batter's over/under to
    # P(over), convert line+juice to a market-expected-TB scalar, z-score it
    # across the slate (self-normalizing: "how much pop does the market give
    # this guy TODAY vs everyone else"), and hand each hitter their z. See
    # _BAT_TB_PROP at module top for the sizing rationale.
    tb_prop_by_name: dict[str, dict] = {}
    if _BAT_TB_PROP:
        try:
            tb_lines_raw, _tb_meta = odds_api.get_batter_total_bases_lines_cached(d.isoformat())
        except Exception as e:
            logging.warning("tb-prop lines fetch failed: %s", e)
            tb_lines_raw = {}
        def _amer_prob(o: int) -> float:
            return 100.0 / (o + 100.0) if o > 0 else (-o) / ((-o) + 100.0)
        scalars: dict[str, dict] = {}
        for nm, info in (tb_lines_raw or {}).items():
            if not isinstance(info, dict) or info.get("book_count", 0) < 2:
                continue  # one-book lines are noise
            line, oo, uo = info.get("line"), info.get("over_odds"), info.get("under_odds")
            if line is None or oo is None or uo is None:
                continue  # need both sides to devig
            po, pu = _amer_prob(int(oo)), _amer_prob(int(uo))
            p_over = po / (po + pu)
            # line + juice → one expected-TB scalar. dP(TB≥line)/dE[TB] ≈ 0.28
            # for a typical ~1.4 TB/G hitter, so ~3.5 TB per unit of P(over).
            # Exact slope barely matters — the z-score normalizes it away.
            market_tb = float(line) + (p_over - 0.5) * 3.5
            scalars[_norm_pitcher_name(nm)] = {
                "line": float(line), "p_over": round(p_over, 3), "mtb": market_tb,
            }
        if len(scalars) >= 30:  # need a real cross-section to z-score against
            vals = [v["mtb"] for v in scalars.values()]
            mu = sum(vals) / len(vals)
            sd = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5
            if sd > 1e-6:
                for nm, v in scalars.items():
                    tb_prop_by_name[nm] = {
                        "line": v["line"], "p_over": v["p_over"],
                        "z": (v["mtb"] - mu) / sd,
                    }

    # Bullpen quality: season bullpen ERA per team. ~30 API calls cached for the day.
    bullpen_era = _bullpen_era_by_team(season)

    # Handedness for all players — single bulk call.
    handedness = mlb_api.handedness_by_player(season)

    # Rolling 14-day form signal — moved to project_hitter/project_pitcher,
    # computed from MLB Stats API K%-rate shift (luck-stripped process metric)
    # using the byDateRange stats already fetched per-player. Replaces the
    # previous Savant /expected_statistics?start_date=...&end_date=... pull
    # which silently ignored the date params and returned season-wide xwOBA
    # for every window — making rolling_factor a no-op since the day it
    # shipped. Detected 2026-05-18, retired same day.

    projections: list[Projection] = []
    # Hitters — everyone non-pitcher in the slate roster pool. Project hitters
    # FIRST so we can compute opposing lineup quality from posted lineups
    # before we project pitchers (the opp lineup factor needs hitter projs).
    # Each project_hitter makes ~4 MLB-API stat calls (L3/L7/L14/season) plus
    # the opposing SP's line — all network I/O. Run them concurrently so a cold
    # slate doesn't serialize hundreds of round-trips (the draft pool felt slow
    # because this loop was sequential). The mlb_api/savant layers are disk-
    # cached and tolerate concurrent access (dynasty already maps the same way).
    def _project_one_hitter(pid, meta):
        team_id = meta.get("teamId")
        m = matchups.get(team_id or 0, {})
        opp_sp = m.get("opp_sp")
        return project_hitter(
            pid, meta["name"],
            team_id=team_id,
            position=meta.get("position"),
            season=season,
            opposing_sp_id=opp_sp,
            park=m.get("park"),
            batting_order=(lineups.get(pid) or {}).get("batting_order"),
            implied_team_total=team_totals.get(team_id) if team_id else None,
            opp_bullpen_era=bullpen_era.get(m.get("opp")) if m.get("opp") else None,
            bats=(handedness.get(pid) or {}).get("bats"),
            opp_throws=(handedness.get(opp_sp) or {}).get("throws") if opp_sp else None,
            opp_abbr=m.get("opp_abbr"),
            opp_sp_name=m.get("opp_sp_name"),
            is_home=m.get("is_home"),
            lineup_status=(lineups.get(pid) or {}).get("status"),
            tb_prop=tb_prop_by_name.get(_norm_pitcher_name(meta["name"])),
            as_of=d,
        )

    hitter_pids = [
        pid for pid, meta in pool.items()
        if meta.get("positionType") != "Pitcher"
        and (team_filter is None or meta.get("teamId") in team_filter)
    ]
    with ThreadPoolExecutor(max_workers=12) as ex:
        projections.extend(ex.map(lambda pid: _project_one_hitter(pid, pool[pid]), hitter_pids))

    # Compute opposing-lineup-quality per team from POSTED lineups.
    # CRITICAL — use each hitter's base_pg (rolling form + Statcast prior,
    # BEFORE any matchup factors), NOT their projected_points. The full
    # projection includes vegas_factor + sp_factor which reflect the
    # opposing pitcher's quality. If we used projected_points, that
    # pitcher's quality would feed back into our own projection of him
    # (good pitcher → suppresses hitter projs → high lineup_factor →
    # boosts pitcher again). base_pg is the lineup's intrinsic quality,
    # independent of who's pitching today — the only honest signal.
    posted_by_team: dict[int, list[float]] = {}
    proj_by_pid = {p.player_id: p for p in projections}
    for pid, ls in lineups.items():
        if ls.get("status") != "in":
            continue
        team_id_ls = ls.get("team_id")
        proj = proj_by_pid.get(pid)
        if team_id_ls and proj and proj.role == "hitter":
            base_pg = (proj.components or {}).get("base_pg")
            if base_pg and base_pg > 0:
                posted_by_team.setdefault(team_id_ls, []).append(base_pg)
    lineup_avg_pg_by_team: dict[int, float] = {}
    for tid, pts_list in posted_by_team.items():
        if len(pts_list) >= 7:  # require near-full lineup posted; partial is noise
            lineup_avg_pg_by_team[tid] = sum(pts_list) / len(pts_list)

    # Pitchers — project AFTER hitters so we can attach opp lineup quality.
    # Parallelized for the same reason (each makes several network stat calls).
    def _project_one_pitcher(sp_id, info):
        sp_name = info["name"] or pool.get(sp_id, {}).get("name", "?")
        return project_pitcher(
            sp_id, sp_name,
            team_id=info["team_id"], season=season,
            opponent_team_id=info["opp_team_id"],
            park=info.get("park"),
            opp_implied_total=team_totals.get(info["opp_team_id"]),
            throws=(handedness.get(sp_id) or {}).get("throws"),
            opp_abbr=info.get("opp_abbr"),
            is_home=info.get("is_home"),
            ump_k_factor=info.get("ump_k_factor"),
            opp_lineup_avg_pg=lineup_avg_pg_by_team.get(info["opp_team_id"]),
            catcher_framing_runs=catcher_framing_by_team.get(info["team_id"]),
            vegas_k_line=k_prop_by_name.get(_norm_pitcher_name(sp_name)),
            as_of=d,
        )

    sp_ids = [
        sp_id for sp_id, info in probable_sps.items()
        if team_filter is None or info["team_id"] in team_filter
    ]
    with ThreadPoolExecutor(max_workers=12) as ex:
        projections.extend(ex.map(lambda sp_id: _project_one_pitcher(sp_id, probable_sps[sp_id]), sp_ids))

    # Two-way players (Ohtani) appear once as a hitter and once as a pitcher
    # if they're also that day's probable SP. Label the pitcher row "(P)" so
    # the UI can disambiguate the two rows.
    by_id: dict[int, list] = {}
    for p in projections:
        by_id.setdefault(p.player_id, []).append(p)
    for plist in by_id.values():
        if len(plist) > 1:
            for p in plist:
                if p.role == "pitcher" and "(P)" not in p.name:
                    p.name = f"{p.name} (P)"

    projections.sort(key=lambda p: p.projected_points, reverse=True)
    return projections
