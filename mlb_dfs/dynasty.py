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

from . import disk_cache, mlb_api, savant

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
        peak = 26.0
        # gentle rise to peak, ~3.5%/yr decline after
        if age <= peak:
            return max(0.85, 1.0 - 0.015 * (peak - age))
        return max(0.40, 1.0 - 0.040 * (age - peak))
    # hitter
    peak = 27.0
    if age <= peak:
        return max(0.86, 1.0 - 0.018 * (peak - age))
    return max(0.45, 1.0 - 0.030 * (age - peak))


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


def _skill_scores(season: int) -> dict[str, dict]:
    """{normalized_name: {role, skill_z, skill_rank, comps}} from bulk Statcast.
    skill_rank is the pool-wide rank by composite z (hitters+pitchers pooled,
    z-scores are role-relative so a +2z arm ≈ a +2z bat)."""
    if season in _SKILL_CACHE:
        return _SKILL_CACHE[season]
    out: dict[str, dict] = {}

    def _flip(lf):
        if "," in lf:
            last, first = [s.strip() for s in lf.split(",", 1)]
            return f"{first} {last}"
        return lf

    def _f(row, key):
        try:
            v = row.get(key)
            return float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    # ---- hitters: join expected (xwoba/xslg/xba/pa) with statcast (qoc) ----
    try:
        exp = savant.batter_expected(season)
        qoc = savant.batter_statcast(season)
        for pid, row in exp.items():
            pa = _f(row, "pa") or 0
            if pa < 120:
                continue
            xwoba = _f(row, "est_woba")
            if xwoba is None:
                continue
            q = qoc.get(pid, {})
            comps = {
                "xwoba": xwoba, "xslg": _f(row, "est_slg"), "xba": _f(row, "est_ba"),
                "barrel": _f(q, "brl_percent"), "hardhit": _f(q, "ev95percent"),
                "sweetspot": _f(q, "anglesweetspotpercent"), "pa": int(pa),
            }
            zs = {
                "xwoba": _z(comps["xwoba"], *_HIT_BASE["xwoba"]),
                "xslg": _z(comps["xslg"], *_HIT_BASE["xslg"]),
                "xba": _z(comps["xba"], *_HIT_BASE["xba"]),
                "barrel": _z(comps["barrel"], *_HIT_BASE["barrel"]),
                "hardhit": _z(comps["hardhit"], *_HIT_BASE["hardhit"]),
                "sweetspot": _z(comps["sweetspot"], *_HIT_BASE["sweetspot"]),
            }
            w = {"xwoba": 0.45, "xslg": 0.12, "xba": 0.08, "barrel": 0.18,
                 "hardhit": 0.12, "sweetspot": 0.05}
            num = sum(w[k] * zs[k] for k in w if zs[k] is not None)
            den = sum(w[k] for k in w if zs[k] is not None)
            if den <= 0:
                continue
            skill_z = num / den
            out[_norm(_flip(row.get("last_name, first_name", "")))] = {
                "role": "hitter", "skill_z": round(skill_z, 3), "comps": comps,
            }
    except Exception:
        pass

    # ---- pitchers: expected (xera/xwoba-against) + statcast (qoc allowed) ----
    try:
        pexp = savant.pitcher_expected(season)
        pqoc = savant.pitcher_statcast(season)
        for pid, row in pexp.items():
            pa = _f(row, "pa") or 0
            if pa < 80:
                continue
            xera = _f(row, "xera")
            xwa = _f(row, "est_woba")
            if xera is None and xwa is None:
                continue
            q = pqoc.get(pid, {})
            comps = {
                "xera": xera, "xwoba_against": xwa,
                "barrel_allowed": _f(q, "brl_percent"),
                "hardhit_allowed": _f(q, "ev95percent"), "pa": int(pa),
            }
            # lower = better → negate
            zs = {
                "xera": -_z(comps["xera"], *_PIT_BASE["xera"]) if comps["xera"] is not None else None,
                "xwoba_against": -_z(comps["xwoba_against"], *_PIT_BASE["xwoba_against"]) if comps["xwoba_against"] is not None else None,
                "barrel_allowed": -_z(comps["barrel_allowed"], *_PIT_BASE["barrel_allowed"]) if comps["barrel_allowed"] is not None else None,
                "hardhit_allowed": -_z(comps["hardhit_allowed"], *_PIT_BASE["hardhit_allowed"]) if comps["hardhit_allowed"] is not None else None,
            }
            w = {"xera": 0.45, "xwoba_against": 0.27, "barrel_allowed": 0.16, "hardhit_allowed": 0.12}
            num = sum(w[k] * zs[k] for k in w if zs[k] is not None)
            den = sum(w[k] for k in w if zs[k] is not None)
            if den <= 0:
                continue
            skill_z = num / den
            out[_norm(_flip(row.get("last_name, first_name", "")))] = {
                "role": "pitcher", "skill_z": round(skill_z, 3), "comps": comps,
            }
    except Exception:
        pass

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
    # Blend the consensus prior with OUR Statcast skill-rank value. When we
    # have skill data, base = 50/50 blend; otherwise pure consensus.
    skill = _skill_scores(season).get(nname)
    skill_block = None
    if skill and skill.get("skill_rank"):
        talent_value = _rank_value(skill["skill_rank"])
        base = _SKILL_BLEND * talent_value + (1 - _SKILL_BLEND) * cons_value
        skill_block = {
            "skill_rank": skill["skill_rank"],
            "skill_z": skill["skill_z"],
            "talent_value": round(talent_value, 1),
            "comps": skill["comps"],
            "is_prospect": skill.get("is_prospect", False),
            "actual_level": skill.get("actual_level"),
        }
    else:
        base = cons_value
    pos_mult = _pos_scarcity(cons["pos"])
    luck = _statcast_luck(season).get(nname)
    luck_mult, luck_note = _luck_multiplier(luck, role)
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
        yr_value = peak_value * yr_factor * pos_mult * luck_mult
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
            "age_factor": round(cur_age_factor, 3),
        },
        "projection_curve": curve,
    }


