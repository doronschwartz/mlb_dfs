"""Daily MLB trivia tied to tonight's slate.

Three questions per day, auto-generated from the slate + current season stats
(no manual curation). Each question has a known correct answer at generation
time, so we can score instantly when a drafter submits. Tracks per-drafter
season-long leaderboard.

Stored at data/trivia/<date>.json. Schema:
  {
    "date": "2026-05-12",
    "generated_at": "2026-05-12T10:30:00",
    "questions": [
      {
        "id": "q1",
        "kind": "season_hr_leader",
        "prompt": "Most 2026 HRs among tonight's hitters?",
        "options": [{"label": "Aaron Judge", "value": 18}, ...],
        "correct_index": 0,
        "explainer": "Judge leads with 18 HRs (Schwarber 16, ...).",
      }, ...
    ],
    "answers": {"<drafter>": {"q1": 0, "q2": 2, "q3": 1, "score": 2}}
  }
"""
from __future__ import annotations

import json
import logging
import os
import random
from datetime import date as Date
from datetime import datetime
from pathlib import Path

from . import mlb_api, odds_api, savant

_DATA_DIR = Path(os.environ.get("MLB_DFS_DRAFT_DIR", "data/drafts")).parent / "trivia"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Bump when generator logic changes so previously-cached easy questions get
# regenerated under the new (harder) rules. Drafter answers are preserved.
_GEN_VERSION = 5


def _path(date: str) -> Path:
    return _DATA_DIR / f"{date}.json"


