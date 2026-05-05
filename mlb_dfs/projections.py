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
import os
import time
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Iterable

from . import mlb_api, odds_api, savant, weather as weather_mod
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

LEAGUE_AVG_HITTER_POINTS_PER_GAME = 6.5
LEAGUE_AVG_SP_POINTS_PER_START = 11.0

# League-median Statcast benchmarks for the multiplier (rough 2024-25 medians).
LG_BARREL_PCT_HITTER = 6.5      # %
LG_HARDHIT_PCT_HITTER = 38.0
LG_BARREL_PCT_ALLOWED = 6.5
LG_XWOBA_AGAINST = 0.310


def _statcast_implied_pg_hitter(brl: float, hh: float) -> float | None:
    """True-talent pts/G estimate from Statcast quality-of-contact metrics.
    Blended with the rolling base in project_hitter.

    Coefficients tuned against 3 days of calibration data. Original formula
    (1.5 + 0.50*brl + 0.045*hh, cap 11) over-projected ELITE-tier hitters by
    ~1.3 pts. Compressed the upper end: lower cap and slope on barrel%."""
    if not brl and not hh:
        return None
    val = 2.0 + (brl or LG_BARREL_PCT_HITTER) * 0.42 + (hh or LG_HARDHIT_PCT_HITTER) * 0.040
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
        base += (LG_BARREL_PCT_ALLOWED - brl_a) * 0.4
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
            delta = (hh_a - LG_HARDHIT_PCT_HITTER) / LG_HARDHIT_PCT_HITTER
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
    rolling_xwoba: float | None = None,
    season_xwoba: float | None = None,
) -> Projection:
    last3 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=3)
    last7 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=7)
    last14 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=14)
    seasn = mlb_api.player_stats(pid, group="hitting", season=season)

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

    # Streak-trust override: 4 days of calibration showed HOT players were
    # consistently under-projected by ~+4 pts and COLD over-projected by ~-3 pts.
    # After first 70% override, residual remained ~+2/-1.3 — the L3 sample
    # itself underestimates HOT players' continuation. Bumped to 80%.
    if pg_3 is not None and games_3 >= 2 and form_tag in ("HOT", "COLD"):
        streak_base = 0.80 * pg_3 + 0.20 * base_pg
        notes.append(f"streak override ({form_tag}): 0.8*L3 + 0.2*weighted → {streak_base:.2f}")
        base_pg = streak_base

    # Opposing SP adjustment: scale by opponent SP's allowed rate vs league avg.
    sp_factor = 1.0
    if opposing_sp_id:
        sp_season = mlb_api.player_stats(opposing_sp_id, group="pitching", season=season)
        ip = _safe_float(sp_season.get("inningsPitched"))
        if ip >= 20:
            era = _safe_float(sp_season.get("era"), default=4.20)
            whip = _safe_float(sp_season.get("whip"), default=1.30)
            # league baselines: 4.20 ERA / 1.30 WHIP
            sp_factor = (era / 4.20) * 0.6 + (whip / 1.30) * 0.4
            sp_factor = max(0.6, min(sp_factor, 1.45))
            notes.append(f"opposing SP adj x{sp_factor:.2f} (ERA {era:.2f} WHIP {whip:.2f})")

    qoc = savant.lookup_batter_qoc(pid, season) or None
    brl = _safe_float((qoc or {}).get("brl_percent"))
    hh = _safe_float((qoc or {}).get("ev95percent"))
    # Statcast-implied true-talent pts/G — blended into the rolling base.
    # Statcast reflects MUCH larger samples than 14 days of game logs, so it
    # acts as a strong prior on true talent. Hot streaks still help (rolling
    # base moves), but a Judge-tier batter on a 3-game cold streak doesn't get
    # punished as hard, and a backup catcher who happened to be on a hot 1-game
    # streak gets dragged toward his Statcast baseline.
    statcast_pg = _statcast_implied_pg_hitter(brl, hh) if (brl or hh) else None
    if statcast_pg is not None:
        # Adaptive blend: Statcast is THE most predictive signal for true talent
        # on a typical day, so it gets full weight (0.35) for steady/untagged
        # players. But when the player is HOT or COLD, the rolling base is
        # capturing a real streak signal that Statcast can't see — calibration
        # showed HOT players were under-projected by ~+5 pts at w=0.35. Drop
        # Statcast weight for streaking players so the streak signal carries.
        STATCAST_WEIGHT = 0.15 if form_tag in ("HOT", "COLD") else 0.35
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
    # factor (which directly affects HR-heavy fantasy scoring). Run env weighted
    # higher since it captures more of the projected box-score value.
    park_factor = 1.0
    if park:
        run_env = park.get("run_env", 1.0)
        hr_f = park.get("hr_factor", 1.0)
        park_factor = (run_env * 0.65) + (hr_f * 0.35)
        # Clamp to keep it from doing more than ~15% in either direction.
        park_factor = max(0.85, min(park_factor, 1.18))
        venue = park.get("venue", "")
        notes.append(f"park {venue} x{park_factor:.2f} (run {run_env:.2f}, HR {hr_f:.2f})")

    # Batting-order PA factor: leadoff hitters get ~4.6 PA/game, #9 gets ~3.7.
    # That's a ~22% PA spread top to bottom. Only applies when lineup is posted.
    order_factor = 1.0
    if batting_order and 1 <= batting_order <= 9:
        # Per-spot multiplier centered at 1.0 (~ #5 hitter is league avg PA).
        ORDER_FACTORS = {1: 1.10, 2: 1.07, 3: 1.05, 4: 1.02, 5: 1.00,
                         6: 0.97, 7: 0.94, 8: 0.91, 9: 0.88}
        order_factor = ORDER_FACTORS[batting_order]
        notes.append(f"batting #{batting_order} x{order_factor:.2f} (PA adj)")

    # Vegas implied team total — best market signal for run scoring environment.
    # League avg implied total ~ 4.5 R/G. Scale projection by sqrt of ratio so
    # it's directional but not dominant. Capped ±15%.
    vegas_factor = 1.0
    if implied_team_total and implied_team_total > 0:
        vegas_factor = (implied_team_total / 4.5) ** 0.55
        vegas_factor = max(0.85, min(vegas_factor, 1.18))
        notes.append(f"Vegas implied {implied_team_total:.1f} R x{vegas_factor:.2f}")
        # Vegas already prices in opposing SP quality. To avoid double-counting,
        # dampen sp_factor toward 1.0 by 50% when Vegas data is present.
        if sp_factor != 1.0:
            sp_factor = 1.0 + (sp_factor - 1.0) * 0.5
            # Also relax the floor since Vegas now carries half the matchup weight.
            sp_factor = max(0.75, min(sp_factor, 1.25))

    # Opposing bullpen factor (whole-staff ERA proxy — pen drives ~35% of innings).
    # Magnitude small because it's already entangled with sp_factor.
    bullpen_factor = 1.0
    if opp_bullpen_era and opp_bullpen_era > 0:
        bullpen_factor = (opp_bullpen_era / 4.20) ** 0.30
        bullpen_factor = max(0.93, min(bullpen_factor, 1.08))
        notes.append(f"opp bullpen ERA {opp_bullpen_era:.2f} x{bullpen_factor:.2f}")

    # Handedness platoon factor: opposite-hand matchup is +5% (well-documented
    # ~30 pt wOBA advantage); same-hand is -5%; switch hitters always opposite.
    platoon_factor = 1.0
    if bats and opp_throws and bats in ("L", "R", "S") and opp_throws in ("L", "R"):
        if bats == "S":
            platoon_factor = 1.03   # switch hitters always have platoon advantage
        elif bats != opp_throws:
            platoon_factor = 1.05   # opposite hand
        else:
            platoon_factor = 0.95   # same hand
        notes.append(f"vs {opp_throws}HP ({bats}H) x{platoon_factor:.2f}")

    # Rolling xwOBA factor — true-talent shift signal from last 14 days vs season.
    # xwOBA is descriptive (luck-stripped) so divergence is real skill-trajectory.
    # Capped ±8% to stay conservative — small samples in 14d still noisy.
    rolling_factor = 1.0
    if rolling_xwoba and season_xwoba and season_xwoba > 0.10:
        ratio = rolling_xwoba / season_xwoba
        rolling_factor = ratio ** 0.45
        rolling_factor = max(0.92, min(rolling_factor, 1.10))
        notes.append(f"rolling xwOBA {rolling_xwoba:.3f} vs szn {season_xwoba:.3f} x{rolling_factor:.2f}")

    proj = base_pg * sp_factor * qoc_factor * park_factor * order_factor * vegas_factor * bullpen_factor * platoon_factor * rolling_factor
    pitfalls: list[str] = []
    if games_14 < 7:
        pitfalls.append(f"Small sample — only {int(games_14)} G in last 14d")
    if sp_factor < 0.85:
        pitfalls.append("Tough opposing SP (high K%, low ERA)")
    if brl and brl < 4.5:
        pitfalls.append(f"Below-avg barrel rate ({brl:.1f}% vs lg ~6.5%)")
    if hh and hh < 32:
        pitfalls.append(f"Low hard-hit% ({hh:.0f}% vs lg ~38%) — quality of contact lagging")
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
            "park_factor": round(park_factor, 3),
            "park_venue": (park or {}).get("venue") if park else None,
            "order_factor": round(order_factor, 3),
            "batting_order": batting_order,
            "vegas_factor": round(vegas_factor, 3),
            "implied_team_total": implied_team_total,
            "bullpen_factor": round(bullpen_factor, 3),
            "opp_bullpen_era": opp_bullpen_era,
            "platoon_factor": round(platoon_factor, 3),
            "bats": bats,
            "vs_throws": opp_throws,
            "rolling_factor": round(rolling_factor, 3),
            "rolling_xwoba": rolling_xwoba,
            "season_xwoba": season_xwoba,
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
    rolling_xwoba: float | None = None,
    season_xwoba: float | None = None,
) -> Projection:
    last7 = mlb_api.player_stats(pid, group="pitching", season=season, last_n_days=7)
    last14 = mlb_api.player_stats(pid, group="pitching", season=season, last_n_days=14)
    seasn = mlb_api.player_stats(pid, group="pitching", season=season)

    base = LEAGUE_AVG_SP_POINTS_PER_START
    notes: list[str] = []

    starts_7 = _safe_float(last7.get("gamesStarted"))
    starts_14 = _safe_float(last14.get("gamesStarted"))
    starts_season = _safe_float(seasn.get("gamesStarted"))
    ps_l7 = _per_start_pitcher_points(last7) if starts_7 >= 1 else None
    ps_l14 = _per_start_pitcher_points(last14) if starts_14 >= 1 else None
    ps_season = _per_start_pitcher_points(seasn) if starts_season >= 3 else None

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
        except Exception:
            pass

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
        STATCAST_WEIGHT = 0.15 if form_tag in ("HOT", "COLD") else 0.35
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
    vegas_factor = 1.0
    if opp_implied_total and opp_implied_total > 0:
        vegas_factor = (4.5 / opp_implied_total) ** 0.55
        vegas_factor = max(0.85, min(vegas_factor, 1.18))
        notes.append(f"opp Vegas {opp_implied_total:.1f} R x{vegas_factor:.2f}")
        # opp_factor (opponent runs/game) overlaps with Vegas implied; dampen.
        if opp_factor != 1.0:
            opp_factor = 1.0 + (opp_factor - 1.0) * 0.5
            opp_factor = max(0.85, min(opp_factor, 1.20))

    # Rolling xwOBA-against — pitcher's recent suppressed-contact form vs season.
    # Inverted: lower rolling xwOBA = better pitcher = higher projection.
    rolling_factor = 1.0
    if rolling_xwoba and season_xwoba and season_xwoba > 0.10:
        ratio = season_xwoba / rolling_xwoba   # invert
        rolling_factor = ratio ** 0.45
        rolling_factor = max(0.92, min(rolling_factor, 1.10))
        notes.append(f"rolling xwOBA-agst {rolling_xwoba:.3f} vs szn {season_xwoba:.3f} x{rolling_factor:.2f}")

    proj = base * opp_factor * qoc_factor * park_factor * vegas_factor * rolling_factor
    pitfalls: list[str] = []
    # SPs typically start every ~5 days, so 2-3 GS in 14d is the norm. Only
    # flag truly tiny samples (1 or 0 starts) — that's where projection noise
    # actually dominates.
    if starts_14 < 2 and _safe_float(seasn.get("gamesStarted")) < 4:
        pitfalls.append(f"Tiny sample — {int(starts_14)} 14d GS, {int(_safe_float(seasn.get('gamesStarted')))} season")
    if opp_factor > 1.15:
        pitfalls.append("Hot offensive opponent (high R/G)")
    if brl_a and brl_a > 8:
        pitfalls.append(f"Vulnerable to hard contact (brl-allowed {brl_a:.1f}%)")
    if xera and xera > 4.75:
        pitfalls.append(f"Underlying xERA {xera:.2f} — luck-adjusted line is rough")
    qoc_tier = _qoc_tier_pitcher(brl_a or 0, xera or 0)
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
            "park_factor": round(park_factor, 3),
            "park_venue": (park or {}).get("venue") if park else None,
            "vegas_factor": round(vegas_factor, 3),
            "opp_implied_total": opp_implied_total,
            "throws": throws,
            "rolling_factor": round(rolling_factor, 3),
            "rolling_xwoba": rolling_xwoba,
            "season_xwoba": season_xwoba,
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
    # Calibration round 2: HOT bias was still ~+5 after first tuning. The
    # season bucket (50-80 games) was drowning the recent signal. Pushed L3
    # recency to 5.0 and capped the season-prior bucket at ~14 game-equivalents
    # so it acts as an anchor, not a tide.
    _add(buckets, pg3, g3, 5.0)
    if pg7 is not None and g7 > 0:
        if pg3 is not None and g3 > 0 and g7 > g3:
            _add(buckets, (pg7 * g7 - pg3 * g3) / (g7 - g3), g7 - g3, 2.2)
        elif pg3 is None:
            _add(buckets, pg7, g7, 2.2)
    if pg14 is not None and g14 > 0:
        if pg7 is not None and g7 > 0 and g14 > g7:
            _add(buckets, (pg14 * g14 - pg7 * g7) / (g14 - g7), g14 - g7, 1.2)
        elif pg7 is None:
            _add(buckets, pg14, g14, 1.2)
    if pgs is not None and gs > 0:
        if pg14 is not None and g14 > 0 and gs > g14:
            prior_g = min(gs - g14, 14)   # cap so season doesn't drown recency
            _add(buckets, (pgs * gs - pg14 * g14) / (gs - g14), prior_g, 0.45)
        elif pg14 is None:
            _add(buckets, pgs, min(gs, 14), 0.45)

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


