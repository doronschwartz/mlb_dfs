"""FastAPI app exposing slate, projections, draft, and live scoring.

Also serves the static SPA from `mlb_dfs/static/`.
"""

from __future__ import annotations

import os
import time
import random
from collections import Counter
from datetime import date as Date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# A per-process boot stamp; used to cache-bust /static asset URLs across deploys.
BUILD_VERSION = str(int(time.time()))

from . import draft as draft_mod
from . import live as live_mod
from . import mlb_api, projections

app = FastAPI(title="MLB DFS", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"


# -------------------- models --------------------


class NewDraftRequest(BaseModel):
    date: str  # YYYY-MM-DD
    drafters: list[str]
    game_pks: list[int] = []  # empty = include the whole slate


class PickRequest(BaseModel):
    draft_id: str
    player_id: int
    slot: str


class ReplaceRequest(BaseModel):
    player_id: int


class MoveRequest(BaseModel):
    new_slot: str


# -------------------- API routes --------------------


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/slate")
def get_slate(date: str | None = None):
    d = Date.fromisoformat(date) if date else Date.today()
    return {"date": d.isoformat(), "games": mlb_api.slate(d)}


@app.get("/api/projections")
def get_projections(date: str | None = None):
    d = Date.fromisoformat(date) if date else Date.today()
    projs = projections.project_slate(d)
    return {
        "date": d.isoformat(),
        "projections": [_proj_to_dict(p) for p in projs],
    }


@app.get("/api/drafts")
def list_drafts_route():
    return {"drafts": draft_mod.list_drafts()}


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str):
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    return _draft_state(dr)


@app.post("/api/drafts")
def create_draft(req: NewDraftRequest):
    try:
        d = Date.fromisoformat(req.date)
    except ValueError:
        raise HTTPException(400, "bad date")
    if len(req.drafters) < 2:
        raise HTTPException(400, "need at least 2 drafters")
    dr = draft_mod.new_draft(d, req.drafters, game_pks=req.game_pks)
    draft_mod.save_draft(dr)
    return _draft_state(dr)


@app.post("/api/drafts/{draft_id}/pick")
def make_pick(draft_id: str, req: PickRequest):
    if req.draft_id != draft_id:
        raise HTTPException(400, "draft_id mismatch")
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")

    team_filter = _team_filter_for(dr)
    projs = projections.project_slate_cached(
        Date.fromisoformat(dr.date), team_filter=team_filter,
    )
    by_id = {p.player_id: p for p in projs}
    proj = by_id.get(req.player_id)
    if not proj:
        raise HTTPException(404, f"player {req.player_id} not in the draft pool")

    try:
        dr.make_pick(req.slot, proj)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    draft_mod.save_draft(dr)
    return _draft_state(dr)


@app.post("/api/drafts/{draft_id}/picks/{pick_number}/replace")
def replace_pick(draft_id: str, pick_number: int, req: ReplaceRequest):
    """Swap a drafted player for a different one (same drafter, same slot)."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    team_filter = _team_filter_for(dr)
    projs = projections.project_slate_cached(
        Date.fromisoformat(dr.date), team_filter=team_filter,
    )
    by_id = {p.player_id: p for p in projs}
    proj = by_id.get(req.player_id)
    if not proj:
        raise HTTPException(404, f"player {req.player_id} not in the draft pool")
    try:
        dr.replace_pick(pick_number, proj)
    except ValueError as e:
        raise HTTPException(400, str(e))
    draft_mod.save_draft(dr)
    return _draft_state(dr)


@app.post("/api/drafts/{draft_id}/picks/{pick_number}/move")
def move_pick(draft_id: str, pick_number: int, req: MoveRequest):
    """Move an existing pick to a different slot. If the destination slot is
    full, the moved pick swaps with the existing slot occupant — useful for
    promoting a bench player into a starting slot when the starter is OOL.
    """
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    try:
        dr.move_pick(pick_number, req.new_slot)
    except ValueError as e:
        raise HTTPException(400, str(e))
    draft_mod.save_draft(dr)
    return _draft_state(dr)


@app.get("/api/drafts/{draft_id}/picks/{pick_number}/move_targets")
def move_targets(draft_id: str, pick_number: int):
    """Slots this pick could legally be moved into."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    return {"targets": dr.eligible_target_slots(pick_number)}


