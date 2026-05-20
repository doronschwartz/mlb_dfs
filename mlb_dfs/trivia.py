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
_GEN_VERSION = 3


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
    # Take top 6 by HR, pick leader + 3 random distractors from positions 1-5.
    # This keeps all 4 options as legitimate HR threats (top 6 of the slate).
    top = scored[:6]
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
    # Take top 5 (lowest ERAs), leader + 3 close-competitor distractors.
    # All options are sub-4 ERA territory — has to actually know who's been ace.
    top = scored[:5]
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
    # Take top 5 (highest implied totals), leader + 3 close-competitor distractors.
    # All options are high-total teams — won't be obvious which is #1.
    top = entries[:5]
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
    # Take top 6 by barrel%, leader + 3 close-competitor distractors.
    # All options are elite barrel-rate guys, ~3-5 point spread from #1 to #6.
    top = scored[:6]
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
    """Continuous partial-credit scorer for numeric_guess questions. Returns
    a fraction in [0, 1] that the submit handler multiplies by the question's
    point value. Bands:
      exact / within 1%:   1.0
      within 5%:           0.75
      within 15%:          0.50
      within 30%:          0.25
      else:                0.00
    When `abs_tol` is provided (small-integer targets like 'career HRs = 4'
    where % bands are silly), use absolute distance: 0 off = 1.0, 1 off = 0.75,
    2 off = 0.5, 3 off = 0.25."""
    try:
        g = float(guess); t = float(target)
    except (TypeError, ValueError):
        return 0.0
    if abs_tol is not None:
        diff = abs(g - t)
        if diff < 0.5:   return 1.0
        if diff <= 1:    return 0.75
        if diff <= 2:    return 0.50
        if diff <= 3:    return 0.25
        return 0.0
    if t == 0:
        return 1.0 if g == 0 else 0.0
    pct = abs(g - t) / abs(t)
    if pct <= 0.01:  return 1.0
    if pct <= 0.05:  return 0.75
    if pct <= 0.15:  return 0.50
    if pct <= 0.30:  return 0.25
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
    top = aged[:5]
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
    top = scored[:5]
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


# Generator registry — picked at random each day so the question kinds vary.
_GENERATORS = [
    ("hr_leader", _q_hr_leader),
    ("lowest_era", _q_lowest_era),
    ("top_team_total", _q_top_team_total),
    ("barrel_king", _q_barrel_king),
    ("numeric_career_hrs", _q_career_hrs),
    ("numeric_career_k", _q_career_strikeouts),
    ("numeric_slate_hr_total", _q_slate_total_hrs_season),
    ("oldest_on_slate", _q_oldest_player),
    ("career_wins_sp", _q_highest_career_war_sp),
]


def _generate(date_str: str) -> dict:
    d = Date.fromisoformat(date_str)
    season = d.year
    rng = random.Random(date_str)  # deterministic per date
    hitters, pitchers = _slate_players(d)
    questions: list[dict] = []
    # v3 shape: aim for 4 questions, mixing kinds. ALWAYS try to include
    # 1 numeric-guess (partial credit, "close counts") for the fun + skill
    # angle. Cap MC questions at 3 so the quiz doesn't feel same-y.
    # First slot: numeric guess — try each numeric generator in random order.
    numeric_pool = [
        lambda: _q_career_hrs(hitters, season, rng),
        lambda: _q_career_strikeouts(pitchers, season, rng),
        lambda: _q_slate_total_hrs_season(hitters, season, rng),
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
    # Always include the Vegas team-total MC (cheap, reliable, daily variety).
    q_tt = _q_top_team_total(d, rng)
    if q_tt:
        questions.append(q_tt)
    # Round out with 2-3 more MC questions from the wider pool.
    mc_pool = [
        lambda: _q_hr_leader(hitters, season, rng),
        lambda: _q_lowest_era(pitchers, season, rng),
        lambda: _q_barrel_king(hitters, season, rng),
        lambda: _q_oldest_player(hitters, pitchers, season, rng),
        lambda: _q_highest_career_war_sp(pitchers, season, rng),
    ]
    rng.shuffle(mc_pool)
    for gen in mc_pool:
        if len(questions) >= 4:
            break
        try:
            q = gen()
            if q:
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


def for_date(date_str: str, force: bool = False) -> dict:
    """Return today's trivia. Generates + persists on first call. Idempotent
    after that (subsequent answers update the same record).

    Auto-regenerates if the cached file's generator_version is missing or older
    than the current _GEN_VERSION. Drafter answers from the old file are
    preserved so the leaderboard isn't lost."""
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


def submit_answer(date_str: str, drafter: str, answers: dict) -> dict:
    """Record a drafter's answers. Returns {score, total, correct, ...}.
    Answers from a given drafter overwrite prior answers for the same day.

    Two question shapes:
      MC: answers[qid] is the option index (0-3); +1 for exact match.
      Numeric: answers[qid] is the typed number; partial credit per
        _score_numeric (1.0 / 0.75 / 0.5 / 0.25 / 0).
    Score is summed as a float and stored. Leaderboard keeps floats.
    """
    data = for_date(date_str)
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
