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
from . import fantrax, mlb_api, notify, odds_api, projections, umpires, weather as weather_mod

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
    drafter_override: str | None = None  # for out-of-order SP pick


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
def get_projections(date: str | None = None, refresh: bool = False):
    d = Date.fromisoformat(date) if date else Date.today()
    projs = projections.project_slate_cached(d, force_refresh=refresh)
    return {
        "date": d.isoformat(),
        "projections": [_proj_to_dict(p) for p in projs],
    }


@app.get("/api/drafts")
def list_drafts_route():
    return {"drafts": draft_mod.list_drafts()}


@app.get("/api/calibration")
def calibration(date: str):
    """For the given date, compare each projected player to their actual
    fantasy points. Returns per-player rows + aggregates we can use to spot
    where the model under/over-projects (by role, form tag, statcast tier)."""
    from . import live
    from .draft import Pick
    d = Date.fromisoformat(date)
    projs = projections.project_slate_cached(d)
    box_index = live._index_boxscores(d)
    rows = []
    for p in projs:
        lines = box_index.get(p.player_id) or []
        if not lines:
            continue
        # Reuse live._score_player by faking a Pick.
        fake = Pick(
            drafter="-", slot=("SP" if p.role == "pitcher" else "UTIL"),
            player_id=p.player_id, name=p.name, position=p.position or "-",
            role=p.role, projected_points=p.projected_points,
            pick_number=0, game_pk=None,
        )
        ps = live._score_player(fake, lines)
        if ps.game_state in ("Pre-Game", "Warmup", "Scheduled", ""):
            continue
        rows.append({
            "player_id": p.player_id, "name": p.name, "role": p.role,
            "position": p.position, "team_id": p.team_id,
            "projected": round(p.projected_points, 2),
            "actual": round(ps.points, 2),
            "diff": round(ps.points - p.projected_points, 2),
            "form_tag": (p.components or {}).get("form_tag", ""),
            "qoc_tier": (p.components or {}).get("qoc_tier", ""),
            "game_state": ps.game_state,
        })
    # Aggregates — bias = mean(actual - projected); MAE = mean|actual - projected|
    def _agg(lst):
        if not lst:
            return {"n": 0, "bias": 0, "mae": 0, "mean_proj": 0, "mean_actual": 0}
        n = len(lst)
        diffs = [r["diff"] for r in lst]
        return {
            "n": n,
            "bias": round(sum(diffs) / n, 2),
            "mae": round(sum(abs(x) for x in diffs) / n, 2),
            "mean_proj": round(sum(r["projected"] for r in lst) / n, 2),
            "mean_actual": round(sum(r["actual"] for r in lst) / n, 2),
        }
    by_role = {
        "hitter": _agg([r for r in rows if r["role"] == "hitter"]),
        "pitcher": _agg([r for r in rows if r["role"] == "pitcher"]),
    }
    by_tag = {tag: _agg([r for r in rows if r["form_tag"] == tag])
              for tag in ["HOT", "COLD", "STEADY", "ELITE", ""]}
    by_tier = {tier: _agg([r for r in rows if r["qoc_tier"] == tier])
               for tier in ["ELITE", "SOLID", "AVERAGE", "POOR", "—", ""]}
    return {
        "date": d.isoformat(),
        "rows": rows,
        "overall": _agg(rows),
        "by_role": by_role,
        "by_form_tag": by_tag,
        "by_qoc_tier": by_tier,
    }


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
        new_pick = dr.make_pick(req.slot, proj, game_pk=game_pk, drafter_override=req.drafter_override)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    draft_mod.save_draft(dr)
    if notify.is_configured():
        next_info = dr.on_the_clock()
        next_msg = f". Next up: {next_info[0]}" if next_info and next_info[0] != "*" else ""
        try:
            notify.notify(
                f"⚾ {new_pick.drafter} picked {new_pick.name} ({new_pick.slot}) — pick #{new_pick.pick_number} of {dr.total_picks()}{next_msg}"
            )
            if dr.is_complete():
                notify.notify(f"🏁 Draft {dr.date} complete — {dr.total_picks()} picks in. Live scoring is on.")
        except Exception:
            pass
    return _draft_state(dr)


class LineupRequest(BaseModel):
    date: str | None = None
    names: list[str]
    league_id: str | None = None
    team_id: str | None = None
    fantrax_players: list[dict] | None = None
    fantrax_slot_counts: dict[str, int] | None = None
    # When True, include minor-leaguers (Fantrax slot "Min"/"Minors") in the
    # ranking — those would need a call-up before they could actually start.
    allow_call_ups: bool = False
    # User-supplied list of names to force-treat as Min slot regardless of
    # what Fantrax's roster API reports. Useful when Fantrax slot data is
    # stale or the league's Min position has an unrecognized short_name.
    force_minors: list[str] | None = None
    # Same idea for bench — names here are treated as currentslot=BN so the
    # action label resolves to KEEP (BN) instead of BENCH ↓ when Fantrax's
    # slot data is stale and still says they're in an active spot.
    force_bench: list[str] | None = None