@app.get("/api/schedule_builder")
def schedule_builder(
    start: str,
    end: str,
    slate_size: int = 5,
    seed_from_existing: bool = True,
):
    """Suggest a per-day slate selection across [start, end] that keeps each
    team's appearance count as even as possible.

    Greedy: for each date, score each scheduled game by the sum of how often
    its two teams have already appeared, and pick the lowest-scoring N.

    If `seed_from_existing` is true, prior saved drafts on dates in or before
    `start` seed the team-counter so the schedule continues evenly from
    however many slates have already been played.
    """
    s = Date.fromisoformat(start)
    e = Date.fromisoformat(end)
    if e < s:
        raise HTTPException(400, "end must be on/after start")

    counts: Counter[str] = Counter()
    if seed_from_existing:
        for did in draft_mod.list_drafts():
            try:
                dr = draft_mod.load_draft(did)
            except Exception:
                continue
            try:
                ddate = Date.fromisoformat(dr.date)
            except Exception:
                continue
            if ddate >= s:
                continue
            # Re-derive teams from the draft's gamePks against that day's schedule.
            try:
                games = mlb_api.schedule(ddate)
            except Exception:
                continue
            selected = set(dr.game_pks) if dr.game_pks else None
            for g in games:
                if selected is not None and g.get("gamePk") not in selected:
                    continue
                aa = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("abbreviation")
                ha = ((g.get("teams") or {}).get("home") or {}).get("team", {}).get("abbreviation")
                if aa: counts[aa] += 1
                if ha: counts[ha] += 1

    days = []
    cur = s
    while cur <= e:
        try:
            games = mlb_api.slate(cur)
        except Exception:
            cur += timedelta(days=1)
            continue
        scored = sorted(
            games,
            key=lambda g: (
                counts[g["away"]["abbr"] or ""] + counts[g["home"]["abbr"] or ""],
                # tiebreak: random-ish so reruns don't always pick the same game
                hash((g.get("gamePk", 0), cur.isoformat())) & 0xFFFF,
            ),
        )
        chosen = [g for g in scored if g["away"]["abbr"] and g["home"]["abbr"]][:slate_size]
        for g in chosen:
            counts[g["away"]["abbr"]] += 1
            counts[g["home"]["abbr"]] += 1
        days.append({
            "date": cur.isoformat(),
            "selected_games": [
                {
                    "gamePk": g["gamePk"],
                    "away_abbr": g["away"]["abbr"],
                    "home_abbr": g["home"]["abbr"],
                    "away_sp": (g["away"]["probablePitcher"] or {}).get("name", "TBD"),
                    "home_sp": (g["home"]["probablePitcher"] or {}).get("name", "TBD"),
                    "status": g.get("detailedStatus", ""),
                }
                for g in chosen
            ],
            "team_counts_after": dict(counts),
        })
        cur += timedelta(days=1)

    return {
        "start": start,
        "end": end,
        "slate_size": slate_size,
        "days": days,
        "team_counts": dict(counts),
        "min_count": min(counts.values()) if counts else 0,
        "max_count": max(counts.values()) if counts else 0,
    }


class ApplyScheduleRequest(BaseModel):
    drafters: list[str]
    days: list[dict]  # [{date: "YYYY-MM-DD", game_pks: [int]}]
    randomize_order: bool = True