def _load(date: str) -> dict | None:
    p = _path(date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _save(date: str, data: dict) -> None:
    _path(date).write_text(json.dumps(data, indent=2))


def _slate_players(d: Date) -> tuple[list[dict], list[dict]]:
    """Returns (hitters, pitchers) participating in today's games.
    Each entry: {player_id, name, team_id, team_abbr}.
    Hitters: pulled from team rosters of slate teams (no lineup filter — slate
    is the universe). Pitchers: today's probable SPs only.
    """
    games = mlb_api.slate(d) or []
    if not games:
        return [], []
    team_ids: set[int] = set()
    pitchers: list[dict] = []
    for g in games:
        for side in ("home", "away"):
            team = g.get(side) or {}
            tid = team.get("id")
            if tid:
                team_ids.add(tid)
            sp = team.get("probablePitcher") or {}
            if sp.get("id"):
                pitchers.append({
                    "player_id": int(sp["id"]),
                    "name": sp.get("fullName") or sp.get("name") or str(sp["id"]),
                    "team_id": tid,
                })
    hitters: list[dict] = []
    for tid in team_ids:
        try:
            roster = mlb_api._get(f"/teams/{tid}/roster", params={"rosterType": "active"})
            for entry in roster.get("roster", []):
                pos = ((entry.get("position") or {}).get("abbreviation") or "")
                if pos in ("SP", "RP", "P"):
                    continue
                p = entry.get("person") or {}
                if p.get("id"):
                    hitters.append({
                        "player_id": int(p["id"]),
                        "name": p.get("fullName") or str(p["id"]),
                        "team_id": tid,
                    })
        except Exception as e:
            logging.debug("trivia: roster fetch failed for team %s: %s", tid, e)
    return hitters, pitchers


def _q_hr_leader(hitters: list[dict], season: int, rng: random.Random) -> dict | None:
    """Q: among tonight's slate hitters, who has the most 2026 HRs?
    Difficulty: distractors are the next-closest 3 HR totals so all 4 options
    are within striking distance — the leader isn't obvious by name."""
    # Sample wider so we catch deep slate hitters too; min 60 for tough mode.
    sample = rng.sample(hitters, min(len(hitters), 60)) if hitters else []
    scored: list[tuple[dict, int]] = []
    for h in sample:
        try:
            s = mlb_api.player_stats(h["player_id"], group="hitting", season=season)
            hr = int(float(s.get("homeRuns") or 0))
            if hr > 0:
                scored.append((h, hr))
        except Exception:
            continue
    if len(scored) < 4:
        return None
    scored.sort(key=lambda x: -x[1])
    # v4: widen pool — top 10 by HR, leader + 3 distractors from positions 1-9.
    # Includes 3rd-5th tier HR guys whose totals are close to the leader's, so
    # casual eyeballing won't cut it.
    top = scored[:10]
    leader = top[0]
    distractors = rng.sample(top[1:], min(3, len(top) - 1))
    options = [leader] + distractors
    rng.shuffle(options)
    correct = options.index(leader)
    others_str = ", ".join(f"{o[0]['name']} {o[1]}" for o in options if o is not leader)
    return {
        "id": "q1",
        "kind": "hr_leader",
        "prompt": "Who has the most 2026 HRs among tonight's slate hitters?",
        "options": [{"label": o[0]["name"], "hint": f"{o[1]} HR"} for o in options],
        "correct_index": correct,
        "explainer": f"{leader[0]['name']} leads with {leader[1]} HRs ({others_str}).",
    }


def _q_lowest_era(pitchers: list[dict], season: int, rng: random.Random) -> dict | None:
    """Q: lowest 2026 ERA among tonight's starting pitchers?"""
    scored: list[tuple[dict, float, float]] = []  # (pitcher, era, ip)
    seen_ids: set[int] = set()
    for p in pitchers:
        if p["player_id"] in seen_ids:
            continue
        seen_ids.add(p["player_id"])
        try:
            s = mlb_api.player_stats(p["player_id"], group="pitching", season=season)
            era = float(s.get("era") or 99)
            ip = float(s.get("inningsPitched") or 0)
            if ip >= 10 and era < 99:
                scored.append((p, era, ip))
        except Exception:
            continue
    if len(scored) < 4:
        return None
    scored.sort(key=lambda x: x[1])  # lowest ERA first
    # v4: widen pool — top 8 lowest ERAs. Mid-3s ERA guys masquerade as the
    # #1 — has to know exact values, not just 'who's been good lately'.
    top = scored[:8]
    leader = top[0]
    distractors = rng.sample(top[1:], min(3, len(top) - 1))
    options = [leader] + distractors
    rng.shuffle(options)
    correct = options.index(leader)
    others_str = ", ".join(f"{o[0]['name']} {o[1]:.2f}" for o in options if o is not leader)
    return {
        "id": "q2",
        "kind": "lowest_era",
        "prompt": "Lowest 2026 ERA among tonight's starting pitchers?",
        "options": [{"label": o[0]["name"], "hint": f"{o[1]:.2f} ERA"} for o in options],
        "correct_index": correct,
        "explainer": f"{leader[0]['name']} leads at {leader[1]:.2f} ERA ({others_str}).",
    }


def _q_top_team_total(d: Date, rng: random.Random) -> dict | None:
    """Q: which team has the highest implied total tonight (Vegas)?"""
    try:
        totals = odds_api.get_team_totals(d.isoformat()) or {}
    except Exception:
        return None
    if not totals:
        return None
    entries = [(abbr, t) for abbr, t in totals.items() if isinstance(t, (int, float)) and t > 0]
    if len(entries) < 4:
        return None
    entries.sort(key=lambda x: -x[1])
    # v4: top 8 highest implied totals. Implied totals cluster tightly (4.5-
    # 5.5 R is the typical band) so 8 candidates is a real challenge.
    top = entries[:8]
    leader = top[0]
    distractors = rng.sample(top[1:], min(3, len(top) - 1))
    options = [leader] + distractors
    rng.shuffle(options)
    correct = options.index(leader)
    others_str = ", ".join(f"{o[0]} {o[1]:.1f}" for o in options if o is not leader)
    return {
        "id": "q3",
        "kind": "top_team_total",
        "prompt": "Highest Vegas-implied team total tonight?",
        "options": [{"label": o[0], "hint": f"{o[1]:.1f} R"} for o in options],
        "correct_index": correct,
        "explainer": f"{leader[0]} at {leader[1]:.1f} R implied ({others_str}).",
    }


def _q_barrel_king(hitters: list[dict], season: int, rng: random.Random) -> dict | None:
    """Q: highest barrel% among tonight's slate hitters (Statcast)?"""
    sample = rng.sample(hitters, min(len(hitters), 30)) if hitters else []
    scored: list[tuple[dict, float]] = []
    for h in sample:
        try:
            qoc = savant.lookup_batter_qoc(h["player_id"], season)
            if not qoc:
                continue
            brl = float(qoc.get("brl_percent") or 0)
            if brl >= 5.0:  # filter to qualified-ish hitters with real data
                scored.append((h, brl))
        except Exception:
            continue
    if len(scored) < 4:
        return None
    scored.sort(key=lambda x: -x[1])
    # v4: widen to top 10 barrel%. The elite-barrel band tightens fast at the
    # top (15+ barrel% guys all cluster) and top-10 brings in 11-13% guys who
    # are plausible distractors.
    top = scored[:10]
    leader = top[0]
    distractors = rng.sample(top[1:], min(3, len(top) - 1))
    options = [leader] + distractors
    rng.shuffle(options)
    correct = options.index(leader)
    others_str = ", ".join(f"{o[0]['name']} {o[1]:.1f}%" for o in options if o is not leader)
    return {
        "id": "q4",
        "kind": "barrel_king",
        "prompt": "Highest 2026 barrel% among tonight's hitters (Statcast)?",
        "options": [{"label": o[0]["name"], "hint": f"{o[1]:.1f}% barrel"} for o in options],
        "correct_index": correct,
        "explainer": f"{leader[0]['name']} at {leader[1]:.1f}% barrel rate ({others_str}).",
    }


# ---- v3 question kinds: harder + numeric-guess "close counts" ---------------


def _score_numeric(guess: float, target: float, *, abs_tol: float | None = None) -> float:
    """Continuous partial-credit scorer for numeric_guess questions. v5 bands —
    still hard to ace, but 'close counts' actually rewards a close guess instead
    of a hard 0 cliff (a ~10%-off guess used to get 0; that felt punitive).
      exact / within 1%:  1.0
      within 3%:          0.80
      within 6%:          0.60
      within 10%:         0.40
      within 15%:         0.25
      within 22%:         0.10
      else:               0.00
    When `abs_tol` is provided (small-integer targets), use absolute distance:
      0 off: 1.0 · 1 off: 0.6 · 2 off: 0.35 · 3 off: 0.15 · else 0
    """
    try:
        g = float(guess); t = float(target)
    except (TypeError, ValueError):
        return 0.0
    if abs_tol is not None:
        diff = abs(g - t)
        if diff < 0.5:   return 1.0
        if diff <= 1:    return 0.60
        if diff <= 2:    return 0.35
        if diff <= 3:    return 0.15
        return 0.0
    if t == 0:
        return 1.0 if g == 0 else 0.0
    pct = abs(g - t) / abs(t)
    if pct <= 0.01: return 1.0
    if pct <= 0.03: return 0.80
    if pct <= 0.06: return 0.60
    if pct <= 0.10: return 0.40
    if pct <= 0.15: return 0.25
    if pct <= 0.22: return 0.10
    return 0.0


def _q_career_hrs(hitters: list[dict], season: int, rng: random.Random) -> dict | None:
    """Numeric guess: pick a random vet on the slate, ask their career HR count.
    Partial credit via _score_numeric — within 5% = 0.75pt, within 15% = 0.5pt,
    etc. Career HR counts are stable and verifiable, hard to guess exactly,
    fun for fantasy-baseball brains who roughly know power vets."""
    pool = list(hitters)
    rng.shuffle(pool)
    for cand in pool[:25]:  # try up to 25 random candidates
        try:
            # Career group not in our standard player_stats helper — pull directly.
            data = mlb_api._get(
                f"/people/{cand['player_id']}/stats",
                params={"stats": "career", "group": "hitting"},
            )
            splits = []
            for grp in data.get("stats", []) or []:
                splits.extend(grp.get("splits", []) or [])
            if not splits:
                continue
            stat = splits[0].get("stat", {}) or {}
            hrs = int(float(stat.get("homeRuns") or 0))
            games = int(float(stat.get("gamesPlayed") or 0))
            # Only ask vets — 100+ career HR threshold keeps it interesting (no
            # 7-HR rookies where "close" is too generous).
            if hrs >= 100 and games >= 400:
                return {
                    "id": "qn",  # renumbered downstream
                    "kind": "numeric_career_hrs",
                    "prompt": f"How many career HRs does {cand['name']} have through this season?",
                    "input": "number",   # frontend: render a number input
                    "correct_value": hrs,
                    "explainer": f"{cand['name']} has {hrs} career HRs in {games} games.",
                    "scoring": "percent",   # _score_numeric default
                }
        except Exception:
            continue
    return None


def _q_career_strikeouts(pitchers: list[dict], season: int, rng: random.Random) -> dict | None:
    """Numeric guess: career strikeouts for a random vet SP tonight. Same
    partial-credit shape as career HRs."""
    pool = list({p["player_id"]: p for p in pitchers}.values())
    rng.shuffle(pool)
    for cand in pool[:15]:
        try:
            data = mlb_api._get(
                f"/people/{cand['player_id']}/stats",
                params={"stats": "career", "group": "pitching"},
            )
            splits = []
            for grp in data.get("stats", []) or []:
                splits.extend(grp.get("splits", []) or [])
            if not splits:
                continue
            stat = splits[0].get("stat", {}) or {}
            ks = int(float(stat.get("strikeOuts") or 0))
            ip = float(stat.get("inningsPitched") or 0)
            if ks >= 300 and ip >= 200:
                return {
                    "id": "qn",
                    "kind": "numeric_career_k",
                    "prompt": f"How many career strikeouts does {cand['name']} have?",
                    "input": "number",
                    "correct_value": ks,
                    "explainer": f"{cand['name']} has {ks} career Ks in {ip:.0f} IP.",
                    "scoring": "percent",
                }
        except Exception:
            continue
    return None


def _q_slate_total_hrs_season(hitters: list[dict], season: int, rng: random.Random) -> dict | None:
    """Numeric guess: TOTAL 2026 HRs across tonight's slate hitters. Wide
    sample (top 40 by season HR), correct value computed at gen time."""
    sample = rng.sample(hitters, min(len(hitters), 80)) if hitters else []
    total = 0
    counted = 0
    for h in sample:
        try:
            s = mlb_api.player_stats(h["player_id"], group="hitting", season=season)
            hr = int(float(s.get("homeRuns") or 0))
            if hr > 0:
                total += hr
                counted += 1
        except Exception:
            continue
    if counted < 30 or total < 50:
        return None
    return {
        "id": "qn",
        "kind": "numeric_slate_hr_total",
        "prompt": f"Total 2026 HRs across tonight's {counted} slate hitters (top of each lineup)?",
        "input": "number",
        "correct_value": total,
        "explainer": f"{counted} hitters with HR data summed to {total}. Close-counts scoring.",
        "scoring": "percent",
    }


def _q_oldest_player(hitters: list[dict], pitchers: list[dict], season: int, rng: random.Random) -> dict | None:
    """Multiple choice: who is the oldest player on tonight's slate? Wider
    spread than stat-leader questions (age is rarely tracked by fantasy users,
    makes for a fun curveball)."""
    candidates = hitters + pitchers
    rng.shuffle(candidates)
    aged: list[tuple[dict, int]] = []
    for c in candidates[:50]:
        try:
            data = mlb_api._get(f"/people/{c['player_id']}")
            person = (data.get("people") or [{}])[0]
            age = int(person.get("currentAge") or 0)
            if age >= 28:
                aged.append((c, age))
        except Exception:
            continue
        if len(aged) >= 12:
            break
    if len(aged) < 4:
        return None
    aged.sort(key=lambda x: -x[1])
    top = aged[:8]
    leader = top[0]
    distractors = rng.sample(top[1:], min(3, len(top) - 1))
    options = [leader] + distractors
    rng.shuffle(options)
    correct = options.index(leader)
    others_str = ", ".join(f"{o[0]['name']} {o[1]}" for o in options if o is not leader)
    return {
        "id": "qm",
        "kind": "oldest_on_slate",
        "prompt": "Oldest player on tonight's slate?",
        "options": [{"label": o[0]["name"], "hint": f"age {o[1]}"} for o in options],
        "correct_index": correct,
        "explainer": f"{leader[0]['name']} at {leader[1]} ({others_str}).",
    }


def _q_highest_career_war_sp(pitchers: list[dict], season: int, rng: random.Random) -> dict | None:
    """Multiple choice: which of tonight's SPs has the most career wins?
    Wins isn't the best skill measure but it's a vet-detector — old aces stand
    out from mid-rotation guys. Distractors picked from top-5 closest to leader."""
    seen: set[int] = set()
    scored: list[tuple[dict, int]] = []
    for p in pitchers:
        if p["player_id"] in seen:
            continue
        seen.add(p["player_id"])
        try:
            data = mlb_api._get(
                f"/people/{p['player_id']}/stats",
                params={"stats": "career", "group": "pitching"},
            )
            splits = []
            for grp in data.get("stats", []) or []:
                splits.extend(grp.get("splits", []) or [])
            if not splits:
                continue
            stat = splits[0].get("stat", {}) or {}
            wins = int(float(stat.get("wins") or 0))
            if wins >= 5:
                scored.append((p, wins))
        except Exception:
            continue
    if len(scored) < 4:
        return None
    scored.sort(key=lambda x: -x[1])
    top = scored[:8]
    leader = top[0]
    distractors = rng.sample(top[1:], min(3, len(top) - 1))
    options = [leader] + distractors
    rng.shuffle(options)
    correct = options.index(leader)
    others_str = ", ".join(f"{o[0]['name']} {o[1]}W" for o in options if o is not leader)
    return {
        "id": "qm",
        "kind": "career_wins_sp",
        "prompt": "Most career wins among tonight's starting pitchers?",
        "options": [{"label": o[0]["name"], "hint": f"{o[1]} career W"} for o in options],
        "correct_index": correct,
        "explainer": f"{leader[0]['name']} with {leader[1]} career wins ({others_str}).",
    }


def _q_exact_ops(hitters: list[dict], season: int, rng: random.Random) -> dict | None:
    """Numeric guess: a regular's 2026 OPS to 3 decimals. With v4 bands you
    need to be within 2% (~.015 on a .700-.850 OPS) for half credit. Very
    hard — has to know slash lines, not just 'who's good'."""
    pool = list(hitters)
    rng.shuffle(pool)
    for cand in pool[:25]:
        try:
            s = mlb_api.player_stats(cand["player_id"], group="hitting", season=season)
            ops_str = s.get("ops")
            pa = int(float(s.get("plateAppearances") or 0))
            if not ops_str or pa < 100:
                continue
            ops = float(ops_str)
            if ops < 0.500 or ops > 1.200:
                continue
            return {
                "id": "qn",
                "kind": "numeric_ops",
                "prompt": f"What's {cand['name']}'s 2026 OPS? (enter as .XXX × 1000 — e.g. .812 = 812)",
                "input": "number",
                "correct_value": int(round(ops * 1000)),
                "explainer": f"{cand['name']} has a .{int(round(ops*1000)):03d} OPS in {pa} PA this season.",
                "scoring": "percent",
            }
        except Exception:
            continue
    return None


def _q_exact_era(pitchers: list[dict], season: int, rng: random.Random) -> dict | None:
    """Numeric guess: a starter's 2026 ERA × 100 (so 3.42 ERA → guess 342).
    Within 2% = 0.5pt — has to actually know the stat. Pitchers tonight only
    (so it's tied to who's actually pitching, makes it fresher than random)."""
    pool = list({p["player_id"]: p for p in pitchers}.values())
    rng.shuffle(pool)
    for cand in pool[:15]:
        try:
            s = mlb_api.player_stats(cand["player_id"], group="pitching", season=season)
            ip = float(s.get("inningsPitched") or 0)
            era_str = s.get("era")
            if not era_str or ip < 15:
                continue
            era = float(era_str)
            if era < 0.50 or era > 9.00:
                continue
            return {
                "id": "qn",
                "kind": "numeric_era",
                "prompt": f"What's {cand['name']}'s 2026 ERA? (×100 — e.g. 3.42 ERA = 342)",
                "input": "number",
                "correct_value": int(round(era * 100)),
                "explainer": f"{cand['name']}'s ERA is {era:.2f} over {ip:.1f} IP.",
                "scoring": "percent",
            }
        except Exception:
            continue
    return None


def _q_combined_career_hrs(hitters: list[dict], season: int, rng: random.Random) -> dict | None:
    """Numeric guess: combined career HRs for 3 random vets on the slate.
    Mental math + memory — adding three players' totals is genuinely hard
    when you have to do it without looking. Within 2% = half credit."""
    pool = list(hitters)
    rng.shuffle(pool)
    selected: list[tuple[dict, int]] = []
    for cand in pool[:40]:
        if len(selected) >= 3:
            break
        try:
            data = mlb_api._get(
                f"/people/{cand['player_id']}/stats",
                params={"stats": "career", "group": "hitting"},
            )
            splits = []
            for grp in data.get("stats", []) or []:
                splits.extend(grp.get("splits", []) or [])
            if not splits:
                continue
            hrs = int(float((splits[0].get("stat") or {}).get("homeRuns") or 0))
            games = int(float((splits[0].get("stat") or {}).get("gamesPlayed") or 0))
            if hrs >= 60 and games >= 300:
                selected.append((cand, hrs))
        except Exception:
            continue
    if len(selected) < 3:
        return None
    total = sum(h for _, h in selected)
    names = ", ".join(c["name"] for c, _ in selected)
    breakdown = ", ".join(f"{c['name']} {h}" for c, h in selected)
    return {
        "id": "qn",
        "kind": "numeric_combined_career_hrs",
        "prompt": f"Combined CAREER HRs for {names}?",
        "input": "number",
        "correct_value": total,
        "explainer": f"Total: {total} ({breakdown}).",
        "scoring": "percent",
    }


# Generator registry — picked at random each day so the question kinds vary.
_GENERATORS = [
    ("hr_leader", _q_hr_leader),
    ("lowest_era", _q_lowest_era),
    ("top_team_total", _q_top_team_total),
    ("barrel_king", _q_barrel_king),
    ("numeric_career_hrs", _q_career_hrs),
    ("numeric_career_k", _q_career_strikeouts),
    ("numeric_slate_hr_total", _q_slate_total_hrs_season),
    ("numeric_ops", _q_exact_ops),
    ("numeric_era", _q_exact_era),
    ("numeric_combined_career_hrs", _q_combined_career_hrs),
    ("oldest_on_slate", _q_oldest_player),
    ("career_wins_sp", _q_highest_career_war_sp),
]


# ---- "actual trivia": evergreen MLB knowledge (records/history/awards) ------
# Per league feedback (JL: "I like more actual trivia"). Each entry:
# (prompt, correct, [3 distractors], explainer). Verifiable, classic facts.
_TRIVIA_BANK: list[tuple] = [
    ("Who holds the single-season home run record?", "Barry Bonds (73)",
     ["Mark McGwire (70)", "Sammy Sosa (66)", "Aaron Judge (62)"],
     "Barry Bonds hit 73 HR in 2001 — the single-season record."),
    ("Who has the most career hits in MLB history?", "Pete Rose (4,256)",
     ["Ty Cobb (4,189)", "Hank Aaron (3,771)", "Stan Musial (3,630)"],
     "Pete Rose holds the career hits record with 4,256."),
    ("Who is MLB's career home run leader?", "Barry Bonds (762)",
     ["Hank Aaron (755)", "Babe Ruth (714)", "Albert Pujols (703)"],
     "Barry Bonds leads with 762 career home runs."),
    ("Who has the most career strikeouts as a pitcher?", "Nolan Ryan (5,714)",
     ["Randy Johnson (4,875)", "Roger Clemens (4,672)", "Steve Carlton (4,136)"],
     "Nolan Ryan struck out 5,714 batters — and threw 7 no-hitters."),
    ("Who has the most career wins by a pitcher?", "Cy Young (511)",
     ["Walter Johnson (417)", "Christy Mathewson (373)", "Warren Spahn (363)"],
     "Cy Young won 511 games — the award is named after him."),
    ("Highest career batting average in MLB history?", "Ty Cobb (.366)",
     ["Rogers Hornsby (.358)", "Shoeless Joe Jackson (.356)", "Ted Williams (.344)"],
     "Ty Cobb's .366 career average is the all-time best."),
    ("Who won the most career MVP awards?", "Barry Bonds (7)",
     ["Mike Trout (3)", "Albert Pujols (3)", "Mickey Mantle (3)"],
     "Barry Bonds won 7 MVPs, far more than anyone else."),
    ("Who won the most Cy Young awards?", "Roger Clemens (7)",
     ["Randy Johnson (5)", "Greg Maddux (4)", "Sandy Koufax (3)"],
     "Roger Clemens won 7 Cy Young awards."),
    ("How long was Joe DiMaggio's record hitting streak?", "56 games",
     ["44 games", "48 games", "61 games"],
     "DiMaggio hit safely in 56 straight games in 1941 — still the record."),
    ("Who is the career stolen base leader?", "Rickey Henderson (1,406)",
     ["Lou Brock (938)", "Ty Cobb (897)", "Billy Hamilton (914)"],
     "Rickey Henderson stole 1,406 bases — also the career runs leader."),
    ("Who has the most career saves?", "Mariano Rivera (652)",
     ["Trevor Hoffman (601)", "Lee Smith (478)", "Kenley Jansen (450)"],
     "Mariano Rivera saved 652 games and was a unanimous Hall of Famer."),
    ("Which player broke MLB's color barrier in 1947?", "Jackie Robinson",
     ["Larry Doby", "Satchel Paige", "Roy Campanella"],
     "Jackie Robinson debuted for the Dodgers in 1947; his #42 is retired league-wide."),
    ("Which franchise has the most World Series titles?", "New York Yankees (27)",
     ["St. Louis Cardinals (11)", "Oakland/Phila. A's (9)", "Boston Red Sox (9)"],
     "The Yankees have won 27 World Series — more than double any other club."),
    ("Who holds the single-season hits record (modern era)?", "Ichiro Suzuki (262)",
     ["George Sisler (257)", "Pete Rose (230)", "Rogers Hornsby (250)"],
     "Ichiro had 262 hits in 2004."),
    ("Who has the most career RBIs?", "Hank Aaron (2,297)",
     ["Albert Pujols (2,218)", "Babe Ruth (2,214)", "Alex Rodriguez (2,086)"],
     "Hank Aaron drove in 2,297 runs."),
    ("Who is the all-time leader in career runs scored?", "Rickey Henderson (2,295)",
     ["Ty Cobb (2,245)", "Barry Bonds (2,227)", "Hank Aaron (2,174)"],
     "Rickey Henderson scored 2,295 runs."),
    ("Most career no-hitters thrown by one pitcher?", "Nolan Ryan (7)",
     ["Sandy Koufax (4)", "Bob Feller (3)", "Justin Verlander (3)"],
     "Nolan Ryan threw 7 no-hitters."),
    ("Who was the last player to hit .400 in a season?", "Ted Williams (1941)",
     ["Tony Gwynn (1994)", "George Brett (1980)", "Rod Carew (1977)"],
     "Ted Williams hit .406 in 1941 — no one has hit .400 since."),
    ("Most consecutive games played (the 'Iron Man' streak)?", "Cal Ripken Jr. (2,632)",
     ["Lou Gehrig (2,130)", "Everett Scott (1,307)", "Steve Garvey (1,207)"],
     "Cal Ripken Jr. played 2,632 straight games, breaking Gehrig's record."),
    ("Who holds the record for career grand slams?", "Alex Rodriguez (25)",
     ["Lou Gehrig (23)", "Manny Ramirez (21)", "Babe Ruth (16)"],
     "A-Rod hit 25 career grand slams."),
    ("Which pitcher has the most career complete games?", "Cy Young (749)",
     ["Pud Galvin (646)", "Walter Johnson (531)", "Warren Spahn (382)"],
     "Cy Young completed 749 games — an unbreakable mark in today's game."),
    ("Who is the only player to win MVP in both leagues?", "Frank Robinson",
     ["Hank Aaron", "Willie Mays", "Alex Rodriguez"],
     "Frank Robinson won MVP in the NL (1961, Reds) and AL (1966, Orioles)."),
    ("Which pitcher holds the modern single-season strikeout record?", "Nolan Ryan (383)",
     ["Sandy Koufax (382)", "Randy Johnson (372)", "Pedro Martinez (313)"],
     "Nolan Ryan struck out 383 in 1973, edging Koufax's 382."),
]
# Defensive: keep only well-formed rows (4 fields, real explainer, 3 distractors).
_TRIVIA_BANK = [t for t in _TRIVIA_BANK if len(t) == 4 and t[3] and len(t[2]) == 3]


def _q_actual_trivia(rng: random.Random, exclude: set | None = None) -> dict | None:
    """Pick an evergreen MLB-knowledge MC question from the bank."""
    pool = [t for t in _TRIVIA_BANK if not exclude or t[0] not in exclude]
    if not pool:
        return None
    prompt, correct, distractors, explainer = rng.choice(pool)
    opts = [correct] + list(distractors)
    rng.shuffle(opts)
    return {
        "id": "q",
        "kind": "actual_trivia",
        "prompt": prompt,
        "options": [{"label": o} for o in opts],
        "correct_index": opts.index(correct),
        "explainer": explainer,
    }


# ---- LIVE actual trivia: current-season MLB stat leaders (pulled fresh) ------
# (leaderCategory, statGroup, phrase, value-suffix). The leaders endpoint ranks
# them already (rank 1 = leader, incl. ERA which ranks ascending), so #1 is the
# answer and the next few are the distractors — all genuinely close, pulled live.
_LIVE_LEADER_CATS = [
    ("homeRuns", "hitting", "home runs", "HR"),
    ("runsBattedIn", "hitting", "RBIs", "RBI"),
    ("stolenBases", "hitting", "stolen bases", "SB"),
    ("battingAverage", "hitting", "batting average", "AVG"),
    ("onBasePlusSlugging", "hitting", "OPS", "OPS"),
    ("hits", "hitting", "hits", "H"),
    ("earnedRunAverage", "pitching", "the lowest ERA", "ERA"),
    ("strikeouts", "pitching", "strikeouts", "K"),
    ("wins", "pitching", "wins", "W"),
    ("saves", "pitching", "saves", "SV"),
]


def _mlb_stat_leaders(season: int, category: str, group: str, limit: int = 6) -> list[tuple]:
    """[(name, value_str)] for a current-season MLB leaderboard, ranked."""
    try:
        d = mlb_api._get("/stats/leaders", params={
            "leaderCategories": category, "season": season,
            "sportId": 1, "statGroup": group, "limit": limit,
        })
    except Exception:
        return []
    out = []
    for cat in d.get("leagueLeaders", []) or []:
        for x in cat.get("leaders", []) or []:
            nm = (x.get("person") or {}).get("fullName")
            if nm:
                out.append((nm, x.get("value")))
    return out


def _q_live_leader(season: int, rng: random.Random, exclude: set | None = None) -> dict | None:
    """LIVE actual-trivia: who currently leads MLB in a real stat this season?
    Pulled fresh from the leaders endpoint so the answer is always current."""
    cats = [c for c in _LIVE_LEADER_CATS if not exclude or c[0] not in (exclude or set())]
    rng.shuffle(cats)
    for category, group, phrase, suffix in cats:
        leaders = _mlb_stat_leaders(season, category, group)
        # de-dup names (a guy can appear once) and need 4 distinct for MC
        seen, uniq = set(), []
        for nm, val in leaders:
            if nm not in seen:
                seen.add(nm); uniq.append((nm, val))
        if len(uniq) < 4:
            continue
        leader = uniq[0]
        opts = [leader] + uniq[1:4]
        rng.shuffle(opts)
        return {
            "id": "q",
            "kind": f"live_leader_{category}",
            "_cat": category,
            "prompt": f"Who currently leads MLB in {phrase} this season?",
            "options": [{"label": nm, "hint": f"{val} {suffix}"} for nm, val in opts],
            "correct_index": opts.index(leader),
            "explainer": f"{leader[0]} leads MLB with {leader[1]} {suffix}.",
        }
    return None


def _generate(date_str: str) -> dict:
    d = Date.fromisoformat(date_str)
    season = d.year
    rng = random.Random(date_str)  # deterministic per date
    hitters, pitchers = _slate_players(d)
    questions: list[dict] = []
    # v5 shape: weighted toward "actual trivia" pulled LIVE (per league feedback
    # — JL: "I like more actual trivia" / "need to be pulling live"). Lead with
    # 2 live current-season stat-leader questions (distinct categories), fresh
    # from the MLB API every day; fall back to the evergreen records bank only
    # if the live fetch is short-handed.
    used_cats: set = set()
    used_prompts: set = set()
    for _ in range(2):
        q = _q_live_leader(season, rng, exclude=used_cats)
        if q:
            used_cats.add(q.get("_cat"))
            q.pop("_cat", None)
        else:
            q = _q_actual_trivia(rng, exclude=used_prompts)
            if q:
                used_prompts.add(q["prompt"])
        if q:
            questions.append(q)
    # One numeric-guess (softer 'close counts' credit) for variety.
    numeric_pool = [
        lambda: _q_career_hrs(hitters, season, rng),
        lambda: _q_career_strikeouts(pitchers, season, rng),
        lambda: _q_slate_total_hrs_season(hitters, season, rng),
        lambda: _q_exact_ops(hitters, season, rng),
        lambda: _q_exact_era(pitchers, season, rng),
        lambda: _q_combined_career_hrs(hitters, season, rng),
    ]
    rng.shuffle(numeric_pool)
    for gen in numeric_pool:
        try:
            q = gen()
            if q:
                questions.append(q)
                break
        except Exception as e:
            logging.warning("trivia numeric generator failed: %s", e)
    # Always try to include the Vegas team-total MC (cheap, reliable).
    q_tt = _q_top_team_total(d, rng)
    if q_tt:
        questions.append(q_tt)
    # Round out with 2-3 more MC questions from the wider pool. Top-8/10
    # distractor windows make these meaningfully harder than v3.
    mc_pool = [
        lambda: _q_hr_leader(hitters, season, rng),
        lambda: _q_lowest_era(pitchers, season, rng),
        lambda: _q_barrel_king(hitters, season, rng),
        lambda: _q_oldest_player(hitters, pitchers, season, rng),
        lambda: _q_highest_career_war_sp(pitchers, season, rng),
    ]
    rng.shuffle(mc_pool)
    seen_mc_kinds: set[str] = set()
    for gen in mc_pool:
        if len(questions) >= 5:
            break
        try:
            q = gen()
            if q and q.get("kind") not in seen_mc_kinds:
                seen_mc_kinds.add(q["kind"])
                questions.append(q)
        except Exception as e:
            logging.warning("trivia mc generator failed: %s", e)
    # Renumber question IDs so the UI can rely on q1/q2/q3/q4 in order.
    for i, q in enumerate(questions, start=1):
        q["id"] = f"q{i}"
    return {
        "date": date_str,
        "generator_version": _GEN_VERSION,
        "generated_at": datetime.utcnow().isoformat(),
        "questions": questions,
        "answers": {},
    }


def _is_future_date(date_str: str) -> bool:
    """True if date_str is AFTER today in ET. Used to gate trivia
    generation — slate signals (probable pitchers, Vegas implied totals,
    lineups) aren't reliably available until the day-of, so generating a
    quiz for tomorrow today locks in stale data that won't match what
    actually happens. Better UX: tell the user to come back."""
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    try:
        return Date.fromisoformat(date_str) > today_et
    except Exception:
        return False


def for_date(date_str: str, force: bool = False) -> dict:
    """Return today's trivia. Generates + persists on first call. Idempotent
    after that (subsequent answers update the same record).

    Auto-regenerates if the cached file's generator_version is missing or older
    than the current _GEN_VERSION. Drafter answers from the old file are
    preserved so the leaderboard isn't lost.

    For FUTURE dates: returns a 'not_yet_available' sentinel instead of
    generating. Probable pitchers, lineups, and Vegas implied totals all
    arrive close to game time — generating a quiz today for tomorrow's
    slate would lock in signals that aren't actually valid yet."""
    if _is_future_date(date_str):
        return {
            "date": date_str,
            "not_yet_available": True,
            "reason": "Slate isn't finalized yet — probable pitchers, lineups, and Vegas lines come in closer to game time. Come back on the day of the slate.",
            "questions": [],
            "answers": {},
        }
    existing = _load(date_str) if not force else None
    if existing and existing.get("questions"):
        if int(existing.get("generator_version") or 1) >= _GEN_VERSION:
            return existing
        # Stale generator — regenerate questions but keep prior answers.
        old_answers = existing.get("answers") or {}
        data = _generate(date_str)
        # Old answers are against the old question set, so we have to discard
        # the per-question answer choices but keep the total score for the
        # season leaderboard (they answered that day).
        for drafter, rec in old_answers.items():
            data.setdefault("answers", {})[drafter] = {
                "answers": {},
                "score": int(rec.get("score") or 0),
                "submitted_at": rec.get("submitted_at"),
                "from_gen_version": int(existing.get("generator_version") or 1),
            }
        _save(date_str, data)
        return data
    data = _generate(date_str)
    _save(date_str, data)
    return data


class TriviaNotYetAvailable(Exception):
    pass


def submit_answer(date_str: str, drafter: str, answers: dict) -> dict:
    """Record a drafter's answers. Returns {score, total, correct, ...}.
    Answers from a given drafter overwrite prior answers for the same day.

    Raises TriviaNotYetAvailable for future dates — quiz isn't generated yet
    so there's nothing meaningful to score against.

    Two question shapes:
      MC: answers[qid] is the option index (0-3); +1 for exact match.
      Numeric: answers[qid] is the typed number; partial credit per
        _score_numeric (1.0 / 0.75 / 0.5 / 0.25 / 0).
    Score is summed as a float and stored. Leaderboard keeps floats.
    """
    data = for_date(date_str)
    if data.get("not_yet_available"):
        raise TriviaNotYetAvailable(data.get("reason") or "Trivia not yet available for this date")
    questions = data.get("questions") or []
    score_total = 0.0
    correct_map: dict[str, object] = {}     # qid -> correct_index OR correct_value
    saved_answers: dict[str, object] = {}
    per_q_score: dict[str, float] = {}
    for q in questions:
        qid = q["id"]
        if q.get("input") == "number" or q.get("kind", "").startswith("numeric_"):
            target = q.get("correct_value")
            correct_map[qid] = target
            user_val = answers.get(qid)
            saved_answers[qid] = user_val
            if user_val is None or user_val == "":
                pts = 0.0
            else:
                # Use absolute-tolerance scoring for small targets (career
                # HR/K counts can be 4-figure; this only triggers when target
                # is tiny which our generators don't produce, but cheap guard).
                abs_tol = 3 if isinstance(target, (int, float)) and abs(target) < 10 else None
                pts = _score_numeric(user_val, target, abs_tol=abs_tol)
            per_q_score[qid] = pts
            score_total += pts
        else:
            target = q.get("correct_index")
            correct_map[qid] = target
            try:
                user_idx = int(answers.get(qid, -1))
            except (TypeError, ValueError):
                user_idx = -1
            saved_answers[qid] = user_idx
            pts = 1.0 if user_idx == target else 0.0
            per_q_score[qid] = pts
            score_total += pts
    # Round stored score to 2 decimals so the leaderboard reads cleanly.
    score_rounded = round(score_total, 2)
    record = {
        "answers": saved_answers,
        "score": score_rounded,
        "per_q": per_q_score,
        "submitted_at": datetime.utcnow().isoformat(),
    }
    data.setdefault("answers", {})[drafter] = record
    _save(date_str, data)
    hints_map = {
        q["id"]: [o.get("hint") for o in (q.get("options") or [])]
        for q in questions
    }
    return {
        "score": score_rounded,
        "total": len(questions),
        "correct": correct_map,
        "per_q": per_q_score,
        "explainers": {q["id"]: q.get("explainer") for q in questions},
        "hints": hints_map,
    }


def result_for(date_str: str, drafter: str) -> dict | None:
    """Return a drafter's submitted answers + the full reveal (correct indices,
    explainers, hints). Used by the UI to re-show a past session to the same
    drafter — but ONLY for that drafter, so other people opening the page can't
    peek by selecting another name.
    Returns None if this drafter hasn't submitted.
    """
    data = _load(date_str)
    if not data:
        return None
    rec = (data.get("answers") or {}).get(drafter)
    if not rec:
        return None
    questions = data.get("questions") or []
    return {
        "drafter": drafter,
        "score": float(rec.get("score") or 0),
        "per_q": rec.get("per_q") or {},
        "total": len(questions),
        "answers": rec.get("answers") or {},
        "correct": {
            q["id"]: q.get("correct_value") if q.get("input") == "number" else q.get("correct_index")
            for q in questions
        },
        "explainers": {q["id"]: q.get("explainer") for q in questions},
        "hints": {q["id"]: [o.get("hint") for o in (q.get("options") or [])] for q in questions},
        "from_gen_version": rec.get("from_gen_version"),  # set if answers were carried over from an older question set
    }


def leaderboard(season_year: int | None = None) -> list[dict]:
    """Aggregate per-drafter score across all stored trivia days. Returns
    list sorted by score desc."""
    totals: dict[str, dict] = {}
    for p in sorted(_DATA_DIR.glob("*.json")):
        date_str = p.stem
        if season_year and not date_str.startswith(str(season_year)):
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        for drafter, rec in (data.get("answers") or {}).items():
            d = totals.setdefault(drafter, {"drafter": drafter, "score": 0.0, "answered_days": 0, "perfect_days": 0})
            d["score"] += float(rec.get("score") or 0)
            d["answered_days"] += 1
            n_questions = len(data.get("questions") or [])
            # Perfect = full points on all questions; numeric "close" doesn't
            # count as perfect since the binary right/wrong feel matters here.
            if n_questions and float(rec.get("score") or 0) >= n_questions - 0.01:
                d["perfect_days"] += 1
    # Round scores in the response so the UI shows clean numbers.
    for d in totals.values():
        d["score"] = round(d["score"], 2)
    return sorted(totals.values(), key=lambda x: (-x["score"], -x["perfect_days"], x["drafter"]))


def public_view(date_str: str) -> dict:
    """View safe to send to clients BEFORE they answer.

    Strips: correct_index, correct_value, explainer — obvious giveaways.
    For MC, also strips option `hint` fields ('15 HR' / '1.83 ERA') since
    the correct option is the leader on the metric.
    Keeps the `input` field (so frontend knows to render a number input vs
    radio buttons) and the `kind` (for display label / scoring rules).
    """
    data = for_date(date_str)
    if data.get("not_yet_available"):
        return {
            "date": data.get("date"),
            "not_yet_available": True,
            "reason": data.get("reason"),
            "questions": [],
            "submissions": [],
        }
    questions = []
    for q in data.get("questions") or []:
        clean_q = {
            k: v for k, v in q.items()
            if k not in ("correct_index", "correct_value", "explainer")
        }
        # MC options: keep label, drop hint
        if q.get("options"):
            clean_q["options"] = [{"label": o.get("label")} for o in (q.get("options") or [])]
        questions.append(clean_q)
    drafter_scores = [
        {"drafter": k, "score": float(v.get("score") or 0), "answered": True}
        for k, v in (data.get("answers") or {}).items()
    ]
    return {
        "date": data.get("date"),
        "questions": questions,
        "submissions": drafter_scores,
    }
