"""Dynasty valuation, multi-year prediction, and a trade analyzer.

Goal: a full dynasty system that's smarter than a static consensus list
(à la HarryKnowsBall) by layering OUR data on top of the FantraxHQ consensus:

  1. Consensus prior — FantraxHQ Roto rank (data/dynasty_top500.csv) gives a
     sane, market-calibrated starting value (it already bakes in production,
     pedigree, and rough age). We anchor to it so we're never wildly wrong.
  2. Statcast luck tilt — season xwOBA vs actual wOBA (hitters) / xERA vs ERA
     (pitchers) from Baseball Savant. Over-performers (wOBA > xwOBA) get
     trimmed (sell-high); under-performers get a bump (buy-low). This is the
     edge: the consensus reacts slowly to luck regression, our data doesn't.
  3. Explicit age curve — we project a year-by-year value path so dynasty
     value reflects remaining prime years, not just this season. A 23yo and
     a 31yo with the same talent get very different dynasty values.
  4. Position scarcity — C/SS/2B premium, 1B/DH/RP discount.

The trade analyzer sums dynasty value per side, adds a consolidation premium
(the best player in a deal is worth more than raw points in roster-capped
leagues), and surfaces win-now vs rebuild context from the age profiles.

Everything is cached: the consensus CSV is static; the Savant season pulls
are already cached 6-24h by savant.py.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date

from . import disk_cache, injuries, mlb_api, savant

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_CSV = os.path.join(_DATA_DIR, "dynasty_top500.csv")


def _norm(name: str) -> str:
    if not name:
        return ""
    d = unicodedata.normalize("NFD", name)
    a = "".join(c for c in d if not unicodedata.combining(c))
    # Drop suffixes/punct so "Ronald Acuna Jr." == "Ronald Acuña Jr"
    a = a.lower().replace(".", "").replace(",", "")
    for suf in (" jr", " sr", " ii", " iii", " iv"):
        if a.endswith(suf):
            a = a[: -len(suf)]
    return " ".join(a.split())


# ---- consensus prior --------------------------------------------------------

_CONSENSUS: dict[str, dict] | None = None


def _consensus() -> dict[str, dict]:
    """{normalized_name: {rank, name, pos, team, age}} from the FantraxHQ CSV.
    Rank is the Roto column (consensus); the Points column is pts-league skewed."""
    global _CONSENSUS
    if _CONSENSUS is not None:
        return _CONSENSUS
    out: dict[str, dict] = {}
    try:
        with open(_CSV, newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("Player") or "").strip()
                if not name:
                    continue
                try:
                    rank = int((row.get("Roto") or "").strip() or 0)
                except ValueError:
                    rank = 0
                if not rank:
                    continue
                try:
                    age = float((row.get("Age") or "").strip() or 0) or None
                except ValueError:
                    age = None
                out[_norm(name)] = {
                    "rank": rank,
                    "name": name,
                    "pos": (row.get("Pos.") or "").strip().upper(),
                    "team": (row.get("Team") or "").strip(),
                    "age": age,
                    "level": (row.get("Level") or "").strip(),
                    "eta": (row.get("ETA") or "").strip(),
                }
    except Exception:
        pass
    _CONSENSUS = out
    return out


# ---- model components --------------------------------------------------------

# Rank → base value. Exponential decay so #1 is worth a lot more than #50, and
# #50 a lot more than #300 — matches how dynasty trade markets actually price
# the top of the board (steep at the top, flat in the deep minors).
# value(rank) = 1000 * exp(-k*(rank-1)). With k=0.0108:
#   #1 ≈ 1000 · #25 ≈ 765 · #50 ≈ 589 · #100 ≈ 343 · #200 ≈ 117 · #300 ≈ 40 · #500 ≈ 5
# i.e. value roughly halves every ~64 ranks.
_DECAY_K = 0.0108


def _rank_value(rank: int) -> float:
    return 1000.0 * math.exp(-_DECAY_K * max(rank - 1, 0))


# Age curves — production multiplier vs peak (=1.0). Hitters peak ~27, pitchers
# ~26 with steeper decline (injury attrition). Used to project future years.
def _age_factor(age: float, role: str) -> float:
    if not age or age <= 0:
        return 1.0
    if role == "pitcher":
        peak = 27.0
        # Softened (v1.3): 0.040/yr decline was brutal — it buried established
        # aces (Skubal at 29 → ×0.88). Aces hold dynasty value into the early
        # 30s; 0.028/yr is closer to how the market prices them.
        if age <= peak:
            return max(0.85, 1.0 - 0.015 * (peak - age))
        return max(0.40, 1.0 - 0.028 * (age - peak))
    # hitter
    peak = 27.0
    if age <= peak:
        return max(0.86, 1.0 - 0.018 * (peak - age))
    return max(0.45, 1.0 - 0.028 * (age - peak))


# Position scarcity multiplier on dynasty value. Catcher + premium infield are
# scarce; corner/DH/RP are replaceable.
_POS_SCARCITY = {
    "C": 1.14, "SS": 1.10, "2B": 1.06, "3B": 1.02, "CF": 1.03,
    "OF": 1.00, "LF": 0.99, "RF": 0.99, "1B": 0.95, "DH": 0.90,
    "SP": 1.00, "RP": 0.62, "P": 0.90, "TWP": 1.12,  # two-way premium
}


def _pos_scarcity(pos: str) -> float:
    if not pos:
        return 1.0
    # Pos can be multi like "2B,OF" or "SS/3B" — take the most scarce listed.
    parts = [p.strip().upper() for p in pos.replace("/", ",").split(",") if p.strip()]
    if not parts:
        return 1.0
    return max(_POS_SCARCITY.get(p, 1.0) for p in parts)


# ---- Statcast luck tilt ------------------------------------------------------

_LUCK_CACHE: dict[int, dict[str, dict]] = {}


# Minimum sample before a luck tilt is trusted — early-season xwOBA/xERA on
# 30 PA is noise (this is what made two-way Ohtani's 0.73-ERA blip drive his
# value). PA = plate appearances (hitters) / batters faced (pitchers).
_MIN_PA_HIT = 120
_MIN_PA_PIT = 80   # ≈ 20 IP


def _statcast_luck(season: int) -> dict[str, dict]:
    """{normalized_name: {"hitter": {...}, "pitcher": {...}}}. Stored per-role
    (not merged) so two-way players don't have their bat value clobbered by a
    tiny-sample pitching line — dynasty_value picks the record matching the
    player's role. Records below the sample gate are dropped."""
    if season in _LUCK_CACHE:
        return _LUCK_CACHE[season]
    out: dict[str, dict] = {}

    def _flip(lastfirst: str) -> str:
        if "," in lastfirst:
            last, first = [s.strip() for s in lastfirst.split(",", 1)]
            return f"{first} {last}"
        return lastfirst

    def _f(row, key):
        try:
            v = row.get(key)
            return float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    try:
        for row in savant.batter_expected(season).values():
            nm = _norm(_flip(row.get("last_name, first_name", "")))
            if not nm:
                continue
            pa = _f(row, "pa") or 0
            woba, xwoba = _f(row, "woba"), _f(row, "est_woba")
            if woba and xwoba and pa >= _MIN_PA_HIT:
                out.setdefault(nm, {})["hitter"] = {
                    "pa": int(pa), "woba": woba, "xwoba": xwoba,
                    "delta": round(xwoba - woba, 3),
                }
    except Exception:
        pass
    try:
        for row in savant.pitcher_expected(season).values():
            nm = _norm(_flip(row.get("last_name, first_name", "")))
            if not nm:
                continue
            pa = _f(row, "pa") or 0
            era, xera = _f(row, "era"), _f(row, "xera")
            if era and xera and pa >= _MIN_PA_PIT:
                out.setdefault(nm, {})["pitcher"] = {
                    "pa": int(pa), "era": era, "xera": xera,
                    "delta": round(era - xera, 2),
                }
    except Exception:
        pass
    _LUCK_CACHE[season] = out
    return out


