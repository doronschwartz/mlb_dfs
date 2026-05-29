"""Stuff+ leaderboard — JL's pitch-quality model, served + Bayesian-corrected.

JL's model (an XGBoost Stuff+ à la FanGraphs: scores pitch nastiness from
physical traits — velo, break, extension, approach angle, arsenal-relative
deception) outputs a per-pitcher × per-pitch-type leaderboard. We ingest that
static snapshot (data/stuff_leaderboard.csv) and add the one thing it lacks:

  SAMPLE-SIZE SHRINKAGE. The raw model has no regression to the mean, so a
  pitch type thrown 50 times sits next to one thrown 1,200 times with equal
  trust. We shrink each row's Stuff+ toward the 100 league mean by
  pitches/(pitches+k) — proper n/(n+k), the same Bayesian treatment our other
  models use — then usage-weight pitch types into one pitcher-level number.

Refresh: re-run JL's pipeline and drop a new pitcher_leaderboard.csv here.
"""
from __future__ import annotations

import csv
import os

_CSV = os.path.join(os.path.dirname(__file__), "data", "stuff_leaderboard.csv")

# Prior strength in pitches. Stuff+ comes from low-noise physical traits so it
# stabilizes fast, but a 50-pitch sample's AVERAGE still deserves regression.
# k≈80 pitches → a 50-pitch read is ~38% shrunk, a 240-pitch read ~25%.
_K_STUFF = 80.0
_LEAGUE_MEAN = 100.0

_CACHE: list[dict] | None = None


def _shrink(value: float, pitches: float) -> float:
    """Regress a Stuff+ value toward the 100 league mean by n/(n+k)."""
    w = pitches / (pitches + _K_STUFF) if pitches > 0 else 0.0
    return _LEAGUE_MEAN + (value - _LEAGUE_MEAN) * w


def _load_rows() -> list[dict]:
    rows = []
    try:
        with open(_CSV, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    pitches = float(r.get("pitches") or 0)
                    raw = float(r.get("stuff_plus") or 100)
                except ValueError:
                    continue
                rows.append({
                    "pitcher_id": int(float(r["pitcher"])) if r.get("pitcher") else None,
                    "name": (r.get("player_name") or "").strip(),
                    "throws": (r.get("p_throws") or "").strip(),
                    "pitch_type": (r.get("pitch_type") or "").strip(),
                    "pitches": int(pitches),
                    "stuff_raw": round(raw, 1),
                    "stuff": round(_shrink(raw, pitches), 1),  # shrunk
                    "whiff_pct": _f(r.get("whiff_pct")),
                    "barrel_pct": _f(r.get("barrel_pct")),
                    "xwoba_against": _f(r.get("xwoba_against")),
                    "velo": _f(r.get("avg_velo")),
                })
    except FileNotFoundError:
        return []
    return rows


def _f(v):
    try:
        return round(float(v), 3) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _name_flip(name: str) -> str:
    """'Rodón, Carlos' -> 'Carlos Rodón' to match the rest of the app."""
    if "," in name:
        last, first = [s.strip() for s in name.split(",", 1)]
        return f"{first} {last}"
    return name


def pitcher_leaderboard(min_pitches: int = 0) -> list[dict]:
    """Pitcher-level Stuff+ — usage-weighted across pitch types, shrunk.
    Sorted best-first. Each entry keeps its per-pitch-type breakdown."""
    global _CACHE
    if _CACHE is None:
        rows = _load_rows()
        by_pitcher: dict[int, dict] = {}
        for r in rows:
            pid = r["pitcher_id"]
            if pid is None:
                continue
            agg = by_pitcher.setdefault(pid, {
                "pitcher_id": pid, "name": _name_flip(r["name"]),
                "throws": r["throws"], "_num": 0.0, "_den": 0.0,
                "total_pitches": 0, "arsenal": [],
            })
            # usage-weight the SHRUNK per-pitch-type stuff into one number
            agg["_num"] += r["stuff"] * r["pitches"]
            agg["_den"] += r["pitches"]
            agg["total_pitches"] += r["pitches"]
            agg["arsenal"].append({
                "pitch_type": r["pitch_type"], "pitches": r["pitches"],
                "stuff": r["stuff"], "stuff_raw": r["stuff_raw"],
                "whiff_pct": r["whiff_pct"], "xwoba_against": r["xwoba_against"],
                "velo": r["velo"],
            })
        out = []
        for a in by_pitcher.values():
            if a["_den"] <= 0:
                continue
            a["stuff"] = round(a["_num"] / a["_den"], 1)
            a["arsenal"].sort(key=lambda x: -x["pitches"])
            del a["_num"], a["_den"]
            out.append(a)
        out.sort(key=lambda x: -x["stuff"])
        _CACHE = out
    if min_pitches > 0:
        return [p for p in _CACHE if p["total_pitches"] >= min_pitches]
    return _CACHE


_BY_ID: dict[int, float] | None = None

# Pitcher-level shrunk Stuff+ is ~N(100, 1.7) across the qualified pool; used to
# z-score Stuff+ as a skill signal in the dynasty + projection models.
_STUFF_MEAN = 100.0
_STUFF_SD = 1.7


def stuff_by_id() -> dict[int, float]:
    """{mlb_pitcher_id: shrunk pitcher-level Stuff+}. Cached."""
    global _BY_ID
    if _BY_ID is None:
        _BY_ID = {p["pitcher_id"]: p["stuff"] for p in pitcher_leaderboard()
                  if p["pitcher_id"] is not None}
    return _BY_ID


def stuff_for_pitcher(pitcher_id: int) -> float | None:
    """Overall shrunk Stuff+ for one MLB pitcher id, or None if not in the set."""
    return stuff_by_id().get(pitcher_id)


# TRUE coverage of the snapshot, read from the underlying pitch dates:
# 2025-04-01 → 2026-04-13. So it's a TRAILING ~12-month window (all of 2025 +
# 2026 through Apr 13), NOT a 2026-season-to-date board — which is why pitch
# counts look high for "2026" (Snell ~640, Burns ~581 span 2025 too) and why
# it's ~6 weeks stale on the front. Update both when JL re-runs the pipeline.
_SNAPSHOT_COVERAGE = "2025-04-01 → 2026-04-13"
_SNAPSHOT_AS_OF = "2026-04-13"


def snapshot_date() -> str:
    """End of the snapshot's data window (last game date covered)."""
    return _SNAPSHOT_AS_OF


def snapshot_coverage() -> str:
    """Full date range the snapshot covers (trailing ~12 months, not 2026-only)."""
    return _SNAPSHOT_COVERAGE


def stuff_z(pitcher_id: int, cap: float = 2.0) -> float | None:
    """Stuff+ as a capped z-score (process-skill signal). None if not covered."""
    v = stuff_by_id().get(pitcher_id)
    if v is None:
        return None
    return max(-cap, min((v - _STUFF_MEAN) / _STUFF_SD, cap))
