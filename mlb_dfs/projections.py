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

import time
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Iterable

from . import mlb_api, savant
from .scoring import HITTER_POINTS, PITCHER_POINTS

LEAGUE_AVG_HITTER_POINTS_PER_GAME = 6.5
LEAGUE_AVG_SP_POINTS_PER_START = 11.0

# League-median Statcast benchmarks for the multiplier (rough 2024-25 medians).
LG_BARREL_PCT_HITTER = 6.5      # %
LG_HARDHIT_PCT_HITTER = 38.0
LG_BARREL_PCT_ALLOWED = 6.5
LG_XWOBA_AGAINST = 0.310


def _qoc_multiplier_hitter(qoc: dict | None) -> tuple[float, list[str]]:
    """Tiny multiplier on top of the base projection from quality-of-contact.
    Capped at ±15% so it can't dominate small-sample noise."""
    if not qoc:
        return 1.0, []
    notes = []
    factor = 1.0
    brl = _safe_float(qoc.get("brl_percent"))
    hh  = _safe_float(qoc.get("ev95percent"))
    if brl:
        delta = (brl - LG_BARREL_PCT_HITTER) / LG_BARREL_PCT_HITTER  # e.g. +0.5 = 50% above lg
        factor *= 1.0 + max(-0.10, min(delta * 0.15, 0.10))
        notes.append(f"barrel {brl:.1f}%")
    if hh:
        delta = (hh - LG_HARDHIT_PCT_HITTER) / LG_HARDHIT_PCT_HITTER
        factor *= 1.0 + max(-0.06, min(delta * 0.10, 0.06))
        notes.append(f"hard-hit {hh:.0f}%")
    return max(0.85, min(factor, 1.15)), notes


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


def _qoc_multiplier_pitcher(qoc: dict | None, expected: dict | None) -> tuple[float, list[str]]:
    """Pitcher version — barrel% / hard-hit% allowed move things the OTHER way:
    high barrel% allowed = worse pitcher = lower projection."""
    factor = 1.0
    notes = []
    if qoc:
        brl_a = _safe_float(qoc.get("brl_percent"))
        if brl_a:
            delta = (brl_a - LG_BARREL_PCT_ALLOWED) / LG_BARREL_PCT_ALLOWED
            factor *= 1.0 - max(-0.10, min(delta * 0.15, 0.15))  # inverted
            notes.append(f"brl-allowed {brl_a:.1f}%")
    if expected:
        xwoba = _safe_float(expected.get("est_woba"))
        if xwoba:
            delta = (xwoba - LG_XWOBA_AGAINST) / LG_XWOBA_AGAINST
            factor *= 1.0 - max(-0.10, min(delta * 0.20, 0.15))
            notes.append(f"xwOBA {xwoba:.3f}")
    return max(0.80, min(factor, 1.20)), notes


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


def project_hitter(
    pid: int,
    name: str,
    *,
    team_id: int | None,
    position: str | None,
    season: int,
    opposing_sp_id: int | None,
) -> Projection:
    last14 = mlb_api.player_stats(pid, group="hitting", season=season, last_n_days=14)
    seasn = mlb_api.player_stats(pid, group="hitting", season=season)

    games_14 = _safe_float(last14.get("gamesPlayed"))
    base_pg = LEAGUE_AVG_HITTER_POINTS_PER_GAME
    notes: list[str] = []

    if games_14 >= 5:
        base_pg = _per_game_hitter_points(last14)
        notes.append(f"14d sample: {int(games_14)} G, {base_pg:.2f} pts/G")
    elif _safe_float(seasn.get("gamesPlayed")) >= 10:
        base_pg = _per_game_hitter_points(seasn)
        notes.append(f"season fallback: {int(_safe_float(seasn.get('gamesPlayed')))} G, {base_pg:.2f} pts/G")
    else:
        notes.append("no sample, league average")

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
    qoc_factor, qoc_notes = _qoc_multiplier_hitter(qoc)
    if qoc_notes:
        notes.append(f"qoc x{qoc_factor:.2f} ({', '.join(qoc_notes)})")

    proj = base_pg * sp_factor * qoc_factor
    brl = _safe_float((qoc or {}).get("brl_percent"))
    hh = _safe_float((qoc or {}).get("ev95percent"))
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
            "pitfalls": pitfalls,
            "sample_games_14d": int(games_14),
            "barrel_pct": _safe_float((qoc or {}).get("brl_percent")) or None,
            "hardhit_pct": _safe_float((qoc or {}).get("ev95percent")) or None,
            "sweet_spot_pct": _safe_float((qoc or {}).get("anglesweetspotpercent")) or None,
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
) -> Projection:
    last14 = mlb_api.player_stats(pid, group="pitching", season=season, last_n_days=14)
    seasn = mlb_api.player_stats(pid, group="pitching", season=season)

    base = LEAGUE_AVG_SP_POINTS_PER_START
    notes: list[str] = []

    starts_14 = _safe_float(last14.get("gamesStarted"))
    if starts_14 >= 1:
        base = _per_start_pitcher_points(last14)
        notes.append(f"14d sample: {int(starts_14)} GS, {base:.2f} pts/start")
    elif _safe_float(seasn.get("gamesStarted")) >= 2:
        base = _per_start_pitcher_points(seasn)
        notes.append(f"season fallback: {int(_safe_float(seasn.get('gamesStarted')))} GS, {base:.2f} pts/start")
    else:
        notes.append("no sample, league average")

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
    qoc_factor, qoc_notes = _qoc_multiplier_pitcher(qoc, expected)
    if qoc_notes:
        notes.append(f"qoc x{qoc_factor:.2f} ({', '.join(qoc_notes)})")

    proj = base * opp_factor * qoc_factor
    brl_a = _safe_float((qoc or {}).get("brl_percent")) or None
    hh_a = _safe_float((qoc or {}).get("ev95percent")) or None
    xera = _safe_float((expected or {}).get("xera")) or None
    xwoba_a = _safe_float((expected or {}).get("est_woba")) or None
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
            "pitfalls": pitfalls,
            "sample_starts_14d": int(starts_14),
            "xera": xera,
            "xwoba_against": xwoba_a,
            "barrel_pct_allowed": brl_a,
            "hardhit_pct_allowed": hh_a,
        },
        notes=notes,
    )


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