def _matchup_elapsed_fraction(sub_caption: str, today: Date) -> float:
    """Parse '(Mon May 4 - Sun May 10)' style range; return share of the week
    completed BEFORE `today`'s games (i.e. games already in the books). Falls
    back to 1.0 (full leverage) if parsing fails — same as previous behavior."""
    import re
    m = re.search(r"\(([A-Za-z]+\s+[A-Za-z]+\s+\d+)\s*-\s*([A-Za-z]+\s+[A-Za-z]+\s+\d+)", sub_caption or "")
    if not m:
        return 1.0
    year = today.year
    from datetime import datetime
    def _parse(s: str):
        try:
            return datetime.strptime(f"{s} {year}", "%a %b %d %Y").date()
        except ValueError:
            return None
    start = _parse(m.group(1))
    end = _parse(m.group(2))
    if not start or not end or end < start:
        return 1.0
    total_days = (end - start).days + 1   # inclusive
    # Games "in the books" = days strictly before `today`.
    elapsed_days = max(0, (today - start).days)
    return max(0.0, min(1.0, elapsed_days / total_days))


@app.post("/api/lineup")
def lineup_advice(req: LineupRequest):
    """For each pasted player name, look up our daily projection + Statcast.

    Inputs are matched case-insensitively, with substring fallback (e.g.,
    'Ohtani' matches 'Shohei Ohtani'). Hitters are ranked by projected pts;
    pitchers separately. Top-N of each get 'START', rest 'SIT'.
    """
    d = Date.fromisoformat(req.date) if req.date else Date.today()
    # Use cached projections when available — force_refresh adds 30s and the
    # global Refresh button is the right place for the user to bust the cache.
    projs = projections.project_slate_cached(d)
    by_lower = {p.name.lower(): p for p in projs}

    def _norm(s):
        import unicodedata
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return "".join(c.lower() for c in s if c.isalnum() or c.isspace())

    # Relievers aren't in projs (we only project probable SPs). Build a
    # secondary index of MLB pitchers whose team plays today, so unmatched
    # pitcher names can still surface as "RP — uncertain" rows.
    today_team_ids: set[int] = set()
    try:
        for g in mlb_api.schedule(d):
            status = (g.get("status") or {}).get("detailedState", "")
            if status in ("Postponed", "Cancelled", "Suspended", "Completed Early"):
                continue
            for side in ("home", "away"):
                t = ((g.get("teams") or {}).get(side) or {}).get("team") or {}
                if t.get("id"):
                    today_team_ids.add(t["id"])
    except Exception:
        pass
    rp_pool: dict[str, dict] = {}
    try:
        slate_pool = mlb_api.players_in_slate(d)
        for pid, meta in slate_pool.items():
            if meta.get("positionType") == "Pitcher" and meta.get("teamId") in today_team_ids:
                rp_pool[(meta.get("name") or "").lower()] = {
                    "name": meta.get("name"),
                    "player_id": pid,
                    "team_id": meta.get("teamId"),
                    "position": meta.get("position") or "P",
                }
    except Exception:
        pass
    # Build a last-name index for quick reliever matching.
    rp_by_lastname: dict[str, list[dict]] = {}
    for k, meta in rp_pool.items():
        parts = _norm(meta["name"] or "").split()
        if parts:
            last = parts[-1]
            SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
            if last in SUFFIXES and len(parts) >= 2:
                last = parts[-2]
            rp_by_lastname.setdefault(last, []).append(meta)

    # Pre-fetch reliever projections in parallel for everyone in the input
    # whose name's last+first-initial matches a today-playing pitcher. This
    # turns 25 sequential MLB API calls (~30s) into ~2s with 12 workers.
    from concurrent.futures import ThreadPoolExecutor
    forced_min_lower: set[str] = {n.strip().lower() for n in (req.force_minors or []) if n and n.strip()}
    # Also build a punctuation-stripped/normalized set so 'T.J Rumfield' matches 'T.J. Rumfield'.
    forced_min_norm: set[str] = {_norm(n) for n in (req.force_minors or []) if n and n.strip()}
    forced_bench_lower: set[str] = {n.strip().lower() for n in (req.force_bench or []) if n and n.strip()}
    forced_bench_norm: set[str] = {_norm(n) for n in (req.force_bench or []) if n and n.strip()}
    # Build eligibility map up front so we can filter SP-only out of the parallel pre-fetch.
    eligibility_map: dict[str, set[str]] = {}
    fp_by_name: dict[str, dict] = {}
    if req.fantrax_players:
        for fp in req.fantrax_players:
            elig = (fp.get("position") or "").upper()
            slots = {s.strip() for s in elig.replace(",", " ").split() if s.strip()}
            nlower = (fp.get("name") or "").lower()
            eligibility_map[nlower] = slots
            fp_by_name[nlower] = fp
    rp_pids_to_fetch: set[int] = set()
    for raw in req.names:
        nm = raw.strip()
        if nm.lower() in by_lower:
            continue   # already in slate projections (probable SP)
        elig = eligibility_map.get(nm.lower(), set())
        if elig == {"SP"}:
            continue   # SP-only → not pitching today if not starting
        n_parts = _norm(nm).split()
        if not n_parts: continue
        last = n_parts[-1]
        SUFFIXES2 = {"jr", "sr", "ii", "iii", "iv"}
        if last in SUFFIXES2 and len(n_parts) >= 2:
            last = n_parts[-2]
        first = n_parts[0]
        for cand in rp_by_lastname.get(last, []):
            cand_parts = _norm(cand["name"] or "").split()
            if cand_parts and cand_parts[0][:1] == first[:1]:
                rp_pids_to_fetch.add(cand["player_id"])
                break
    if rp_pids_to_fetch:
        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(lambda pid: projections.project_reliever_cats(pid, d.year), rp_pids_to_fetch))

    # Build a tighter matcher: keyed by ASCII-normalized last name, with
    # full-name disambiguation when multiple players share a last name.
    by_lastname: dict[str, list] = {}
    for p in projs:
        parts = _norm(p.name).split()
        if not parts:
            continue
        # Handle "Jr." / "II" / "III" suffixes — the real last name is the
        # token before any suffix-like word.
        SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
        real_last = parts[-1]
        if real_last in SUFFIXES and len(parts) >= 2:
            real_last = parts[-2]
        by_lastname.setdefault(real_last, []).append(p)

    # Pull current weekly matchup state for leverage weighting.
    matchup = {}
    leverage_map = {}
    if req.league_id and req.team_id:
        try:
            matchup = fantrax.get_current_matchup(req.league_id, req.team_id) or {}
            # Parse "(Mon May 4 - Sun May 10)" or "(Mon May 4 - Sun May 10, 2026)"
            # to get the period bounds, then compute how much of the week is in
            # the books as of `d`. Early in the week, no cat is actually decided,
            # so leverage scales toward neutral (1.0).
            elapsed_fraction = _matchup_elapsed_fraction(matchup.get("subCaption") or "", d)
            for cat, (my_v, opp_v) in (matchup.get("values") or {}).items():
                leverage_map[cat] = projections.category_leverage(my_v, opp_v, cat, elapsed_fraction)
            matchup["elapsed_fraction"] = round(elapsed_fraction, 2)
        except Exception:
            matchup = {}

    results = []
    for raw in req.names:
        name = raw.strip()
        if not name:
            continue
        lower = name.lower()
        # Minor-leaguer short-circuit: skip ranking unless allow_call_ups=True.
        fp_early = fp_by_name.get(lower, {})
        cur_slot_early = (fp_early.get("slot") or "").lower()
        is_forced_min = (lower in forced_min_lower) or (_norm(name) in forced_min_norm)
        # NB: "mi" is intentionally NOT in this list — MI = Middle Infield, an
        # active slot, not Minors. Variants seen across leagues: Min, Minors,
        # Minor, MiL, ML, "Minor League".
        is_min_slot = (cur_slot_early in ("min", "minors", "minor", "mil", "ml", "minor league", "minorleague"))
        if (is_forced_min or is_min_slot) and not req.allow_call_ups:
            # Is this player actually MLB-active today (just hasn't been moved
            # up in Fantrax yet)? Cross-check against today's slate pool.
            mlb_active = lower in by_lower or lower in rp_pool
            if not mlb_active:
                # Try last-name + first-initial match against slate pool.
                n_parts = _norm(name).split()
                if n_parts:
                    last_e = n_parts[-1]
                    SUFFIXES_E = {"jr", "sr", "ii", "iii", "iv"}
                    if last_e in SUFFIXES_E and len(n_parts) >= 2:
                        last_e = n_parts[-2]
                    first_e = n_parts[0]
                    for cand in by_lastname.get(last_e, []):
                        cand_first = _norm(cand.name).split()[0] if cand.name else ""
                        if cand_first[:1] == first_e[:1]:
                            mlb_active = True
                            break
                    if not mlb_active:
                        for cand_meta in rp_by_lastname.get(last_e, []):
                            cand_parts = _norm(cand_meta["name"] or "").split()
                            if cand_parts and cand_parts[0][:1] == first_e[:1]:
                                mlb_active = True
                                break
            results.append({
                "input": name,
                "matched_name": fp_early.get("name") or name,
                "role": None, "position": "Minors",
                "projection": 0.0, "cat_value": 0.0, "cat_proj": {},
                "team_id": None, "components": {},
                "playing_today": False, "is_rp": False,
                "current_slot": fp_early.get("slot"), "is_minors": True,
                "mlb_active_today": mlb_active,
                "forced_minors": is_forced_min and not is_min_slot,
            })
            continue
        proj = by_lower.get(lower)
        if not proj:
            # Strict last-name + first-initial match. No more sloppy substring.
            n_parts = _norm(name).split()
            if n_parts:
                last = n_parts[-1]
                SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
                if last in SUFFIXES and len(n_parts) >= 2:
                    last = n_parts[-2]
                first = n_parts[0] if n_parts else ""
                candidates = by_lastname.get(last, [])
                # ALWAYS require first name to match, even on single-candidate.
                # Otherwise "Kenley Jansen" picks "Danny Jansen" just because
                # Kenley isn't in today's slate.
                for cand in candidates:
                    cand_first = _norm(cand.name).split()[0] if cand.name else ""
                    if not cand_first or not first:
                        continue
                    if cand_first[:1] != first[:1]:
                        continue
                    # First initials match — accept if names are very close.
                    if cand_first == first or cand_first.startswith(first) or first.startswith(cand_first):
                        proj = cand
                        break
        # If no SP-projection match, see if this is a reliever on a team playing
        # today. ONLY consider someone a reliever today when Fantrax marks them
        # RP-eligible (or P-eligible) — pure SPs who aren't starting today
        # simply aren't pitching.
        is_rp = False
        rp_meta = None
        rp_rates = None
        if not proj:
            elig_today = eligibility_map.get(name.lower(), set())
            rp_eligible = bool(elig_today & {"RP", "P"})
            sp_only = elig_today == {"SP"}
            if rp_eligible or (not elig_today and not sp_only):
                n_parts = _norm(name).split()
                if n_parts:
                    last = n_parts[-1]
                    first = n_parts[0]
                    SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
                    if last in SUFFIXES and len(n_parts) >= 2:
                        last = n_parts[-2]
                    for cand in rp_by_lastname.get(last, []):
                        cand_parts = _norm(cand["name"] or "").split()
                        if not cand_parts: continue
                        cand_first = cand_parts[0]
                        if cand_first[:1] == first[:1]:
                            rp_meta = cand
                            rp_rates = projections.project_reliever_cats(cand["player_id"], d.year)
                            if rp_rates:
                                is_rp = True
                                break

        # H2H Categories value: project per-cat contributions, sum z-scores.
        cat_value = 0.0
        cat_proj: dict = {}
        if proj is not None:
            c = proj.components or {}
            if proj.role == "hitter":
                cat_value, cat_proj = projections.category_value_hitter(
                    proj,
                    vegas_factor=c.get("vegas_factor", 1.0),
                    park_factor=c.get("park_factor", 1.0),
                    platoon_factor=c.get("platoon_factor", 1.0),
                    order_factor=c.get("order_factor", 1.0),
                    leverage=leverage_map,
                )
            else:
                cat_value, cat_proj = projections.category_value_pitcher(
                    proj,
                    vegas_factor=c.get("vegas_factor", 1.0),
                    park_factor=c.get("park_factor", 1.0),
                    leverage=leverage_map,
                )
        elif is_rp and rp_rates:
            cat_value, cat_proj = projections.category_value_reliever(rp_rates, leverage=leverage_map)
        # Estimate FP for relievers using simple linear approx of their rates.
        rp_fp = 0.0
        if is_rp and rp_rates:
            usage = rp_rates.get("_usage", 0.35)
            ip = rp_rates.get("_ip_per_app", 1.0)
            # Outs × 0.75 + K × 1 - ER from ERA × usage
            outs_per_app = ip * 3
            er_per_app = (rp_rates["ERA"] / 9) * ip
            rp_fp = (outs_per_app * 0.75 + rp_rates["K"] / max(usage, 0.01) * 1.0 - er_per_app * 3.0) * usage
        c_components = (proj.components if proj else {})
        opp_abbr = c_components.get("opp_abbr")
        opp_sp_name = c_components.get("opp_sp_name")
        is_home = c_components.get("is_home")
        # For RPs, look up their team's matchup data.
        if is_rp and rp_meta and not opp_abbr:
            rp_tid = rp_meta.get("team_id")
            if rp_tid:
                # We need the schedule to derive opponent. Use rp_pool's team membership
                # to find what team they're on, then look up that team's matchup.
                for g in mlb_api.schedule(d):
                    teams_g = g.get("teams") or {}
                    h = (teams_g.get("home") or {}).get("team", {})
                    a = (teams_g.get("away") or {}).get("team", {})
                    if h.get("id") == rp_tid:
                        opp_abbr = projections._TEAM_ABBR.get(a.get("id", 0), "")
                        is_home = True
                        break
                    if a.get("id") == rp_tid:
                        opp_abbr = projections._TEAM_ABBR.get(h.get("id", 0), "")
                        is_home = False
                        break
        # Look up the player's CURRENT Fantrax slot (where they're slotted right
        # now). When a Fantrax pull was sent, fp_by_name was built above —
        # use it for action recommendations (KEEP / BENCH / PROMOTE).
        fp = fp_by_name.get(name.lower(), {})
        current_slot = fp.get("slot")
        # Force-bench override: if the user marked this name as already on
        # bench (Fantrax slot data may be stale), treat current_slot as BN so
        # the action label resolves to KEEP (BN) instead of BENCH ↓.
        if (lower in forced_bench_lower) or (_norm(name) in forced_bench_norm):
            current_slot = "BN"
        results.append({
            "input": name,
            "player_id": proj.player_id if proj else (rp_meta.get("player_id") if rp_meta else None),
            "matched_name": proj.name if proj else (rp_meta["name"] if rp_meta else None),
            "role": proj.role if proj else ("pitcher" if is_rp else None),
            "position": proj.position if proj else ("RP" if is_rp else None),
            "projection": proj.projected_points if proj else (round(rp_fp, 2) if is_rp else 0.0),
            "cat_value": round(cat_value, 2),
            "cat_proj": {k: round(v, 3) for k, v in cat_proj.items()},
            "team_id": proj.team_id if proj else (rp_meta["team_id"] if rp_meta else None),
            "components": c_components,
            "playing_today": (proj is not None) or is_rp,
            "is_rp": is_rp,
            "rp_usage": (rp_rates.get("_usage") if rp_rates else None),
            "opp_abbr": opp_abbr,
            "opp_sp_name": opp_sp_name,
            "is_home": is_home,
            "current_slot": current_slot,
        })
    # Apply MLB lineup confirmation: a player who's in the slate pool but whose
    # team has posted today's lineup card without them is scratched/benched IRL.
    # Tag with lineup_status so _assign can force them to BN (won't take a START
    # slot) but they still appear in the actives table — so the user gets a
    # KEEP (BN) for already-benched scratches and BENCH ↓ (was X) for scratches
    # currently in an active Fantrax slot. RPs don't appear in the batting card.
    try:
        lineup_statuses = mlb_api.lineups_by_date(d)
    except Exception:
        lineup_statuses = {}
    for r in results:
        pid = r.get("player_id")
        ls = lineup_statuses.get(pid) if pid else None
        status = ls.get("status") if ls else "pending"
        r["lineup_status"] = status
        r["scratched"] = (status == "out" and not r.get("is_rp") and not r.get("is_minors"))
    # Rank hitters / pitchers separately, mark top-N as START.
    hitters = [r for r in results if r["role"] == "hitter" and r["playing_today"]]
    pitchers = [r for r in results if r["role"] == "pitcher" and r["playing_today"]]
    # Sort by H2H Cat value (z-score sum across the 5 cats).
    hitters.sort(key=lambda r: r["cat_value"], reverse=True)
    pitchers.sort(key=lambda r: r["cat_value"], reverse=True)

    # Position-aware slot filling. Hardcode the standard MLB H2H Cat active-
    # roster shape — same authority as fantrax.get_roster's hardcoded shape.
    # Ignore frontend-supplied fantrax_slot_counts (often stale or row-counted
    # multi-period dup, which inflates SS/OF/SP and prevents BENCH labels from
    # ever appearing). 11 active hitter slots, 9 active pitcher slots.
    STANDARD_SLOTS = {
        "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1, "CI": 1, "MI": 1,
        "OF": 4, "UT": 2, "SP": 4, "RP": 3, "P": 2,
    }
    slot_capacity: dict[str, int] = dict(STANDARD_SLOTS)

    if slot_capacity:
        # Hitter slots to fill (everything that isn't a pitcher slot, BN, IR, Res).
        HITTER_SLOTS = ("C", "1B", "2B", "3B", "SS", "CI", "MI", "OF", "UT")
        PITCHER_SLOTS = ("SP", "RP", "P")
        hit_slots = {s: slot_capacity.get(s, 0) for s in HITTER_SLOTS if slot_capacity.get(s, 0) > 0}
        pit_slots = {s: slot_capacity.get(s, 0) for s in PITCHER_SLOTS if slot_capacity.get(s, 0) > 0}
        # Greedy assign in cat-value-desc order, prefer most-restrictive slot.
        SLOT_PRIORITY_HIT = ["C", "SS", "2B", "3B", "1B", "MI", "CI", "OF", "UT"]
        SLOT_PRIORITY_PIT = ["SP", "RP", "P"]

        def _assign(rows, slots, priority):
            remaining = dict(slots)
            for r in rows:
                r["slot_assignment"] = None
                if not r["playing_today"]:
                    r["recommendation"] = "OFF"
                    continue
                if r.get("scratched"):
                    # Scratched from posted lineup — can't START, force BN.
                    r["recommendation"] = "BN"
                    continue
                elig = eligibility_map.get(r["input"].lower(), set())
                # Try slot priorities in order, take first that the player is
                # eligible for AND has remaining capacity.
                placed = False
                for s in priority:
                    if remaining.get(s, 0) <= 0:
                        continue
                    if s == "UT":
                        # UT accepts any non-pitcher.
                        placed = True
                    elif s == "P":
                        placed = True
                    elif s in elig:
                        placed = True
                    elif s == "MI" and (elig & {"2B", "SS"}):
                        placed = True
                    elif s == "CI" and (elig & {"1B", "3B"}):
                        placed = True
                    elif s == "OF" and (elig & {"OF", "LF", "CF", "RF"}):
                        placed = True
                    if placed:
                        remaining[s] -= 1
                        r["slot_assignment"] = s
                        r["recommendation"] = "START"
                        break
                if not placed:
                    r["recommendation"] = "BN"
            return remaining

        _assign(hitters, hit_slots, SLOT_PRIORITY_HIT)
        _assign(pitchers, pit_slots, SLOT_PRIORITY_PIT)
    else:
        # Fallback: top-N heuristic.
        for i, r in enumerate(hitters):
            r["recommendation"] = "START" if i < 8 and r["playing_today"] else ("SIT" if r["playing_today"] else "OFF")
        for i, r in enumerate(pitchers):
            r["recommendation"] = "START" if i < 2 and r["playing_today"] else ("SIT" if r["playing_today"] else "OFF")
    minors_callup = [r["input"] for r in results if r.get("is_minors") and r.get("mlb_active_today")]
    minors_pure = [r["input"] for r in results if r.get("is_minors") and not r.get("mlb_active_today")]
    minors_list = minors_callup + minors_pure
    return {
        "date": d.isoformat(),
        "hitters": hitters,
        "pitchers": pitchers,
        "unmatched": [
            {"input": r["input"], "lineup_status": r.get("lineup_status")}
            for r in results if not r["playing_today"] and not r.get("is_minors")
        ],
        "minors": minors_list,
        "minors_callup": minors_callup,
        "minors_pure": minors_pure,
        "matchup": matchup,
        "leverage": leverage_map,
        "slot_capacity": slot_capacity,
    }