@app.post("/api/schedule_builder/apply")
def apply_schedule(req: ApplyScheduleRequest):
    """Bulk-create one draft per day with the chosen slate. Drafter order is
    randomized per day if requested (each draft gets its own snake order)."""
    if len(req.drafters) < 2:
        raise HTTPException(400, "need at least 2 drafters")
    created, skipped = [], []
    for entry in req.days:
        try:
            d = Date.fromisoformat(entry["date"])
        except Exception:
            skipped.append({"date": entry.get("date"), "reason": "bad date"})
            continue
        order = list(req.drafters)
        if req.randomize_order:
            random.shuffle(order)
        try:
            dr = draft_mod.new_draft(d, order, game_pks=entry.get("game_pks") or [])
            draft_mod.save_draft(dr)
            created.append({"date": entry["date"], "drafters": order})
        except Exception as ex:
            skipped.append({"date": entry["date"], "reason": str(ex)})
    return {"created": created, "skipped": skipped}


@app.get("/api/lineups")
def get_lineups(date: str | None = None):
    d = Date.fromisoformat(date) if date else Date.today()
    return {"date": d.isoformat(), "lineups": mlb_api.lineups_by_date(d)}


@app.delete("/api/drafts/{draft_id}/last_pick")
def undo_last_pick(draft_id: str):
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    if not dr.picks:
        raise HTTPException(400, "no picks to undo")
    dr.picks.pop()
    draft_mod.save_draft(dr)
    return _draft_state(dr)


@app.post("/api/drafts/{draft_id}/reset")
def reset_draft(draft_id: str):
    """Clear all picks; keep drafters and selected games."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    dr.picks = []
    draft_mod.save_draft(dr)
    return _draft_state(dr)


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str):
    if not draft_mod.delete_draft(draft_id):
        raise HTTPException(404, f"draft {draft_id} not found")
    return {"ok": True, "deleted": draft_id}


@app.get("/api/drafts/{draft_id}/pool")
def get_pool(draft_id: str):
    """All draft-eligible players for this draft, undrafted, sorted by projection."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    team_filter = _team_filter_for(dr)
    projs = projections.project_slate_cached(
        Date.fromisoformat(dr.date), team_filter=team_filter,
    )
    picked = dr.picked_ids()
    on_clock = dr.on_the_clock()
    remaining = dr.remaining_slots(on_clock[0]) if on_clock else []
    pool = []
    for p in projs:
        if p.player_id in picked:
            continue
        eligible = []
        for s in ["IF", "OF", "UTIL", "BN", "SP"]:
            if draft_mod._slot_eligible(s, p) and (not remaining or s in remaining):
                eligible.append(s)
        pool.append({
            "player_id": p.player_id,
            "name": p.name,
            "position": p.position,
            "role": p.role,
            "team_id": p.team_id,
            "projected_points": p.projected_points,
            "eligible_slots": eligible,
            "notes": list(p.notes),
        })
    lineups = mlb_api.lineups_by_date(
        Date.fromisoformat(dr.date),
        game_pks=set(dr.game_pks) if dr.game_pks else None,
    )
    for p in pool:
        ls = lineups.get(p["player_id"])
        p["lineup_status"] = ls.get("status") if ls else "pending"
    return {
        "on_the_clock": on_clock,
        "remaining_slots": remaining,
        "pool": pool,
    }


@app.get("/api/drafts/{draft_id}/recommend")
def recommend(draft_id: str, top_n: int = 8):
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    team_filter = _team_filter_for(dr)
    projs = projections.project_slate_cached(
        Date.fromisoformat(dr.date), team_filter=team_filter,
    )
    recs = dr.recommend(projs, top_n=top_n)
    lineups = mlb_api.lineups_by_date(
        Date.fromisoformat(dr.date),
        game_pks=set(dr.game_pks) if dr.game_pks else None,
    )
    for r in recs:
        ls = lineups.get(r["player_id"])
        r["lineup_status"] = ls.get("status") if ls else "pending"
    return {
        "on_the_clock": dr.on_the_clock(),
        "recommendations": recs,
    }