# In-memory cache for project_slate so repeated polls don't hammer MLB Stats API.
_PROJ_CACHE: dict[tuple, tuple[float, list]] = {}
_PROJ_TTL_SEC = 300  # 5 minutes


def project_slate_cached(d: Date, *, team_filter: set[int] | None = None) -> list["Projection"]:
    """Same as project_slate but memoizes per (date, team_filter) for 5 minutes.

    Projections are based on rolling stat windows + season averages — these
    don't move minute-to-minute, so a TTL cache makes draft polling cheap
    without staleness that matters in practice.
    """
    key = (d.isoformat(), tuple(sorted(team_filter)) if team_filter else None)
    now = time.time()
    cached = _PROJ_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _PROJ_TTL_SEC:
        return cached[1]
    projs = project_slate(d, team_filter=team_filter)
    _PROJ_CACHE[key] = (now, projs)
    return projs


def project_slate(d: Date, *, team_filter: set[int] | None = None) -> list[Projection]:
    """Project everyone playing today. Handles probable SPs + position players.

    If `team_filter` is provided, only project players from those team IDs (used
    when a draft is restricted to a subset of the day's games).
    """
    season = d.year
    games = mlb_api.schedule(d)

    # Build matchup map: team_id -> opposing team_id and opposing SP id
    matchups: dict[int, dict] = {}
    probable_sps: dict[int, dict] = {}  # sp_id -> {team_id, opp_team_id}
    for g in games:
        home = ((g.get("teams") or {}).get("home") or {})
        away = ((g.get("teams") or {}).get("away") or {})
        home_team = (home.get("team") or {}).get("id")
        away_team = (away.get("team") or {}).get("id")
        home_sp = (home.get("probablePitcher") or {}).get("id")
        away_sp = (away.get("probablePitcher") or {}).get("id")
        if home_team and away_team:
            matchups[home_team] = {"opp": away_team, "opp_sp": away_sp}
            matchups[away_team] = {"opp": home_team, "opp_sp": home_sp}
        if home_sp:
            probable_sps[home_sp] = {
                "team_id": home_team, "opp_team_id": away_team,
                "name": (home.get("probablePitcher") or {}).get("fullName"),
            }
        if away_sp:
            probable_sps[away_sp] = {
                "team_id": away_team, "opp_team_id": home_team,
                "name": (away.get("probablePitcher") or {}).get("fullName"),
            }

    pool = mlb_api.players_in_slate(d)
    projections: list[Projection] = []

    # Pitchers — only project the probable starters; relievers aren't draftable as SP
    for sp_id, info in probable_sps.items():
        if team_filter is not None and info["team_id"] not in team_filter:
            continue
        projections.append(project_pitcher(
            sp_id, info["name"] or pool.get(sp_id, {}).get("name", "?"),
            team_id=info["team_id"], season=season,
            opponent_team_id=info["opp_team_id"],
        ))

    # Hitters — everyone non-pitcher in the slate roster pool
    for pid, meta in pool.items():
        if meta.get("positionType") == "Pitcher":
            continue
        if team_filter is not None and meta.get("teamId") not in team_filter:
            continue
        m = matchups.get(meta.get("teamId") or 0, {})
        projections.append(project_hitter(
            pid, meta["name"],
            team_id=meta.get("teamId"),
            position=meta.get("position"),
            season=season,
            opposing_sp_id=m.get("opp_sp"),
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