# Two-tier cache: in-memory (instant) + disk (survives redeploys/restarts).
# Projections are based on rolling stat windows that only meaningfully change
# after games complete overnight, so we hold them for 6h.
_PROJ_CACHE: dict[tuple, tuple[float, list]] = {}
_PROJ_TTL_SEC = 6 * 3600


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


def project_slate_cached(
    d: Date, *, team_filter: set[int] | None = None, force_refresh: bool = False
) -> list["Projection"]:
    """Memoized per date (full slate). team_filter is applied downstream so
    projections-tab and draft-tab share one cache entry — first hit pays the
    ~50 MLB API calls, the rest are instant. 6h TTL, persisted to disk so
    redeploys don't force a recompute."""
    key = (d.isoformat(), None)
    now = time.time()
    full: list["Projection"] | None = None
    if not force_refresh:
        cached = _PROJ_CACHE.get(key)
        if cached is not None and (now - cached[0]) < _PROJ_TTL_SEC:
            full = cached[1]
        if full is None:
            path = _proj_disk_path(key)
            try:
                if os.path.exists(path) and (now - os.path.getmtime(path)) < _PROJ_TTL_SEC:
                    with open(path) as f:
                        raw = json.load(f)
                    full = [_proj_from_dict(x) for x in raw]
                    _PROJ_CACHE[key] = (os.path.getmtime(path), full)
            except Exception:
                full = None
    if full is None:
        full = project_slate(d, team_filter=None)
        _PROJ_CACHE[key] = (now, full)
        try:
            from .disk_cache import CACHE_DIR
            os.makedirs(CACHE_DIR, exist_ok=True)
            path = _proj_disk_path(key)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump([_proj_to_dict(p) for p in full], f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            pass
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

    # Build matchup map: team_id -> {opp, opp_sp, park (run_env, hr_factor)}
    matchups: dict[int, dict] = {}
    probable_sps: dict[int, dict] = {}  # sp_id -> {team_id, opp_team_id, park}
    for g in games:
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
        if home_team and away_team:
            matchups[home_team] = {"opp": away_team, "opp_sp": away_sp, "park": park}
            matchups[away_team] = {"opp": home_team, "opp_sp": home_sp, "park": park}
        if home_sp:
            probable_sps[home_sp] = {
                "team_id": home_team, "opp_team_id": away_team, "park": park,
                "name": (home.get("probablePitcher") or {}).get("fullName"),
            }
        if away_sp:
            probable_sps[away_sp] = {
                "team_id": away_team, "opp_team_id": home_team, "park": park,
                "name": (away.get("probablePitcher") or {}).get("fullName"),
            }

    pool = mlb_api.players_in_slate(d)
    # Pull lineups for batting order info (None if lineup not yet posted).
    try:
        lineups = mlb_api.lineups_by_date(d)
    except Exception:
        lineups = {}

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

    # Bullpen quality: season bullpen ERA per team. ~30 API calls cached for the day.
    bullpen_era = _bullpen_era_by_team(season)

    # Handedness for all players — single bulk call.
    handedness = mlb_api.handedness_by_player(season)

    # Rolling 14-day xwOBA from Baseball Savant — true-talent shift signal.
    # Compared to season xwOBA, divergence indicates hot/cold underlying skill.
    from datetime import timedelta as _td
    rolling_start = (d - _td(days=14)).isoformat()
    rolling_end = (d - _td(days=1)).isoformat()
    try:
        rolling_batter = savant.batter_expected_range(season, rolling_start, rolling_end)
        rolling_pitcher = savant.pitcher_expected_range(season, rolling_start, rolling_end)
        season_batter = savant.batter_expected(season)
        season_pitcher = savant.pitcher_expected(season)
    except Exception:
        rolling_batter, rolling_pitcher, season_batter, season_pitcher = {}, {}, {}, {}

    projections: list[Projection] = []

    # Pitchers — only project the probable starters; relievers aren't draftable as SP
    for sp_id, info in probable_sps.items():
        if team_filter is not None and info["team_id"] not in team_filter:
            continue
        sp_throws = (handedness.get(sp_id) or {}).get("throws")
        rolling_pitch_x = _safe_float((rolling_pitcher.get(sp_id) or {}).get("est_woba")) or None
        season_pitch_x = _safe_float((season_pitcher.get(sp_id) or {}).get("est_woba")) or None
        projections.append(project_pitcher(
            sp_id, info["name"] or pool.get(sp_id, {}).get("name", "?"),
            team_id=info["team_id"], season=season,
            opponent_team_id=info["opp_team_id"],
            park=info.get("park"),
            opp_implied_total=team_totals.get(info["opp_team_id"]),
            throws=sp_throws,
            rolling_xwoba=rolling_pitch_x,
            season_xwoba=season_pitch_x,
        ))

    # Hitters — everyone non-pitcher in the slate roster pool
    for pid, meta in pool.items():
        if meta.get("positionType") == "Pitcher":
            continue
        if team_filter is not None and meta.get("teamId") not in team_filter:
            continue
        team_id = meta.get("teamId")
        m = matchups.get(team_id or 0, {})
        bo = (lineups.get(pid) or {}).get("batting_order")
        bats = (handedness.get(pid) or {}).get("bats")
        opp_sp = m.get("opp_sp")
        opp_throws = (handedness.get(opp_sp) or {}).get("throws") if opp_sp else None
        rolling_x = _safe_float((rolling_batter.get(pid) or {}).get("est_woba")) or None
        season_x = _safe_float((season_batter.get(pid) or {}).get("est_woba")) or None
        projections.append(project_hitter(
            pid, meta["name"],
            team_id=team_id,
            position=meta.get("position"),
            season=season,
            opposing_sp_id=opp_sp,
            park=m.get("park"),
            batting_order=bo,
            implied_team_total=team_totals.get(team_id) if team_id else None,
            opp_bullpen_era=bullpen_era.get(m.get("opp")) if m.get("opp") else None,
            bats=bats,
            opp_throws=opp_throws,
            rolling_xwoba=rolling_x,
            season_xwoba=season_x,
        ))

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