class FantraxCookieRequest(BaseModel):
    cookie: str


@app.post("/api/fantrax/cookie")
def fantrax_set_cookie(req: FantraxCookieRequest):
    fantrax.save_cookie(req.cookie)
    return {"ok": True, "authenticated": fantrax.is_authenticated()}


@app.get("/api/fantrax/auth")
def fantrax_auth_status():
    return {"authenticated": fantrax.is_authenticated()}


@app.get("/api/fantrax/league_info")
def fantrax_league_info(league_id: str, deep: str | None = None):
    try:
        return fantrax.get_league_info(league_id, deep=deep)
    except fantrax.FantraxAuthError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Fantrax: {e}")


@app.get("/api/fantrax/teams")
def fantrax_teams(league_id: str):
    try:
        return {"teams": fantrax.list_teams(league_id)}
    except fantrax.FantraxAuthError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Fantrax: {e}")


@app.get("/api/fantrax/roster")
def fantrax_roster(league_id: str, team_id: str | None = None):
    try:
        return fantrax.get_roster(league_id, team_id)
    except fantrax.FantraxAuthError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Fantrax: {e}")


@app.get("/api/fantrax/_probe")
def fantrax_probe(league_id: str, team_id: str, date: str | None = None, method: str | None = None, view: str | None = None):
    """Diagnostic probe: try many candidate Fantrax method names with daily-
    lineup-related params and surface their top-level response shape so we can
    find which one actually carries today's slot assignments. ?method=X overrides
    to call a single method directly with the supplied view/date."""
    from fantraxapi.api import Method, _request
    sess = fantrax._session()
    today = date or Date.today().isoformat()
    # Each entry: (label, Method)
    if method:
        kw = {"teamId": team_id}
        if view: kw["view"] = view
        if date: kw["date"] = date
        attempts = [(f"{method} {kw}", Method(method, **kw))]
    else:
        # Each call gets a fresh session — a malformed call kills the underlying
        # connection on Fantrax's side, so reusing the session corrupts later
        # ones. We swap out the session per attempt below.
        attempts = [
            ("getTeamRosterInfo STATS", Method("getTeamRosterInfo", teamId=team_id, view="STATS")),
            ("getTeamRosterInfo SCHEDULE_FULL", Method("getTeamRosterInfo", teamId=team_id, view="SCHEDULE_FULL")),
            ("getTeamRoster", Method("getTeamRoster", teamId=team_id)),
            ("getRosterEditMode", Method("getRosterEditMode", teamId=team_id)),
            ("getMyTeamRoster", Method("getMyTeamRoster", teamId=team_id)),
            ("getEditTeamRosterInfo", Method("getEditTeamRosterInfo", teamId=team_id)),
            ("getDailyTransactions", Method("getDailyTransactions", teamId=team_id)),
            ("getTeamFantasyTeam", Method("getTeamFantasyTeam", teamId=team_id)),
            ("getTeamLineup", Method("getTeamLineup", teamId=team_id)),
        ]
    out: dict[str, dict] = {}
    for label, m in attempts:
        # Fresh session per attempt: a malformed Fantrax method tears down the
        # connection, so a shared session leaves later calls in a broken state.
        s = fantrax._session()
        try:
            raw = _request(league_id, [m], session=s)
        except Exception as e:
            out[label] = {"error": str(e)[:300]}
            continue
        # Summarize the response shape so we can spot slot-like data.
        if isinstance(raw, dict):
            top_keys = list(raw.keys())
            sample = {}
            for k in top_keys[:5]:
                v = raw.get(k)
                if isinstance(v, list) and v:
                    sample[k] = {"type": "list", "len": len(v),
                                 "first_keys": list(v[0].keys()) if isinstance(v[0], dict) else type(v[0]).__name__}
                elif isinstance(v, dict):
                    sample[k] = {"type": "dict", "keys": list(v.keys())[:8]}
                else:
                    sample[k] = {"type": type(v).__name__, "val": str(v)[:60]}
            out[label] = {"top_keys": top_keys, "sample": sample}
        else:
            out[label] = {"type": type(raw).__name__, "val": str(raw)[:200]}
    return out




