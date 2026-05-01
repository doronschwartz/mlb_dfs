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
from . import historic
from . import k_props
from . import live as live_mod
from . import mlb_api, odds_api, projections

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
    game_pk: int | None = None  # required if player's team has a DH in slate


class ReplaceRequest(BaseModel):
    player_id: int
    game_pk: int | None = None


class MoveRequest(BaseModel):
    new_slot: str


class UpdateGamesRequest(BaseModel):
    game_pks: list[int]


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
        game_pk = _resolve_game_pk_for_pick(dr, proj, req.game_pk)
    except HTTPException:
        raise
    try:
        dr.make_pick(req.slot, proj, game_pk=game_pk)
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
        game_pk = _resolve_game_pk_for_pick(dr, proj, req.game_pk)
    except HTTPException:
        raise
    try:
        dr.replace_pick(pick_number, proj, game_pk=game_pk)
    except ValueError as e:
        raise HTTPException(400, str(e))
    draft_mod.save_draft(dr)
    return _draft_state(dr)


@app.post("/api/drafts/{draft_id}/games")
def update_games(draft_id: str, req: UpdateGamesRequest):
    """Replace the draft's selected gamePks. Picks are not modified — they
    keep their original game_pk so previously-drafted players still score
    from their original game even if it's no longer in the slate. Pool /
    recommend / projections will be filtered to the new game_pks set."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    dr.game_pks = sorted(set(req.game_pks))
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
        # Seed from the spreadsheet's historic team-appearance counts so the
        # builder picks up where the season left off, not from zero.
        for team, n in historic.team_counts().items():
            counts[team] += int(n)
        # Include live saved drafts ONLY when the date is:
        #   1. before the rebuild range start (`ddate < s`) — otherwise it'd
        #      double-count games we're about to repick, and
        #   2. strictly in the past (`ddate < today`) — drafts created for a
        #      future day haven't been "played" so shouldn't bias the count, and
        #   3. not already covered by the historic CSV — otherwise double-count.
        today = Date.today()
        historic_dates = {e.get("date") for e in historic.standings()}
        for did in draft_mod.list_drafts():
            try:
                dr = draft_mod.load_draft(did)
            except Exception:
                continue
            try:
                ddate = Date.fromisoformat(dr.date)
            except Exception:
                continue
            if ddate >= s or ddate >= today or dr.date in historic_dates:
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

    # The friend league plays Sun-Thu only — skip Friday (weekday 4) and
    # Saturday (weekday 5) when proposing slates.
    SKIP_WEEKDAYS = {4, 5}
    days = []
    cur = s
    while cur <= e:
        if cur.weekday() in SKIP_WEEKDAYS:
            cur += timedelta(days=1)
            continue
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


@app.get("/api/stats/standings")
def stats_standings():
    """All-time standings + per-day breakdown.

    Combines two sources:
      1. Historic standings (data/historic/standings.json) — imported once
         from the spreadsheet; covers the season prior to the live system.
      2. Current saved drafts on the volume — scored live so today's totals
         update as games progress.

    Per-day rows are unioned by date; a date present in both prefers the
    live computation (fresher).
    """
    drafts_data = []
    seen_dates: set[str] = set()
    for did in draft_mod.list_drafts():
        try:
            dr = draft_mod.load_draft(did)
        except Exception:
            continue
        if not dr.picks:
            continue
        try:
            standings = live_mod.score_draft(dr)
        except Exception:
            continue
        drafts_data.append({
            "date": dr.date,
            "drafters": list(dr.drafters),
            "is_complete": dr.is_complete(),
            "source": "live",
            "standings": [
                {
                    "drafter": s.drafter,
                    "rank": s.rank,
                    "total": round(s.total, 2),
                    "full_total": round(s.full_total, 2),
                }
                for s in standings
            ],
        })
        seen_dates.add(dr.date)

    # Merge in historic days that aren't superseded by a live draft.
    for entry in historic.standings():
        if entry.get("date") in seen_dates:
            continue
        drafts_data.append({**entry, "source": "historic"})

    # Aggregate per drafter.
    by_drafter: dict[str, dict] = {}
    for entry in drafts_data:
        for s in entry["standings"]:
            m = by_drafter.setdefault(s["drafter"], {
                "drafter": s["drafter"], "rank_counts": {1: 0, 2: 0, 3: 0},
                "total_points": 0.0, "days": 0,
                "max_points": float("-inf"), "min_points": float("inf"),
            })
            m["rank_counts"][s["rank"]] = m["rank_counts"].get(s["rank"], 0) + 1
            m["total_points"] += s["total"]
            m["days"] += 1
            m["max_points"] = max(m["max_points"], s["total"])
            m["min_points"] = min(m["min_points"], s["total"])

    records = []
    for drafter, m in by_drafter.items():
        days = m["days"] or 1
        records.append({
            "drafter": drafter,
            "first": m["rank_counts"].get(1, 0),
            "second": m["rank_counts"].get(2, 0),
            "third": m["rank_counts"].get(3, 0),
            "total_points": round(m["total_points"], 2),
            "avg_points": round(m["total_points"] / days, 2),
            "max_points": (round(m["max_points"], 2) if m["max_points"] != float("-inf") else 0.0),
            "min_points": (round(m["min_points"], 2) if m["min_points"] != float("inf") else 0.0),
            "days": m["days"],
        })
    records.sort(key=lambda r: (-r["first"], -r["total_points"]))

    drafts_data.sort(key=lambda x: x["date"])
    return {"records": records, "per_day": drafts_data}


@app.get("/api/stats/players")
def stats_players(top_n: int = 50):
    """Player aggregate stats across all saved drafts AND historic picks:
    pick counts per drafter, average points per pick (overall + per drafter).

    Keyed by player name (historic data has no MLB player_id), so a player
    drafted both in the historic CSV and in a current saved draft will be
    correctly aggregated as long as the names match.
    """
    by_name: dict[str, dict] = {}
    for did in draft_mod.list_drafts():
        try:
            dr = draft_mod.load_draft(did)
        except Exception:
            continue
        if not dr.picks:
            continue
        try:
            standings = live_mod.score_draft(dr)
        except Exception:
            continue
        scored: dict[tuple[str, int], float] = {}
        for s in standings:
            for pick, ps in s.picks:
                scored[(s.drafter, pick.player_id)] = ps.points if ps else 0.0
        for p in dr.picks:
            entry = by_name.setdefault(p.name, {
                "name": p.name,
                "role": p.role,
                "position": p.position,
                "picks_by_drafter": {},
                "scores_by_drafter": {},
                "all_scores": [],
            })
            entry["picks_by_drafter"][p.drafter] = entry["picks_by_drafter"].get(p.drafter, 0) + 1
            sc = scored.get((p.drafter, p.player_id), 0.0)
            entry["scores_by_drafter"].setdefault(p.drafter, []).append(sc)
            entry["all_scores"].append(sc)

    # Historic picks: each is one (date, drafter, player, score, role).
    for h in historic.picks():
        name = h.get("player_name") or "?"
        entry = by_name.setdefault(name, {
            "name": name,
            "role": h.get("role", "hitter"),
            "position": None,
            "picks_by_drafter": {},
            "scores_by_drafter": {},
            "all_scores": [],
        })
        entry["picks_by_drafter"][h["drafter"]] = entry["picks_by_drafter"].get(h["drafter"], 0) + 1
        entry["scores_by_drafter"].setdefault(h["drafter"], []).append(h["score"])
        entry["all_scores"].append(h["score"])

    out = []
    for _, e in by_name.items():
        n = len(e["all_scores"])
        avg = sum(e["all_scores"]) / n if n else 0.0
        avg_per_drafter = {
            d: round(sum(s) / len(s), 2) if s else 0.0
            for d, s in e["scores_by_drafter"].items()
        }
        out.append({
            "name": e["name"],
            "role": e["role"],
            "position": e["position"],
            "picks_by_drafter": e["picks_by_drafter"],
            "avg_per_drafter": avg_per_drafter,
            "total_picks": n,
            "avg_per_pick": round(avg, 2),
        })
    out.sort(key=lambda x: (-x["total_picks"], -x["avg_per_pick"]))
    return {
        "hitters": [p for p in out if p["role"] == "hitter"][:top_n],
        "pitchers": [p for p in out if p["role"] == "pitcher"][:top_n],
    }


@app.get("/api/team_totals")
def get_team_totals_endpoint(date: str | None = None):
    """Vegas-implied team run totals (the-odds-api totals + spreads markets)."""
    d = Date.fromisoformat(date) if date else Date.today()
    return {
        "configured": odds_api.is_configured(),
        "date": d.isoformat(),
        "totals": odds_api.get_team_totals(d.isoformat()),
    }


@app.get("/api/k_props/odds")
def get_kprops_odds(date: str | None = None, refresh: bool = False):
    """Pull live pitcher-K prop lines from the-odds-api.com (when configured).

    Cache-first: if we already pulled odds for this date today, return the
    saved file (no API credits burned). Pass refresh=true to force a re-pull.
    Yesterday's saved file is auto-deleted on every call so we never serve
    stale data.
    """
    d = Date.fromisoformat(date) if date else Date.today()
    pitchers, meta = odds_api.get_pitcher_strikeout_lines_cached(
        d.isoformat(), force_refresh=refresh,
    )
    return {
        "configured": odds_api.is_configured(),
        "date": d.isoformat(),
        "pitchers": pitchers,
        "cached": meta["cached"],
        "fetched_at": meta["fetched_at"],
    }


@app.get("/api/k_props")
def get_k_props(date: str | None = None):
    """Predicted strikeouts for every probable SP on the slate.

    Combines pitcher K% + lineup K% (weighted by batting order) + park
    factor — same algorithm as Yaakov's K Prop Tester Colab, minus the
    Baseball Savant whiff-rate component.
    """
    d = Date.fromisoformat(date) if date else Date.today()
    return {"date": d.isoformat(), "rows": k_props.k_props_for_date(d)}


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
    team_games = _team_to_slate_gamepks(dr)
    labels = _game_label_map_full(dr)
    for p in pool:
        ls = lineups.get(p["player_id"])
        p["lineup_status"] = ls.get("status") if ls else "pending"
        slate_games = team_games.get(p.get("team_id") or 0, [])
        p["team_games_in_slate"] = [
            {"game_pk": gpk, "label": labels.get(gpk, "")}
            for gpk in slate_games
        ]
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
    team_games = _team_to_slate_gamepks(dr)
    labels = _game_label_map_full(dr)
    by_proj = {p.player_id: p for p in projs}
    for r in recs:
        ls = lineups.get(r["player_id"])
        r["lineup_status"] = ls.get("status") if ls else "pending"
        proj = by_proj.get(r["player_id"])
        slate_games = team_games.get(proj.team_id, []) if proj and proj.team_id else []
        r["team_games_in_slate"] = [
            {"game_pk": gpk, "label": labels.get(gpk, "")}
            for gpk in slate_games
        ]
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
    try:
        labels = _game_label_map_full(dr)
    except Exception:
        labels = {}
    def _pick_dict(p):
        ls = lineups.get(p.player_id)
        return {
            "slot": p.slot, "name": p.name, "player_id": p.player_id,
            "position": p.position, "role": p.role,
            "projected": p.projected_points, "pick_number": p.pick_number,
            "drafter": p.drafter,
            "lineup_status": (ls.get("status") if ls else "pending"),
            "game_pk": p.game_pk,
            "game_label": labels.get(p.game_pk, "") if p.game_pk else "",
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


def _team_to_slate_gamepks(dr) -> dict[int, list[int]]:
    """team_id -> list of gamePks the team plays *within the draft's slate*."""
    selected = set(dr.game_pks) if dr.game_pks else None
    out: dict[int, list[int]] = {}
    for g in mlb_api.schedule(Date.fromisoformat(dr.date)):
        pk = g.get("gamePk")
        if selected is not None and pk not in selected:
            continue
        for side in ("home", "away"):
            t = ((g.get("teams") or {}).get(side) or {}).get("team") or {}
            tid = t.get("id")
            if tid:
                out.setdefault(tid, []).append(pk)
    return out