def _luck_multiplier(rec: dict | None, role: str) -> tuple[float, str]:
    """Statcast luck → a SMALL value tilt (±5%) + a note. v2 audit fixes:
      - role-aware: two-way/position players use the hitter record, pitchers
        the pitcher record (no more bat value clobbered by a tiny IP sample)
      - ±5% magnitude (was ±10%) — for dynasty, luck is a buy-low/sell-high
        nudge, not a big revaluation
      - elite underlying skill is NOT flagged 'sell-high': an ace outrunning
        an already-elite xERA is still an ace, so we don't ding him
    """
    if not rec:
        return 1.0, ""
    if role == "pitcher":
        p = rec.get("pitcher")
        if not p:
            return 1.0, ""
        d = p["delta"]  # era - xera; + = unlucky (ERA worse than skill → improvement)
        mult = 1.0 + max(-0.05, min(d / 0.75 * 0.05, 0.05))
        if d >= 0.40:
            return mult, f"buy-low: {p['era']:.2f} ERA vs {p['xera']:.2f} xERA (better arm than results)"
        if d <= -0.40:
            if p["xera"] <= 3.20:
                # Outperforming, but the underlying skill is elite — not a
                # 'sell-high, he's bad coming' situation. Don't alarm or penalize.
                return max(mult, 0.99), f"elite {p['xera']:.2f} xERA (ERA {p['era']:.2f} slightly over-performing)"
            return mult, f"sell-high: {p['era']:.2f} ERA vs {p['xera']:.2f} xERA (results beat the arm)"
        return mult, ""
    # hitter / two-way / position player
    h = rec.get("hitter")
    if not h:
        return 1.0, ""
    d = h["delta"]  # xwoba - woba; + = unlucky/buy-low
    mult = 1.0 + max(-0.05, min(d / 0.030 * 0.05, 0.05))
    if d >= 0.015:
        return mult, f"buy-low: {h['woba']:.3f} wOBA vs {h['xwoba']:.3f} xwOBA (better bat than results)"
    if d <= -0.015:
        if h["xwoba"] >= 0.360:
            return max(mult, 0.99), f"elite {h['xwoba']:.3f} xwOBA (wOBA {h['woba']:.3f} slightly over-performing)"
        return mult, f"sell-high: {h['woba']:.3f} wOBA vs {h['xwoba']:.3f} xwOBA (results beat the bat)"
    return 1.0, ""


# ---- skill-level talent model (OUR data drives the board) -------------------
# Beyond the consensus prior, we compute each player's true-talent SKILL from
# the full Statcast slew (all bulk-cached, no per-player calls):
#   hitters  → xwOBA, xSLG, xBA, barrel%, hard-hit%, sweet-spot%
#   pitchers → xERA, xwOBA-against, barrel%-allowed, hard-hit%-allowed
# Each metric is z-scored against a league baseline, combined into a weighted
# composite, then the whole pool is ranked and mapped to the same 0-1000 scale
# as the consensus. base_value blends the two — so a guy the market underrates
# on current skill rises, and an overrated name falls, on OUR numbers.

_SKILL_CACHE: dict[int, dict[str, dict]] = {}

# ---- minor-league prospect stats --------------------------------------------
# Prospects have no MLB Statcast, so they'd fall back to 100% consensus. We
# pull their MiLB production (the CSV tells us their level → one sportId, so
# it's 1 search + 1 stats call each, parallelized + cached 24h on disk) and
# build an MLB-equivalent skill read: production haircut by level, plus the
# single biggest prospect signal — age relative to level (an 18yo holding his
# own in AA is far more valuable than a 24yo doing the same).

_LEVEL_SPORTID = {"AAA": 11, "AA": 12, "A+": 13, "A": 14, "RK": 16, "ROOKIE": 16,
                  "CPX": 16, "DSL": 16, "MLB": 1}
# MLB-equivalence haircut on MiLB production.
_LEVEL_FACTOR = {"MLB": 1.0, "AAA": 0.80, "AA": 0.62, "A+": 0.45, "A": 0.32,
                 "RK": 0.20, "ROOKIE": 0.20, "CPX": 0.18, "DSL": 0.15}
# Typical age for the level — young-for-level is the dominant prospect signal.
_LEVEL_EXP_AGE = {"MLB": 27, "AAA": 24, "AA": 23, "A+": 22, "A": 21,
                  "RK": 19, "ROOKIE": 19, "CPX": 19, "DSL": 18}


@disk_cache.cached_disk(7 * 86400, namespace="mlb_player_id")
def _resolve_id(name: str) -> int | None:
    """Name → MLB Stats API player id (cached 7d). First search hit."""
    try:
        data = mlb_api._get("/people/search", params={"names": name})
        ppl = data.get("people", []) or []
        return ppl[0]["id"] if ppl else None
    except Exception:
        return None


_SPORTID_LEVEL = {1: "MLB", 11: "AAA", 12: "AA", 13: "A+", 14: "A", 16: "RK"}


@disk_cache.cached_disk(86400, namespace="durability_yby")
def _games_by_year(pid: int, group: str) -> dict:
    """{season_year: games_played} at MLB level (sportId 1), from yearByYear.
    For pitchers we also capture starts. One call, cached 24h."""
    out: dict[str, dict] = {}
    try:
        data = mlb_api._get(
            f"/people/{pid}/stats",
            params={"stats": "yearByYear", "group": group, "sportId": 1},
        )
        for s in data.get("stats", []) or []:
            for sp in s.get("splits", []) or []:
                yr = sp.get("season")
                st = sp.get("stat", {}) or {}
                if yr:
                    out[yr] = {
                        "games": int(float(st.get("gamesPlayed") or 0)),
                        "starts": int(float(st.get("gamesStarted") or 0)),
                        "ip": float(st.get("inningsPitched") or 0) if st.get("inningsPitched") else 0,
                    }
    except Exception:
        pass
    return out


_DURABILITY_CACHE: dict[int, dict[str, dict]] = {}