@app.get("/api/notify/test")
def notify_test():
    if not notify.is_configured():
        return {
            "configured": False,
            "hint": "Set fly secrets: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM (e.g. whatsapp:+14155238886), TWILIO_TO (comma-separated whatsapp:+15551234567)",
        }
    return notify.notify("✅ MLB DFS notifications working")


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
    end: str | None = None,
    slate_size: int = 6,
    seed_from_existing: bool = True,
):
    """Suggest a per-day slate selection across the week starting `start`
    (which must be a Sunday). End auto-derives as start+6 (full week); the
    builder skips Fri/Sat anyway so the practical range is Sun→Thu.

    Greedy: for each date, score each scheduled game by the sum of how often
    its two teams have already appeared, and pick the lowest-scoring N.
    Day games are preferred as tiebreaker (more likely to be watched live).

    If `seed_from_existing` is true, prior saved drafts on dates in or before
    `start` seed the team-counter so the schedule continues evenly from
    however many slates have already been played.
    """
    s = Date.fromisoformat(start)
    e = Date.fromisoformat(end) if end else (s + timedelta(days=6))
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
        def _is_day_game(g):
            # MLB schedule returns gameDate as ISO Z (UTC). Day games on the
            # east coast start ~17-21 UTC; night games ~23 UTC onward.
            iso = g.get("gameDate") or ""
            try:
                hour = int(iso[11:13])
                return hour < 22   # ~before 6pm ET
            except Exception:
                return False
        scored = sorted(
            games,
            key=lambda g: (
                counts[g["away"]["abbr"] or ""] + counts[g["home"]["abbr"] or ""],
                # Day games preferred (more likely to be watched live). False=0
                # sorts before True=1, so we negate.
                0 if _is_day_game(g) else 1,
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


@app.get("/api/insights")
def get_insights(date: str | None = None):
    """One row per game with vegas team totals, weather, and umpire data merged.
    Each upstream is wrapped in try/except so a slow third-party doesn't 502
    the whole tab."""
    d = Date.fromisoformat(date) if date else Date.today()
    games = mlb_api.schedule(d)
    try: totals = odds_api.get_team_totals(d.isoformat())
    except Exception: totals = {}
    try: ump_rows = umpires.umpires_for_date(d.isoformat())
    except Exception: ump_rows = []
    ump_by_pk = {u["game_pk"]: u for u in ump_rows if u.get("game_pk")}
    out = []
    for g in games:
        teams = g.get("teams") or {}
        home = (teams.get("home") or {}).get("team") or {}
        away = (teams.get("away") or {}).get("team") or {}
        home_abbr = home.get("abbreviation") or ""
        away_abbr = away.get("abbreviation") or ""
        wx = weather_mod.park_forecast(home_abbr, g.get("gameDate") or "")
        ump = ump_by_pk.get(g.get("gamePk"))
        out.append({
            "gamePk": g.get("gamePk"),
            "gameDate": g.get("gameDate"),
            "matchup": f"{away_abbr}@{home_abbr}",
            "away_abbr": away_abbr,
            "home_abbr": home_abbr,
            "away_total": totals.get(away.get("name") or ""),
            "home_total": totals.get(home.get("name") or ""),
            "weather": wx,
            "ump": ump,
        })
    return {"date": d.isoformat(), "games": out}


@app.get("/api/umpires")
def get_umpires_endpoint(date: str | None = None):
    """Assigned HP umpire per game + season-average pitcher-favor and
    derived K-factor (favor/50)."""
    d = Date.fromisoformat(date) if date else Date.today()
    return {"date": d.isoformat(), "games": umpires.umpires_for_date(d.isoformat())}


@app.get("/api/weather")
def get_weather_endpoint(date: str | None = None):
    """Per-park forecast for the slate, including HR-factor (wind direction
    relative to CF orientation × wind speed)."""
    d = Date.fromisoformat(date) if date else Date.today()
    games = mlb_api.schedule(d)
    out = []
    for g in games:
        teams = g.get("teams") or {}
        home_abbr = ((teams.get("home") or {}).get("team") or {}).get("abbreviation") or ""
        away_abbr = ((teams.get("away") or {}).get("team") or {}).get("abbreviation") or ""
        fc = weather_mod.park_forecast(home_abbr, g.get("gameDate") or "")
        out.append({
            "gamePk": g.get("gamePk"),
            "matchup": f"{away_abbr}@{home_abbr}",
            "home": home_abbr,
            "weather": fc,
        })
    return {"date": d.isoformat(), "games": out}


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
        eligible = []          # open-slot pills (only slots the user can still fill)
        position_slots = []    # what position(s) this player qualifies for, regardless of need
        for s in ["IF", "OF", "UTIL", "BN", "SP"]:
            if draft_mod._slot_eligible(s, p):
                position_slots.append(s)
                if not remaining or s in remaining:
                    eligible.append(s)
        pool.append({
            "player_id": p.player_id,
            "name": p.name,
            "position": p.position,
            "role": p.role,
            "team_id": p.team_id,
            "projected_points": p.projected_points,
            "eligible_slots": eligible,
            "position_slots": position_slots,
            "notes": list(p.notes),
            "components": p.components,
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
    # Pull live projections so the Proj column reflects the current model,
    # not the snapshot at draft time.
    try:
        live_projs = projections.project_slate_cached(Date.fromisoformat(dr.date))
        live_proj_by_id = {lp.player_id: lp for lp in live_projs}
    except Exception:
        live_proj_by_id = {}
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
                        "projected": (live_proj_by_id[p.player_id].projected_points
                                      if p.player_id in live_proj_by_id
                                      else p.projected_points),
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


def _can_jump_for_sp(dr) -> str | None:
    """Drafter name iff exactly one drafter still has open SP slots."""
    needers = []
    for d in dr.drafters:
        cnt = sum(1 for p in dr.picks if p.drafter == d and p.slot == "SP")
        if cnt < draft_mod.SLOTS.count("SP"):
            needers.append(d)
    return needers[0] if len(needers) == 1 else None


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
    try:
        projs = projections.project_slate_cached(Date.fromisoformat(dr.date))
        proj_by_id = {p.player_id: p for p in projs}
    except Exception:
        proj_by_id = {}
    def _pick_dict(p):
        ls = lineups.get(p.player_id)
        proj = proj_by_id.get(p.player_id)
        # Prefer the LIVE projection over the snapshot at draft-time so values
        # reflect the current model + latest stats. Falls back to snapshot if
        # the player isn't in today's slate (e.g., off day).
        live_pts = proj.projected_points if proj else None
        return {
            "slot": p.slot, "name": p.name, "player_id": p.player_id,
            "position": p.position, "role": p.role,
            "projected": live_pts if live_pts is not None else p.projected_points,
            "projected_at_pick": p.projected_points,
            "pick_number": p.pick_number,
            "drafter": p.drafter,
            "lineup_status": (ls.get("status") if ls else "pending"),
            "game_pk": p.game_pk,
            "game_label": labels.get(p.game_pk, "") if p.game_pk else "",
            "components": proj.components if proj else {},
            "notes": list(proj.notes) if proj else [],
        }
    return {
        "draft_id": dr.draft_id,
        "date": dr.date,
        "drafters": dr.drafters,
        "picks": [_pick_dict(p) for p in dr.picks],
        "on_the_clock": dr.on_the_clock(),
        "is_complete": dr.is_complete(),
        "sp_jump_drafter": _can_jump_for_sp(dr),
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
            "gameDate": g.get("gameDate"),
        })
    return out


def main():
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("mlb_dfs.web:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