def _resolve_game_pk_for_pick(dr, proj, requested_game_pk: int | None) -> int | None:
    """Decide which gamePk a pick should be tied to.

    - team has 1 slate game -> auto-resolve to it
    - team has 2+ slate games (DH) -> requested_game_pk must be one of them
    - team has 0 slate games (shouldn't happen via normal pool, but safe) -> None
    """
    team_id = proj.team_id
    if not team_id:
        return requested_game_pk
    games = _team_to_slate_gamepks(dr).get(team_id, [])
    if len(games) == 1:
        return games[0]
    if len(games) >= 2:
        if requested_game_pk in games:
            return requested_game_pk
        raise HTTPException(
            400,
            f"{proj.name}'s team has {len(games)} games in this slate "
            f"(doubleheader): you must pick which game. Options: {games}",
        )
    return requested_game_pk


def _game_label_map_full(dr) -> dict[int, str]:
    """gamePk -> short label like 'DET@ATL' or 'DET@ATL G1' for doubleheaders."""
    selected = set(dr.game_pks) if dr.game_pks else None
    games = []
    for g in mlb_api.schedule(Date.fromisoformat(dr.date)):
        pk = g.get("gamePk")
        if selected is not None and pk not in selected:
            continue
        games.append(g)
    by_pair: dict[tuple, list[dict]] = {}
    for g in games:
        teams = g.get("teams") or {}
        away_id = ((teams.get("away") or {}).get("team") or {}).get("id")
        home_id = ((teams.get("home") or {}).get("team") or {}).get("id")
        by_pair.setdefault((away_id, home_id), []).append(g)
    labels: dict[int, str] = {}
    for _, glist in by_pair.items():
        glist_sorted = sorted(glist, key=lambda g: g.get("gameDate") or "")
        for i, g in enumerate(glist_sorted):
            teams = g.get("teams") or {}
            aa = ((teams.get("away") or {}).get("team") or {}).get("abbreviation") or "?"
            ha = ((teams.get("home") or {}).get("team") or {}).get("abbreviation") or "?"
            base = f"{aa}@{ha}"
            label = base if len(glist_sorted) == 1 else f"{base} G{i+1}"
            labels[g.get("gamePk")] = label
    return labels


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