def _durability(season: int) -> dict[str, dict]:
    """{normalized_name: {mult, note, avg}} — multi-year durability tendency
    from games played over the two prior completed seasons. This turns the
    injury signal from a current-status snapshot into a real track record:
    a chronically banged-up player (few games/season) gets a standing dynasty
    discount even when healthy today. Neutral (1.0) for players without 2 yrs
    of MLB history (prospects, recent call-ups) — we can't assess them."""
    if season in _DURABILITY_CACHE:
        return _DURABILITY_CACHE[season]
    prior_years = [str(season - 1), str(season - 2)]
    targets = []
    for nname, cons in _consensus().items():
        # Skip clear prospects (no MLB track record to assess).
        if (cons.get("level") or "").strip().upper() not in ("", "MLB"):
            continue
        targets.append((nname, cons))

    def _one(item):
        nname, cons = item
        pid = _resolve_id(cons["name"])
        if not pid:
            return None
        role = _role_for(cons["pos"])
        group = "pitching" if role == "pitcher" else "hitting"
        gby = _games_by_year(pid, group)
        if role == "pitcher":
            starts = [gby[y]["starts"] for y in prior_years if y in gby and gby[y]["starts"] > 0]
            if len(starts) < 1:
                return None
            avg = sum(starts) / len(starts)
            # 30+ starts = iron man; scale down for missed time
            if avg >= 28: mult, note = 1.0, ""
            elif avg >= 24: mult, note = 0.98, f"~{avg:.0f} starts/yr"
            elif avg >= 18: mult, note = 0.95, f"durability risk (~{avg:.0f} starts/yr)"
            else: mult, note = 0.91, f"injury-prone (~{avg:.0f} starts/yr)"
        else:
            games = [gby[y]["games"] for y in prior_years if y in gby and gby[y]["games"] > 0]
            if len(games) < 1:
                return None
            avg = sum(games) / len(games)
            if avg >= 145: mult, note = 1.0, ""
            elif avg >= 130: mult, note = 0.98, f"~{avg:.0f} G/yr"
            elif avg >= 110: mult, note = 0.95, f"durability risk (~{avg:.0f} G/yr)"
            else: mult, note = 0.91, f"injury-prone (~{avg:.0f} G/yr)"
        return (nname, {"mult": mult, "note": note, "avg": round(avg, 1)})

    out: dict[str, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(_one, targets):
                if r:
                    out[r[0]] = r[1]
    except Exception as e:
        logging.warning("durability build failed: %s", e)
    _DURABILITY_CACHE[season] = out
    return out


@disk_cache.cached_disk(86400, namespace="milb_line")
def _milb_line(pid: int, season: int, sportid: int, group: str) -> dict:
    """One season stat line for a player at a given level. Cached 24h."""
    try:
        data = mlb_api._get(
            f"/people/{pid}/stats",
            params={"stats": "season", "season": season, "group": group, "sportId": sportid},
        )
        for s in data.get("stats", []) or []:
            sp = s.get("splits", []) or []
            if sp:
                return sp[0].get("stat", {}) or {}
    except Exception:
        pass
    return {}


def _milb_best_level(pid: int, season: int, group: str) -> tuple[str, dict] | None:
    """Find a prospect's PRIMARY current level by querying each level and
    taking the one with the most PA/BF. Robust to promotions + stale CSV
    levels (a 'AA' prospect who's now in AAA or the majors is caught). Each
    per-level call is disk-cached, so this is cheap after the first build."""
    key = "plateAppearances" if group == "hitting" else "battersFaced"
    best = None  # (pa, level, stat)
    for sid, level in _SPORTID_LEVEL.items():
        stat = _milb_line(pid, season, sid, group)
        if not stat:
            continue
        try:
            pa = float(stat.get(key) or 0)
        except (TypeError, ValueError):
            pa = 0
        if pa > 0 and (best is None or pa > best[0]):
            best = (pa, level, stat)
    if best is None:
        return None
    return best[1], best[2]


def _prospect_skill_z(level: str, role: str, age: float | None, stat: dict) -> tuple[float, dict] | None:
    """MLB-equivalent skill z for a prospect from one MiLB line. None if the
    sample is too small to trust."""
    lf = _LEVEL_FACTOR.get(level, 0.4)
    exp_age = _LEVEL_EXP_AGE.get(level, 22)
    age_bonus = ((exp_age - age) * 0.10) if age else 0.0  # +0.10z per year young-for-level
    if role == "hitter":
        pa = float(stat.get("plateAppearances") or 0)
        if pa < 40:
            return None
        try:
            ops = float(stat.get("ops") or 0)
        except (TypeError, ValueError):
            ops = 0
        if ops <= 0:
            return None
        prod_z = (ops - 0.700) / 0.130  # MiLB OPS baseline ~.700, sd ~.130
        comps = {"level": level, "pa": int(pa), "ops": round(ops, 3),
                 "avg": stat.get("avg"), "hr": stat.get("homeRuns"),
                 "age_vs_level": round(exp_age - age, 1) if age else None}
    else:
        bf = float(stat.get("battersFaced") or 0)
        ip = float(stat.get("inningsPitched") or 0) if stat.get("inningsPitched") else 0
        if bf < 40:
            return None
        try:
            k = float(stat.get("strikeOuts") or 0); bb = float(stat.get("baseOnBalls") or 0)
            era = float(stat.get("era") or 0) if stat.get("era") else None
        except (TypeError, ValueError):
            return None
        kbb = (k - bb) / bf  # K-BB rate
        prod_z = (kbb - 0.13) / 0.07  # MiLB K-BB% baseline ~13%, sd ~7%
        if era is not None:
            prod_z += max(-1.0, min((3.80 - era) / 1.20, 1.0)) * 0.4  # ERA tilt
        comps = {"level": level, "bf": int(bf), "era": era,
                 "kbb_pct": round(kbb * 100, 1),
                 "age_vs_level": round(exp_age - age, 1) if age else None}
    skill_z = lf * prod_z + age_bonus
    # Cap so a dominant low-level line can't outrank MLB MVPs outright.
    skill_z = max(-1.5, min(skill_z, 1.6))
    return round(skill_z, 3), comps


def _prospect_skills(season: int) -> dict[str, dict]:
    """{normalized_name: {role, skill_z, comps, is_prospect}} for consensus
    players carrying a minor-league Level. Parallelized + per-call disk-cached."""
    targets = []
    for nname, cons in _consensus().items():
        lvl = (cons.get("level") or "").strip().upper()
        if not lvl or lvl == "MLB":
            continue  # MLB players handled by the Statcast path
        targets.append((nname, cons, lvl))

    def _one(item):
        nname, cons, _csv_lvl = item
        pid = _resolve_id(cons["name"])
        if not pid:
            return None
        role = _role_for(cons["pos"])
        group = "pitching" if role == "pitcher" else "hitting"
        # Detect their ACTUAL current level (handles promotions / stale CSV).
        found = _milb_best_level(pid, season, group)
        if not found:
            return None
        level, stat = found
        res = _prospect_skill_z(level, role, cons.get("age"), stat)
        if not res:
            return None
        z, comps = res
        return (nname, {"role": role, "skill_z": z, "comps": comps,
                        "is_prospect": True, "actual_level": level})

    out: dict[str, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(_one, targets):
                if r:
                    out[r[0]] = r[1]
    except Exception as e:
        logging.warning("prospect skill build failed: %s", e)
    return out

# (mean, sd) baselines for z-scoring. Tuned to ~2026 mid-season qualified pops.
_HIT_BASE = {
    "xwoba": (0.315, 0.040), "xslg": (0.410, 0.075), "xba": (0.245, 0.025),
    "barrel": (8.5, 4.2), "hardhit": (40.0, 6.5), "sweetspot": (33.0, 4.5),
}
_PIT_BASE = {  # lower is better → z is negated in the composite
    "xera": (4.20, 0.85), "xwoba_against": (0.315, 0.035),
    "barrel_allowed": (8.0, 3.0), "hardhit_allowed": (39.0, 5.0),
}


def _z(val, mean, sd):
    if val is None or sd == 0:
        return None
    return (val - mean) / sd


# Multi-year window: current + 2 prior seasons. Each year weighted by
# recency × sample, so a 40-IP injured 2026 doesn't override two elite
# full seasons (the Skubal fix), but a true breakout still moves because
# recency is weighted up.
_YEAR_RECENCY = {0: 1.30, 1: 1.00, 2: 0.70}  # offset from current → weight


def _flip_name(lf: str) -> str:
    if "," in lf:
        last, first = [s.strip() for s in lf.split(",", 1)]
        return f"{first} {last}"
    return lf


def _sf(row, key):
    try:
        v = row.get(key)
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _skill_scores(season: int) -> dict[str, dict]:
    """{normalized_name: {role, skill_z, skill_rank, comps, traj}} from MULTI-
    YEAR bulk Statcast (current + 2 prior). Per-metric values are a sample×
    recency-weighted blend across years — stable true talent that doesn't
    crater on one down/injured season. Also computes a trajectory delta
    (this year vs prior baseline) for the ascending/declining factor."""
    if season in _SKILL_CACHE:
        return _SKILL_CACHE[season]
    out: dict[str, dict] = {}
    years = [season - off for off in (0, 1, 2)]

    def _wavg(per_year):
        """per_year: list of (value, weight). Returns weighted mean or None."""
        pairs = [(v, w) for v, w in per_year if v is not None and w > 0]
        if not pairs:
            return None
        return sum(v * w for v, w in pairs) / sum(w for _, w in pairs)

    # ---------- hitters ----------
    try:
        exp_by_year = {y: savant.batter_expected(y) for y in years}
        qoc_by_year = {y: savant.batter_statcast(y) for y in years}
        # pid universe across years
        pids = set()
        for y in years:
            pids |= set(exp_by_year[y].keys())
        for pid in pids:
            metrics = {k: [] for k in ("xwoba", "xslg", "xba", "barrel", "hardhit", "sweetspot")}
            cur_sample = 0.0      # current-year PA (for display)
            total_sample = 0.0    # all-year PA (for n/(n+k) shrinkage)
            name = ""
            recent_xwoba = prior_xwoba = None
            for off, y in enumerate(years):
                row = exp_by_year[y].get(pid)
                if not row:
                    continue
                pa = _sf(row, "pa") or 0
                if pa < 30:  # floor; below this a year is noise (continuous shrink handles the rest)
                    continue
                rw = _YEAR_RECENCY[off]
                wt = pa * rw
                q = qoc_by_year[y].get(pid, {})
                vals = {
                    "xwoba": _sf(row, "est_woba"), "xslg": _sf(row, "est_slg"),
                    "xba": _sf(row, "est_ba"), "barrel": _sf(q, "brl_percent"),
                    "hardhit": _sf(q, "ev95percent"), "sweetspot": _sf(q, "anglesweetspotpercent"),
                }
                for k, v in vals.items():
                    metrics[k].append((v, wt))
                total_sample += pa
                if off == 0:
                    cur_sample = pa
                if not name:
                    name = _flip_name(row.get("last_name, first_name", ""))
                if off == 0:
                    recent_xwoba = vals["xwoba"]
                else:
                    prior_xwoba = vals["xwoba"] if prior_xwoba is None else prior_xwoba
            wm = {k: _wavg(metrics[k]) for k in metrics}
            if wm["xwoba"] is None:
                continue
            zs = {
                "xwoba": _z(wm["xwoba"], *_HIT_BASE["xwoba"]),
                "xslg": _z(wm["xslg"], *_HIT_BASE["xslg"]),
                "xba": _z(wm["xba"], *_HIT_BASE["xba"]),
                "barrel": _z(wm["barrel"], *_HIT_BASE["barrel"]),
                "hardhit": _z(wm["hardhit"], *_HIT_BASE["hardhit"]),
                "sweetspot": _z(wm["sweetspot"], *_HIT_BASE["sweetspot"]),
            }
            w = {"xwoba": 0.45, "xslg": 0.12, "xba": 0.08, "barrel": 0.18,
                 "hardhit": 0.12, "sweetspot": 0.05}
            num = sum(w[k] * zs[k] for k in w if zs[k] is not None)
            den = sum(w[k] for k in w if zs[k] is not None)
            if den <= 0 or not name:
                continue
            traj = (recent_xwoba - prior_xwoba) if (recent_xwoba is not None and prior_xwoba is not None) else None
            comps = {k: (round(wm[k], 3) if k in ("xwoba","xslg","xba") and wm[k] is not None
                         else round(wm[k],1) if wm[k] is not None else None) for k in wm}
            comps["pa"] = int(cur_sample)
            comps["total_pa"] = int(total_sample)
            comps["multi_year"] = len([1 for v, _w in metrics["xwoba"] if v is not None])
            out[_norm(name)] = {"role": "hitter", "skill_z": round(num / den, 3),
                                "comps": comps, "traj": round(traj, 3) if traj is not None else None}
    except Exception as e:
        logging.warning("multi-year hitter skill failed: %s", e)

    # ---------- pitchers ----------
    try:
        pexp_by_year = {y: savant.pitcher_expected(y) for y in years}
        pqoc_by_year = {y: savant.pitcher_statcast(y) for y in years}
        pids = set()
        for y in years:
            pids |= set(pexp_by_year[y].keys())
        for pid in pids:
            metrics = {k: [] for k in ("xera", "xwoba_against", "barrel_allowed", "hardhit_allowed")}
            cur_sample = 0.0
            total_sample = 0.0
            name = ""
            recent_xera = prior_xera = None
            for off, y in enumerate(years):
                row = pexp_by_year[y].get(pid)
                if not row:
                    continue
                pa = _sf(row, "pa") or 0
                if pa < 30:
                    continue
                rw = _YEAR_RECENCY[off]
                wt = pa * rw
                q = pqoc_by_year[y].get(pid, {})
                vals = {
                    "xera": _sf(row, "xera"), "xwoba_against": _sf(row, "est_woba"),
                    "barrel_allowed": _sf(q, "brl_percent"), "hardhit_allowed": _sf(q, "ev95percent"),
                }
                for k, v in vals.items():
                    metrics[k].append((v, wt))
                total_sample += pa
                if off == 0:
                    cur_sample = pa
                    recent_xera = vals["xera"]
                elif prior_xera is None:
                    prior_xera = vals["xera"]
                if not name:
                    name = _flip_name(row.get("last_name, first_name", ""))
            wm = {k: _wavg(metrics[k]) for k in metrics}
            if wm["xera"] is None and wm["xwoba_against"] is None:
                continue
            zs = {
                "xera": -_z(wm["xera"], *_PIT_BASE["xera"]) if wm["xera"] is not None else None,
                "xwoba_against": -_z(wm["xwoba_against"], *_PIT_BASE["xwoba_against"]) if wm["xwoba_against"] is not None else None,
                "barrel_allowed": -_z(wm["barrel_allowed"], *_PIT_BASE["barrel_allowed"]) if wm["barrel_allowed"] is not None else None,
                "hardhit_allowed": -_z(wm["hardhit_allowed"], *_PIT_BASE["hardhit_allowed"]) if wm["hardhit_allowed"] is not None else None,
            }
            w = {"xera": 0.45, "xwoba_against": 0.27, "barrel_allowed": 0.16, "hardhit_allowed": 0.12}
            num = sum(w[k] * zs[k] for k in w if zs[k] is not None)
            den = sum(w[k] for k in w if zs[k] is not None)
            if den <= 0 or not name:
                continue
            # pitcher trajectory: xERA going DOWN = improving → positive traj
            traj = (prior_xera - recent_xera) if (recent_xera is not None and prior_xera is not None) else None
            comps = {
                "xera": round(wm["xera"], 2) if wm["xera"] is not None else None,
                "xwoba_against": round(wm["xwoba_against"], 3) if wm["xwoba_against"] is not None else None,
                "barrel_allowed": round(wm["barrel_allowed"], 1) if wm["barrel_allowed"] is not None else None,
                "hardhit_allowed": round(wm["hardhit_allowed"], 1) if wm["hardhit_allowed"] is not None else None,
                "pa": int(cur_sample),
                "total_pa": int(total_sample),
                "multi_year": len([1 for v, _w in metrics["xera"] if v is not None]),
            }
            out[_norm(name)] = {"role": "pitcher", "skill_z": round(num / den, 3),
                                "comps": comps, "traj": round(traj, 3) if traj is not None else None}
    except Exception as e:
        logging.warning("multi-year pitcher skill failed: %s", e)

    # Fold in minor-league prospects (MiLB production → MLB-equivalent z).
    # Don't overwrite a player who already has an MLB Statcast read.
    try:
        for nname, rec in _prospect_skills(season).items():
            if nname not in out:
                out[nname] = rec
    except Exception as e:
        logging.warning("prospect merge failed: %s", e)

    # Pool-wide rank by skill_z (desc), assign skill_rank.
    ordered = sorted(out.items(), key=lambda kv: -kv[1]["skill_z"])
    for i, (_nm, rec) in enumerate(ordered, start=1):
        rec["skill_rank"] = i
    _SKILL_CACHE[season] = out
    return out


# How much OUR skill model drives base value when we have Statcast on a player.
# Prospects/minors/low-PA (no Statcast) fall back to 100% consensus.
_SKILL_BLEND = 0.50


def _injury_factor(name: str, role: str) -> tuple[float, str]:
    """Dynasty injury-risk discount. We don't have multi-year IL history, but
    the ESPN feed's CURRENT status is a reasonable proxy: a guy on the 60-day
    IL right now carries real dynasty risk, a day-to-day tweak almost none.
    Pitchers carry a small standing arm-risk haircut on top (TJ attrition) —
    kept light since the steeper pitcher age curve already prices some of it."""
    mult, note = 1.0, ""
    rec = injuries.lookup(name)
    if rec:
        s = (rec.get("status") or "").lower()
        typ = rec.get("type") or ""
        if "60-day" in s:
            mult, note = 0.90, f"on 60-day IL ({typ}) — dynasty risk"
        elif "15-day" in s:
            mult, note = 0.95, f"on 15-day IL ({typ})"
        elif "10-day" in s:
            mult, note = 0.97, f"on 10-day IL ({typ})"
        elif "day-to-day" in s:
            mult, note = 0.99, f"day-to-day ({typ})"
    if role == "pitcher":
        mult *= 0.97  # standing arm-injury risk premium for pitchers
        note = (note + " · " if note else "") + "pitcher arm-risk haircut"
    return mult, note


def _multipos_factor(pos: str) -> tuple[float, str]:
    """Multi-position eligibility is real dynasty value — a 2B/SS/OF fills
    holes, covers injuries, and unlocks roster construction. Small premium
    by count of distinct real positions (DH/util don't count as flexibility)."""
    parts = {p.strip().upper() for p in (pos or "").replace("/", ",").split(",") if p.strip()}
    real = parts - {"DH", "UT", "UTIL", "TWP"}
    n = len(real)
    if n >= 3:
        return 1.06, f"{n}-position eligible (flexibility premium)"
    if n == 2:
        return 1.03, f"{n}-position eligible"
    return 1.0, ""


def _young_ascending_factor(age: float | None, skill_z: float | None) -> tuple[float, str]:
    """The age curve credits a fixed peak derived from CURRENT production, but
    a very young player already posting elite skill is likely still ASCENDING
    — their true peak is probably higher than today's line. Modest bonus for
    age ≤ 23 with above-average skill; bigger the younger + better they are."""
    if age is None or skill_z is None or age > 23 or skill_z < 0.4:
        return 1.0, ""
    # up to +8% for a 20yo with +1.5z skill
    bonus = min(0.08, (24 - age) * 0.015 + (skill_z - 0.4) * 0.03)
    if bonus < 0.01:
        return 1.0, ""
    return 1.0 + bonus, f"young & ascending (age {age:.0f}, skill z+{skill_z:.1f}) ×{1+bonus:.2f}"


def _eta_factor(eta: str, season: int) -> tuple[float, str]:
    """For prospects, how soon they'll contribute. The CSV ETA column is a
    free signal the consensus rank under-weights: a 2026 arrival is worth
    more than a 2028 lottery ticket (sooner value + less time for the bust
    risk to bite). Already-up / this-year = 1.0; each extra year out ~ -5%."""
    try:
        yr = int((eta or "").strip())
    except (TypeError, ValueError):
        return 1.0, ""
    years_out = yr - season
    if years_out <= 0:
        return 1.0, ""
    mult = max(0.82, 1.0 - 0.05 * years_out)
    return mult, f"ETA {yr} ({years_out}y out) ×{mult:.2f}"


# ---- dynasty value -----------------------------------------------------------

def _role_for(pos: str) -> str:
    p = (pos or "").upper()
    if p in ("SP", "RP", "P"):
        return "pitcher"
    return "hitter"


def dynasty_value(nname: str, season: int) -> dict | None:
    """Full dynasty valuation for one (normalized) name. Returns the score,
    the multi-year projected value path, and a component breakdown."""
    cons = _consensus().get(nname)
    if not cons:
        return None
    role = _role_for(cons["pos"])
    cons_value = _rank_value(cons["rank"])
    # Blend the consensus prior with OUR skill-rank value. v1.3: the blend is
    # CONFIDENCE-WEIGHTED by sample size, not a flat 50/50. Single-season
    # Statcast is a noisy/incomplete read on a multi-year dynasty asset —
    # leaning 50% on a 40-IP injured sample buried established aces (Skubal
    # #5 consensus → #45). Now: more sample → more skill weight, capped at
    # _SKILL_BLEND. A thin sample leans on the consensus (the career proxy).
    skill = _skill_scores(season).get(nname)
    skill_block = None
    if skill and skill.get("skill_rank"):
        # Proper Bayesian shrinkage: confidence = n/(n+k) on the player's TOTAL
        # multi-year sample, where k is the prior strength in PA-equivalents.
        # Continuous (no hard gate) — a thin sample shrinks smoothly toward the
        # consensus prior; a multi-season sample asymptotes to full skill weight.
        comps = skill.get("comps") or {}
        sample = comps.get("total_pa") or comps.get("pa") or comps.get("bf") or 0
        is_prospect = skill.get("is_prospect", False)
        # k tuned to where each metric roughly stabilizes (PA-equivalents).
        k_prior = 90 if skill.get("role") == "pitcher" else 220
        if is_prospect:
            k_prior = 160  # MiLB lines are noisier per-PA → shrink harder
        conf = sample / (sample + k_prior) if sample else 0.0
        eff_blend = _SKILL_BLEND * conf
        talent_value = _rank_value(skill["skill_rank"])
        base = eff_blend * talent_value + (1 - eff_blend) * cons_value
        skill_block = {
            "skill_rank": skill["skill_rank"],
            "skill_z": skill["skill_z"],
            "talent_value": round(talent_value, 1),
            "blend_weight": round(eff_blend, 2),
            "sample": int(sample),
            "comps": skill["comps"],
            "is_prospect": skill.get("is_prospect", False),
            "actual_level": skill.get("actual_level"),
        }
    else:
        base = cons_value
    pos_mult = _pos_scarcity(cons["pos"])
    luck = _statcast_luck(season).get(nname)
    luck_mult, luck_note = _luck_multiplier(luck, role)
    inj_mult, inj_note = _injury_factor(cons["name"], role)
    dur = _durability(season).get(nname)
    dur_mult = dur["mult"] if dur else 1.0
    dur_note = dur["note"] if dur else ""
    eta_mult, eta_note = _eta_factor(cons.get("eta"), season)
    multipos_mult, multipos_note = _multipos_factor(cons["pos"])
    young_mult, young_note = _young_ascending_factor(
        cons.get("age"), skill["skill_z"] if skill else None)
    # Year-over-year trajectory: this season's xwOBA/xERA vs the prior baseline.
    # Ascending true talent (esp. young) is a dynasty buy; sliding skill is a
    # warning the consensus is slow to price. Small ±4% — it's a trend nudge.
    traj_mult, traj_note = 1.0, ""
    if skill and skill.get("traj") is not None:
        t = skill["traj"]
        if skill["role"] == "hitter":  # +xwOBA delta = improving
            traj_mult = 1.0 + max(-0.04, min(t / 0.030 * 0.04, 0.04))
            if abs(t) >= 0.015:
                traj_note = f"{'rising' if t>0 else 'sliding'} xwOBA {t:+.3f} YoY"
        else:  # pitcher traj = prior_xera - recent_xera; + = improving
            traj_mult = 1.0 + max(-0.04, min(t / 0.60 * 0.04, 0.04))
            if abs(t) >= 0.30:
                traj_note = f"{'improving' if t>0 else 'declining'} xERA {-t:+.2f} YoY"
    age = cons.get("age")

    # Multi-year projection: de-age the consensus value to a peak-talent
    # baseline, then walk the age curve forward 6 seasons with a discount.
    # dynasty_score = Σ discounted future-year values. This is what makes a
    # 23yo worth more than a 31yo at the same current rank.
    DISCOUNT = 0.90
    HORIZON = 6
    curve: list[dict] = []
    cur_age_factor = _age_factor(age, role) if age else 1.0
    peak_value = base / max(cur_age_factor, 0.5)  # implied peak production value
    dynasty_score = 0.0
    for k in range(HORIZON):
        yr_age = (age + k) if age else None
        yr_factor = _age_factor(yr_age, role) if yr_age else cur_age_factor
        yr_value = (peak_value * yr_factor * pos_mult * luck_mult * inj_mult
                    * eta_mult * multipos_mult * young_mult * traj_mult * dur_mult)
        discounted = yr_value * (DISCOUNT ** k)
        dynasty_score += discounted
        curve.append({
            "year": season + k,
            "age": round(yr_age, 1) if yr_age else None,
            "value": round(yr_value, 1),
        })

    return {
        "name": cons["name"],
        "pos": cons["pos"],
        "team": cons["team"],
        "age": age,
        "role": role,
        "level": (cons.get("level") or "").strip(),
        "eta": (cons.get("eta") or "").strip(),
        "consensus_rank": cons["rank"],
        "dynasty_score": round(dynasty_score, 1),
        "this_year_value": round(curve[0]["value"], 1) if curve else 0,
        "components": {
            "rank_base": round(base, 1),
            "consensus_value": round(cons_value, 1),
            "skill": skill_block,  # None when no Statcast sample
            "skill_blend": _SKILL_BLEND if skill_block else 0.0,
            "pos_scarcity": pos_mult,
            "luck_mult": round(luck_mult, 3),
            "luck_note": luck_note,
            "injury_mult": round(inj_mult, 3),
            "injury_note": inj_note,
            "durability_mult": round(dur_mult, 3),
            "durability_note": dur_note,
            "eta_mult": round(eta_mult, 3),
            "eta_note": eta_note,
            "multipos_mult": round(multipos_mult, 3),
            "multipos_note": multipos_note,
            "young_mult": round(young_mult, 3),
            "young_note": young_note,
            "traj_mult": round(traj_mult, 3),
            "traj_note": traj_note,
            "age_factor": round(cur_age_factor, 3),
        },
        "projection_curve": curve,
    }


_RANKINGS_CACHE: dict[int, list[dict]] = {}


def rankings(season: int, limit: int = 500) -> list[dict]:
    """Our dynasty rankings — re-rank the consensus pool by OUR dynasty_score.
    Includes a `consensus_rank` so the UI can show our-vs-market disagreement.
    Cached per-season in-process (rebuilds are deterministic + the sub-models
    are already disk-cached); cleared on restart."""
    if season not in _RANKINGS_CACHE:
        out = []
        for nname in _consensus():
            v = dynasty_value(nname, season)
            if v:
                out.append(v)
        out.sort(key=lambda x: -x["dynasty_score"])
        for i, v in enumerate(out, start=1):
            v["our_rank"] = i
            v["rank_delta"] = v["consensus_rank"] - i  # + = we're higher than market
        _RANKINGS_CACHE[season] = out
    return _RANKINGS_CACHE[season][:limit]


# ---- trade analyzer ----------------------------------------------------------

def _slot_weight(i: int) -> float:
    """Diminishing weight for the i-th-best player in a package (0-based).
    Roster spots are scarce, so each additional, lesser player is worth less:
    best ×1.0, 2nd ×0.90, 3rd ×0.81, 4th ×0.73, … floored at 0.55."""
    return max(0.55, 1.0 - 0.10 * i)


def _package_value(values: list[float]) -> float:
    """Concave package value — deliberately NOT a pure sum. Bakes in both the
    consolidation premium (one star outweighs several role players) AND a
    package detriment (quantity is discounted): you can only roster so many,
    and a 3-for-1 hands real value to the side getting the single best asset."""
    return sum(v * _slot_weight(i) for i, v in enumerate(sorted(values, reverse=True)))


def _suggest_balancers(light_side_len: int, gap: float, exclude_norms: set[str],
                       season: int, k: int = 3) -> tuple[list[dict], float]:
    """When a deal is uneven, suggest players the LIGHTER side could add to
    even it. The added player lands in the next package slot (discounted), so
    we target a raw value of gap / next_slot_weight and return the board
    players closest to it, excluding everyone already in the trade."""
    w = _slot_weight(light_side_len)
    target_raw = gap / w if w else gap
    cands = []
    for v in rankings(season):
        if _norm(v["name"]) in exclude_norms:
            continue
        cands.append((abs(v["dynasty_score"] - target_raw), v))
    cands.sort(key=lambda x: x[0])
    picks = [{kk: v[kk] for kk in ("name", "pos", "age", "dynasty_score", "our_rank")}
             for _, v in cands[:k]]
    return picks, round(target_raw, 0)


def evaluate_trade(side_a_names: list[str], side_b_names: list[str], season: int) -> dict:
    """Evaluate a dynasty trade. Returns per-side valuations, the winner, a
    fairness verdict, and smart context (consolidation, win-now vs rebuild)."""
    def _val_side(names):
        vals, missing = [], []
        for nm in names:
            v = dynasty_value(_norm(nm), season)
            if v:
                vals.append(v)
            else:
                missing.append(nm)
        return vals, missing

    a_vals, a_missing = _val_side(side_a_names)
    b_vals, b_missing = _val_side(side_b_names)
    a_raw = sum(v["dynasty_score"] for v in a_vals)
    b_raw = sum(v["dynasty_score"] for v in b_vals)
    # Package value is concave, not additive (see _package_value): the side
    # sending a multi-player package has its total discounted, and the side
    # sending the single best asset is rewarded.
    a_total = _package_value([v["dynasty_score"] for v in a_vals])
    b_total = _package_value([v["dynasty_score"] for v in b_vals])

    diff = a_total - b_total
    bigger = max(a_total, b_total)
    pct = abs(diff) / bigger if bigger else 0
    if pct < 0.05:
        verdict = "Even — fair deal both ways"
    elif pct < 0.15:
        winner = "A" if diff > 0 else "B"
        verdict = f"Slight edge to side {winner}"
    elif pct < 0.30:
        winner = "A" if diff > 0 else "B"
        verdict = f"Side {winner} wins this clearly"
    else:
        winner = "A" if diff > 0 else "B"
        verdict = f"Lopsided — side {winner} wins big"

    def _age_profile(vals):
        ages = [v["age"] for v in vals if v.get("age")]
        return round(sum(ages) / len(ages), 1) if ages else None

    a_age = _age_profile(a_vals)
    b_age = _age_profile(b_vals)
    context = []
    if a_age and b_age and abs(a_age - b_age) >= 2.5:
        younger, older = ("A", "B") if a_age < b_age else ("B", "A")
        context.append(f"Side {younger} is the rebuild/youth side (avg age "
                       f"{min(a_age,b_age)} vs {max(a_age,b_age)}); side {older} is win-now.")
    if len(a_vals) != len(b_vals):
        more, fewer = ("A", "B") if len(a_vals) > len(b_vals) else ("B", "A")
        disc = a_total / a_raw if more == "A" and a_raw else (b_total / b_raw if b_raw else 1.0)
        context.append(f"Side {fewer} consolidates ({len(a_vals)}-for-{len(b_vals)}) — "
                       f"quality over quantity. Side {more}'s package is discounted "
                       f"~{round((1-disc)*100)}% (roster spots are scarce; value isn't additive).")

    # Balancer: when the deal isn't even, suggest who the lighter side could
    # add to close the gap.
    balancer = None
    if pct >= 0.05 and (a_vals or b_vals):
        light = "A" if a_total < b_total else "B"
        light_len = len(a_vals) if light == "A" else len(b_vals)
        exclude = {_norm(n) for n in (side_a_names + side_b_names)}
        picks, target = _suggest_balancers(light_len, abs(diff), exclude, season)
        balancer = {
            "side_to_add": light,
            "gap": round(abs(diff), 1),
            "target_value": target,
            "suggestions": picks,
        }

    return {
        "side_a": {"players": a_vals, "raw": round(a_raw, 1), "total": round(a_total, 1), "avg_age": a_age, "missing": a_missing},
        "side_b": {"players": b_vals, "raw": round(b_raw, 1), "total": round(b_total, 1), "avg_age": b_age, "missing": b_missing},
        "diff": round(diff, 1),
        "verdict": verdict,
        "context": context,
        "balancer": balancer,
    }


# ---- minor-league reconnaissance + free-agent pickups -----------------------
# The dynasty board's candidate pool is the top-500 consensus CSV, so deep
# risers climbing the minors won't appear on it. milb_recon() actively scans
# the AAA + AA stat leaderboards (MLB Stats API), scores each leader with the
# same MLB-equivalent prospect model the board uses (_prospect_skill_z), and
# surfaces young-for-level breakouts. free_agent_pickups() then subtracts the
# players rostered anywhere in the user's league to leave true pickups.


def _people_bulk(ids: list[int]) -> dict[int, dict]:
    """{pid: {age, pos, team}} for a batch of player ids (one call per 100)."""
    out: dict[int, dict] = {}
    uniq = list(dict.fromkeys(i for i in ids if i))
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i + 100]
        try:
            data = mlb_api._get("/people", params={
                "personIds": ",".join(map(str, chunk)),
                "hydrate": "currentTeam",
            })
        except Exception:
            continue
        for p in data.get("people", []) or []:
            out[p["id"]] = {
                "age": p.get("currentAge"),
                "pos": ((p.get("primaryPosition") or {}).get("abbreviation") or ""),
                "team": ((p.get("currentTeam") or {}).get("abbreviation")
                         or (p.get("currentTeam") or {}).get("name") or ""),
            }
    return out


@disk_cache.cached_disk(6 * 3600, namespace="milb_leader_ids")
def _milb_leader_ids(season: int, sportid: int, group: str, category: str, limit: int) -> list[tuple]:
    """Sorted top-N (pid, name) for a MiLB level/stat from /stats/leaders."""
    try:
        data = mlb_api._get("/stats/leaders", params={
            "leaderCategories": category, "season": season,
            "sportId": sportid, "statGroup": group, "limit": limit,
        })
    except Exception:
        return []
    out = []
    for cat in data.get("leagueLeaders", []) or []:
        for ld in cat.get("leaders", []) or []:
            per = ld.get("person") or {}
            if per.get("id"):
                out.append((per["id"], per.get("fullName")))
    return out


@disk_cache.cached_disk(6 * 3600, namespace="milb_recon")
def milb_recon(season: int, limit_per: int = 35, min_skill_z: float = 0.55) -> list[dict]:
    """Scan AAA + AA leaderboards for rising, young-for-level prospects.
    Returns dicts sorted by recon_score (best first), deduped by player id."""
    levels = ((11, "AAA"), (12, "AA"))
    cats = (("hitting", "onBasePlusSlugging", "hitter"),
            ("pitching", "strikeoutsPer9Inn", "pitcher"))
    targets = []  # (pid, name, sportid, level, group, role)
    seen = set()
    for sid, level in levels:
        for group, cat, role in cats:
            for pid, name in _milb_leader_ids(season, sid, group, cat, limit_per):
                key = (pid, level)
                if key in seen:
                    continue
                seen.add(key)
                targets.append((pid, name, sid, level, group, role))
    people = _people_bulk([t[0] for t in targets])

    def _one(t):
        pid, name, sid, level, group, role = t
        stat = _milb_line(pid, season, sid, group)
        if not stat:
            return None
        info = people.get(pid, {})
        res = _prospect_skill_z(level, role, info.get("age"), stat)
        if not res:
            return None
        z, comps = res
        if z < min_skill_z:
            return None
        avl = comps.get("age_vs_level")
        if avl is not None and avl < -0.5:   # clearly old-for-level → not a riser
            return None
        # recon_score: MLB-equiv skill, lifted for level-proximity to the
        # majors and for being young-for-level. Own scale (not dynasty value).
        lvl_prox = {"AAA": 1.0, "AA": 0.85}.get(level, 0.7)
        recon = round((z + 1.5) * 100 * lvl_prox + (avl or 0) * 8, 1)
        return {
            "name": name, "player_id": pid, "level": level,
            "age": info.get("age"), "role": role,
            "pos": info.get("pos") or ("P" if role == "pitcher" else ""),
            "team": info.get("team") or "", "skill_z": z,
            "recon_score": recon, "milb": comps,
        }

    best: dict[int, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(_one, targets):
                if r and (r["player_id"] not in best
                          or r["recon_score"] > best[r["player_id"]]["recon_score"]):
                    best[r["player_id"]] = r
    except Exception as e:
        logging.warning("milb recon failed: %s", e)
    return sorted(best.values(), key=lambda x: -x["recon_score"])


@disk_cache.cached_disk(4 * 3600, namespace="recent_form")
def _recent_form(pid: int, role: str, season: int) -> dict | None:
    """Short-term hot/cold read: recent pts/G vs season + a form tag
    (HOT/COLD/STEADY/ELITE), reusing the daily projection's form logic. Lets a
    low-dynasty-value bat on a heater still surface as a streaming add."""
    from . import projections as P
    g = "pitching" if role == "pitcher" else "hitting"
    try:
        last3 = mlb_api.player_stats(pid, group=g, season=season, last_n_days=3)
        last7 = mlb_api.player_stats(pid, group=g, season=season, last_n_days=7)
        last14 = mlb_api.player_stats(pid, group=g, season=season, last_n_days=14)
        seasn = mlb_api.player_stats(pid, group=g, season=season)
    except Exception:
        return None

    def _f(d, k):
        try:
            return float(d.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    if role == "pitcher":
        s7, s14, ss = _f(last7, "gamesStarted"), _f(last14, "gamesStarted"), _f(seasn, "gamesStarted")
        ps7 = P._per_start_pitcher_points(last7) if s7 >= 1 else None
        ps14 = P._per_start_pitcher_points(last14) if s14 >= 1 else None
        pss = P._per_start_pitcher_points(seasn) if ss >= 3 else None
        tag, _n = P._form_tag_pitcher(ps7, ps14, pss, int(s7), int(s14))
        recent = ps14 if ps14 is not None else ps7
        return {"tag": tag, "recent_pg": round(recent, 1) if recent is not None else None,
                "season_pg": round(pss, 1) if pss is not None else None}
    g3, g7, g14, gs = (_f(last3, "gamesPlayed"), _f(last7, "gamesPlayed"),
                       _f(last14, "gamesPlayed"), _f(seasn, "gamesPlayed"))
    pg3 = P._per_game_hitter_points(last3) if g3 >= 1 else None
    pg7 = P._per_game_hitter_points(last7) if g7 >= 2 else None
    pg14 = P._per_game_hitter_points(last14) if g14 >= 5 else None
    pgs = P._per_game_hitter_points(seasn) if gs >= 10 else None
    tag, _n = P._form_tag_hitter(pg3, pg7, pg14, int(g3), int(g14))
    recent = pg7 if pg7 is not None else pg3
    return {"tag": tag, "recent_pg": round(recent, 1) if recent is not None else None,
            "season_pg": round(pgs, 1) if pgs is not None else None}


def _attach_form(players: list[dict], season: int) -> None:
    """Mutates each player: adds ['form'] = {tag, recent_pg, season_pg}."""
    def _one(v):
        pid = _resolve_id(v["name"])
        return v, (_recent_form(pid, v.get("role") or "hitter", season) if pid else None)
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for v, form in ex.map(_one, players):
                v["form"] = form
    except Exception as e:
        logging.warning("form attach failed: %s", e)


class _NameSet:
    """Name membership with a fuzzy fallback for nickname/legal-name splits.
    The MLB Stats API returns full legal names (e.g. 'Leodalis De Vries') while
    Fantrax rosters use the common name ('Leo De Vries'); an exact normalized
    match misses these and shows owned players as available. We also match when
    the last token is equal AND one first name is a prefix of the other."""
    def __init__(self, norms):
        self.full = set(norms)
        self.by_last: dict[str, list[str]] = {}
        for n in norms:
            toks = n.split()
            if toks:
                self.by_last.setdefault(toks[-1], []).append(toks[0])

    def has(self, name: str) -> bool:
        nn = _norm(name)
        if nn in self.full:
            return True
        toks = nn.split()
        if not toks:
            return False
        f, last = toks[0], toks[-1]
        for rf in self.by_last.get(last, []):
            if f and rf and min(len(f), len(rf)) >= 3 and (f.startswith(rf) or rf.startswith(f)):
                return True
        return False


def free_agent_pickups(season: int, rostered_norm: set[str],
                       limit: int = 60, milb_limit: int = 30) -> dict:
    """Best-available pickups: the consensus board AND AAA/AA risers, minus
    everyone rostered in the league. Pure best-available (no roster-need tilt).
    Each available player carries a recent hot/cold form read, and we surface a
    `hot` shortlist so a streaming-worthy bat surfaces even at low dynasty value."""
    ranks = rankings(season)
    rostered = _NameSet(rostered_norm)
    cons = _NameSet({_norm(v["name"]) for v in ranks})
    available = [v for v in ranks if not rostered.has(v["name"])][:limit]
    _attach_form(available, season)
    hot = sorted(
        [v for v in available if (v.get("form") or {}).get("tag") in ("HOT", "ELITE")],
        key=lambda v: -((v.get("form") or {}).get("recent_pg") or 0),
    )[:12]
    risers = []
    for r in milb_recon(season):
        if rostered.has(r["name"]) or cons.has(r["name"]):
            continue   # rostered (fuzzy), or already shown on the consensus board
        risers.append(r)
    return {"available": available, "hot": hot, "milb_risers": risers[:milb_limit]}