def rankings(season: int, limit: int = 500) -> list[dict]:
    """Our dynasty rankings — re-rank the consensus pool by OUR dynasty_score.
    Includes a `consensus_rank` so the UI can show our-vs-market disagreement."""
    out = []
    for nname in _consensus():
        v = dynasty_value(nname, season)
        if v:
            out.append(v)
    out.sort(key=lambda x: -x["dynasty_score"])
    for i, v in enumerate(out, start=1):
        v["our_rank"] = i
        v["rank_delta"] = v["consensus_rank"] - i  # + = we're higher on them than market
    return out[:limit]


# ---- trade analyzer ----------------------------------------------------------

def _consolidation_premium(values: list[float]) -> float:
    """In roster-capped leagues the single best player in a package is worth
    more than the raw sum (you can only roster so many; quality > quantity).
    Add a premium to whichever side has the highest-value single player,
    scaled by how lopsided the package counts are."""
    if not values:
        return 0.0
    return max(values) * 0.08  # 8% of the best player's value


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
    # Consolidation premium goes to the side RECEIVING the best player — i.e.
    # the side giving up quantity for quality. We add it to each side's value
    # of what they GIVE so the comparison reflects "what you send out".
    a_best = _consolidation_premium([v["dynasty_score"] for v in a_vals])
    b_best = _consolidation_premium([v["dynasty_score"] for v in b_vals])
    a_total = a_raw + a_best
    b_total = b_raw + b_best

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
        context.append(f"Side {fewer} consolidates ({len(a_vals)}-for-{len(b_vals)}) — "
                       f"quality over quantity, worth a premium in capped leagues.")

    return {
        "side_a": {"players": a_vals, "raw": round(a_raw, 1), "total": round(a_total, 1), "avg_age": a_age, "missing": a_missing},
        "side_b": {"players": b_vals, "raw": round(b_raw, 1), "total": round(b_total, 1), "avg_age": b_age, "missing": b_missing},
        "diff": round(diff, 1),
        "verdict": verdict,
        "context": context,
    }
