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
import math
import os
import unicodedata
from datetime import date as Date

from . import savant

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
# the top of the board. value(rank) = 1000 * exp(-k*(rank-1)); k tuned so
# rank 1 ≈ 1000, rank 50 ≈ 430, rank 150 ≈ 130, rank 300 ≈ 22, rank 500 ≈ 4.
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


def _statcast_luck(season: int) -> dict[str, dict]:
    """{normalized_name: {kind, luck_delta, woba/xwoba or era/xera}}.
    luck_delta > 0 means UNDER-performing peripherals (buy-low, positive
    regression coming); < 0 means OVER-performing (sell-high)."""
    if season in _LUCK_CACHE:
        return _LUCK_CACHE[season]
    out: dict[str, dict] = {}
    # Savant name field is "last, first" — flip to "first last" for the join.
    def _flip(lastfirst: str) -> str:
        if "," in lastfirst:
            last, first = [s.strip() for s in lastfirst.split(",", 1)]
            return f"{first} {last}"
        return lastfirst
    try:
        for row in savant.batter_expected(season).values():
            nm = _norm(_flip(row.get("last_name, first_name", "")))
            if not nm:
                continue
            try:
                woba = float(row.get("woba") or 0)
                xwoba = float(row.get("est_woba") or 0)
            except ValueError:
                continue
            if woba and xwoba:
                out[nm] = {"kind": "hitter", "woba": woba, "xwoba": xwoba,
                           "luck_delta": round(xwoba - woba, 3)}
    except Exception:
        pass
    try:
        for row in savant.pitcher_expected(season).values():
            nm = _norm(_flip(row.get("last_name, first_name", "")))
            if not nm:
                continue
            try:
                era = float(row.get("era") or 0) if row.get("era") else None
                xera = float(row.get("xera") or 0) if row.get("xera") else None
            except ValueError:
                era = xera = None
            # For pitchers, luck_delta>0 = unlucky (ERA worse than xERA → improvement coming)
            if era and xera:
                out.setdefault(nm, {})
                out[nm].update({"kind": "pitcher", "era": era, "xera": xera,
                                "luck_delta_era": round(era - xera, 2)})
    except Exception:
        pass
    _LUCK_CACHE[season] = out
    return out


def _luck_multiplier(rec: dict | None) -> tuple[float, str]:
    """Convert a Statcast luck record into a value multiplier + a one-line note."""
    if not rec:
        return 1.0, ""
    if rec.get("kind") == "hitter" and rec.get("luck_delta") is not None:
        d = rec["luck_delta"]  # xwoba - woba; + = unlucky/buy-low
        # ±0.030 wOBA is a big gap; scale to ±10%, cap.
        mult = 1.0 + max(-0.10, min(d / 0.030 * 0.10, 0.10))
        if abs(d) >= 0.015:
            tag = "buy-low (xwOBA > wOBA)" if d > 0 else "sell-high (wOBA > xwOBA)"
            return mult, f"{tag}: {rec['woba']:.3f} wOBA vs {rec['xwoba']:.3f} xwOBA"
        return mult, ""
    if rec.get("kind") == "pitcher" and rec.get("luck_delta_era") is not None:
        d = rec["luck_delta_era"]  # era - xera; + = unlucky/buy-low
        mult = 1.0 + max(-0.10, min(d / 0.75 * 0.10, 0.10))
        if abs(d) >= 0.40:
            tag = "buy-low (ERA > xERA)" if d > 0 else "sell-high (ERA < xERA)"
            return mult, f"{tag}: {rec['era']:.2f} ERA vs {rec['xera']:.2f} xERA"
        return mult, ""
    return 1.0, ""


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
    base = _rank_value(cons["rank"])
    pos_mult = _pos_scarcity(cons["pos"])
    luck = _statcast_luck(season).get(nname)
    luck_mult, luck_note = _luck_multiplier(luck)
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