@app.get("/api/drafts/{draft_id}/score")
def score(draft_id: str):
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    standings = live_mod.score_draft(dr)
    return {
        "draft_id": draft_id,
        "standings": [
            {
                "drafter": s.drafter,
                "rank": s.rank,
                "total": round(s.total, 2),
                "full_total": round(s.full_total, 2),
                "picks": [
                    {
                        "slot": p.slot,
                        "name": p.name,
                        "player_id": p.player_id,
                        "pick_number": p.pick_number,
                        "drafter": p.drafter,
                        "projected": p.projected_points,
                        "actual": (ps.points if ps and ps.played else None),
                        "raw": (ps.raw if ps else None),
                        "game_state": (ps.game_state if ps else None),
                        "counted": (ps.counted_in_total if ps else False),
                        "played": (ps.played if ps else False),
                        "lineup_status": (ps.lineup_status if ps else "pending"),
                        "promoted": (ps.promoted_from_bench if ps else False),
                        "breakdown": (ps.breakdown if ps else []),
                    }
                    for p, ps in s.picks
                ],
            }
            for s in standings
        ],
    }


# -------------------- static SPA --------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (STATIC_DIR / "index.html").read_text()
        return html.replace("__BUILD__", BUILD_VERSION)


# -------------------- helpers --------------------


def _proj_to_dict(p) -> dict:
    return {
        "player_id": p.player_id,
        "name": p.name,
        "team_id": p.team_id,
        "position": p.position,
        "role": p.role,
        "projected_points": p.projected_points,
        "components": p.components,
        "notes": p.notes,
    }


def _draft_state(dr) -> dict:
    try:
        lineups = mlb_api.lineups_by_date(
        Date.fromisoformat(dr.date),
        game_pks=set(dr.game_pks) if dr.game_pks else None,
    )
    except Exception:
        lineups = {}
    def _pick_dict(p):
        ls = lineups.get(p.player_id)
        return {
            "slot": p.slot, "name": p.name, "player_id": p.player_id,
            "position": p.position, "role": p.role,
            "projected": p.projected_points, "pick_number": p.pick_number,
            "drafter": p.drafter,
            "lineup_status": (ls.get("status") if ls else "pending"),
        }
    return {
        "draft_id": dr.draft_id,
        "date": dr.date,
        "drafters": dr.drafters,
        "picks": [_pick_dict(p) for p in dr.picks],
        "on_the_clock": dr.on_the_clock(),
        "is_complete": dr.is_complete(),
        "game_pks": list(dr.game_pks),
        "selected_games": _selected_games_summary(dr),
        "rosters": {
            d: [_pick_dict(p) for p in dr.roster_for(d)]
            for d in dr.drafters
        },
    }


def _team_filter_for(dr) -> set[int] | None:
    """Resolve a draft's selected gamePks into the set of eligible team IDs."""
    if not dr.game_pks:
        return None
    selected = set(dr.game_pks)
    teams: set[int] = set()
    for g in mlb_api.schedule(Date.fromisoformat(dr.date)):
        if g.get("gamePk") not in selected:
            continue
        for side in ("home", "away"):
            t = ((g.get("teams") or {}).get(side) or {}).get("team") or {}
            if t.get("id"):
                teams.add(t["id"])
    return teams or None


def _selected_games_summary(dr) -> list[dict]:
    if not dr.game_pks:
        return []
    selected = set(dr.game_pks)
    out = []
    for g in mlb_api.schedule(Date.fromisoformat(dr.date)):
        if g.get("gamePk") not in selected:
            continue
        away = ((g.get("teams") or {}).get("away") or {}).get("team") or {}
        home = ((g.get("teams") or {}).get("home") or {}).get("team") or {}
        out.append({
            "gamePk": g.get("gamePk"),
            "away_abbr": away.get("abbreviation"),
            "home_abbr": home.get("abbreviation"),
        })
    return out


def main():
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("mlb_dfs.web:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
