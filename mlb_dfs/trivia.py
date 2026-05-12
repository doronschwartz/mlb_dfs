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


# Generator registry — picked at random each day so the question kinds vary.
_GENERATORS = [
    ("hr_leader", _q_hr_leader),
    ("lowest_era", _q_lowest_era),
    ("top_team_total", _q_top_team_total),
    ("barrel_king", _q_barrel_king),
]


def _generate(date_str: str) -> dict:
    d = Date.fromisoformat(date_str)
    season = d.year
    rng = random.Random(date_str)  # deterministic per date
    hitters, pitchers = _slate_players(d)
    questions: list[dict] = []
    # Always include team total if available (cheapest), then 2 of the other three.
    q_tt = _q_top_team_total(d, rng)
    if q_tt:
        questions.append(q_tt)
    # Try the remaining three in random order; take first two that succeed.
    pool = [
        lambda: _q_hr_leader(hitters, season, rng),
        lambda: _q_lowest_era(pitchers, season, rng),
        lambda: _q_barrel_king(hitters, season, rng),
    ]
    rng.shuffle(pool)
    for gen in pool:
        if len(questions) >= 3:
            break
        try:
            q = gen()
            if q:
                questions.append(q)
        except Exception as e:
            logging.warning("trivia generator failed: %s", e)
    # Renumber question IDs so the UI can rely on q1/q2/q3.
    for i, q in enumerate(questions, start=1):
        q["id"] = f"q{i}"
    return {
        "date": date_str,
        "generated_at": datetime.utcnow().isoformat(),
        "questions": questions,
        "answers": {},
    }


def for_date(date_str: str, force: bool = False) -> dict:
    """Return today's trivia. Generates + persists on first call. Idempotent
    after that (subsequent answers update the same record)."""
    existing = _load(date_str) if not force else None
    if existing and existing.get("questions"):
        return existing
    data = _generate(date_str)
    _save(date_str, data)
    return data


def submit_answer(date_str: str, drafter: str, answers: dict[str, int]) -> dict:
    """Record a drafter's answers. Returns {score, correct_indices, total}.
    Answers from a given drafter overwrite prior answers for the same day."""
    data = for_date(date_str)
    questions = data.get("questions") or []
    score = 0
    correct_map: dict[str, int] = {}
    for q in questions:
        qid = q["id"]
        correct_map[qid] = q["correct_index"]
        if int(answers.get(qid, -1)) == q["correct_index"]:
            score += 1
    record = {"answers": {q["id"]: int(answers.get(q["id"], -1)) for q in questions}, "score": score, "submitted_at": datetime.utcnow().isoformat()}
    data.setdefault("answers", {})[drafter] = record
    _save(date_str, data)
    # Reveal the hidden hints on submission so the post-answer view can show
    # each option's actual metric value (e.g. '15 HR') alongside the ✓/✗.
    hints_map = {
        q["id"]: [o.get("hint") for o in (q.get("options") or [])]
        for q in questions
    }
    return {
        "score": score,
        "total": len(questions),
        "correct": correct_map,
        "explainers": {q["id"]: q.get("explainer") for q in questions},
        "hints": hints_map,
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
            d = totals.setdefault(drafter, {"drafter": drafter, "score": 0, "answered_days": 0, "perfect_days": 0})
            d["score"] += int(rec.get("score") or 0)
            d["answered_days"] += 1
            n_questions = len(data.get("questions") or [])
            if n_questions and int(rec.get("score") or 0) == n_questions:
                d["perfect_days"] += 1
    return sorted(totals.values(), key=lambda x: (-x["score"], -x["perfect_days"], x["drafter"]))


def public_view(date_str: str) -> dict:
    """View safe to send to clients BEFORE they answer.

    Strips: correct_index, explainer (obvious giveaways), AND option `hint`
    fields like '15 HR' / '1.83 ERA' / '5.8 R' — these trivially reveal the
    answer since the correct option is the leader on the metric. Hints are
    only revealed in the submit response (alongside the explainer).
    """
    data = for_date(date_str)
    questions = []
    for q in data.get("questions") or []:
        clean_q = {k: v for k, v in q.items() if k not in ("correct_index", "explainer")}
        clean_q["options"] = [{"label": o.get("label")} for o in (q.get("options") or [])]
        questions.append(clean_q)
    drafter_scores = [
        {"drafter": k, "score": int(v.get("score") or 0), "answered": True}
        for k, v in (data.get("answers") or {}).items()
    ]
    return {
        "date": data.get("date"),
        "questions": questions,
        "submissions": drafter_scores,
    }
