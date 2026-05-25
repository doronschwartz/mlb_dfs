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


@app.on_event("startup")
async def _prewarm_projections():
    """Compute today's projections in a background thread on startup so the
    first user request doesn't pay the ~30-60s cold-cache cost. If it fails,
    log and move on — the on-demand computation will still work."""
    import threading, logging as _log
    def _warm():
        try:
            from . import projections as _p
            d = Date.today()
            _p.project_slate_cached(d)
            _log.info("projection pre-warm complete for %s", d.isoformat())
        except Exception as e:
            _log.warning("projection pre-warm failed: %s", e)
    threading.Thread(target=_warm, daemon=True).start()

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


@app.get("/api/dynasty_rankings")
def dynasty_rankings():
    """Top-500 dynasty rankings. Prefers data/dynasty_top500.csv (the real
    FantraxHQ export, 500 names with positions, teams, ages); falls back to
    the legacy hand-curated data/dynasty_top500.txt if the CSV is missing.

    Returns {"rankings": [name, ...]} for backwards-compat with the existing
    frontend, plus {"meta": [...]} with the per-player metadata (pos, team,
    age) for any future UI that wants to surface it. Rank is the Roto column
    (2nd col), which is the consensus FantraxHQ rank — the Points column
    (1st col) is points-league-skewed and ranks pitchers far higher than
    most leagues actually do.
    """
    import csv, os
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    csv_path = os.path.join(data_dir, "dynasty_top500.csv")
    txt_path = os.path.join(data_dir, "dynasty_top500.txt")

    rankings: list[str] = []
    meta: list[dict] = []

    if os.path.exists(csv_path):
        try:
            with open(csv_path, newline="") as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    name = (row.get("Player") or "").strip()
                    if not name:
                        continue
                    try:
                        roto_rank = int(row.get("Roto", "").strip() or 0)
                    except (TypeError, ValueError):
                        roto_rank = 0
                    if not roto_rank:
                        continue
                    meta.append({
                        "rank": roto_rank,
                        "name": name,
                        "pos": (row.get("Pos.") or "").strip(),
                        "team": (row.get("Team") or "").strip(),
                        "age": (row.get("Age") or "").strip(),
                    })
            meta.sort(key=lambda x: x["rank"])
            rankings = [m["name"] for m in meta]
        except Exception:
            rankings = []
            meta = []

    if not rankings and os.path.exists(txt_path):
        try:
            with open(txt_path) as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#"):
                        rankings.append(s)
        except Exception:
            pass

    return {"rankings": rankings, "meta": meta, "n": len(rankings)}


@app.get("/api/health")
def health():
    return {"ok": True}


# ---- Dynasty: our rankings, predictions, trade analyzer ---------------------

@app.get("/api/dynasty/rankings")
def dynasty_our_rankings(limit: int = 500, season: int | None = None):
    """OUR dynasty rankings — consensus prior re-shaped by age curve, position
    scarcity, and Statcast luck. Each row carries our_rank vs consensus_rank
    so the UI can show where we disagree with the market."""
    from . import dynasty
    yr = season or Date.today().year
    return {"season": yr, "rankings": dynasty.rankings(yr, limit=limit)}


@app.get("/api/dynasty/player/{name}")
def dynasty_player(name: str, season: int | None = None):
    """Full dynasty valuation + multi-year projection curve for one player."""
    from . import dynasty
    yr = season or Date.today().year
    v = dynasty.dynasty_value(dynasty._norm(name), yr)
    if not v:
        raise HTTPException(404, f"'{name}' not in the dynasty pool (top-500 consensus)")
    return v


class DynastyTradeRequest(BaseModel):
    side_a: list[str]
    side_b: list[str]
    season: int | None = None


@app.post("/api/dynasty/trade")
def dynasty_trade(req: DynastyTradeRequest):
    """Evaluate a dynasty trade — per-side value, winner, fairness verdict,
    and win-now/rebuild + consolidation context."""
    from . import dynasty
    if not req.side_a or not req.side_b:
        raise HTTPException(400, "both sides need at least one player")
    yr = req.season or Date.today().year
    return dynasty.evaluate_trade(req.side_a, req.side_b, yr)


class AskAlgoRequest(BaseModel):
    names: list[str]
    date: str | None = None


@app.post("/api/ask_algo")
def ask_algo(req: AskAlgoRequest):
    """Look up projections for a free-form list of player names. Used by the
    Ask Algo tab — paste a roster, get back ranked projections + matchup
    context for each player on the date. Name matching is fuzzy: case-
    insensitive substring, accent-insensitive, common-suffix-stripped.
    """
    d = Date.fromisoformat(req.date) if req.date else Date.today()
    # Use the cached slate — /api/projections already pre-warmed this on startup
    # and the cache is per-day + per-MODEL_REV with stampede protection. Calling
    # the uncached project_slate would recompute ~400 projections every time
    # (~10s) instead of returning the cached version (<10ms). Same data, vastly
    # faster.
    try:
        projections_today = projections.project_slate_cached(d, team_filter=None)
    except Exception as e:
        raise HTTPException(500, f"projection slate failed: {e}")

    import unicodedata
    def _norm(s: str) -> str:
        if not s:
            return ""
        nfkd = unicodedata.normalize("NFKD", s)
        no_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
        return no_accent.lower().replace(".", "").replace("'", "").strip()

    by_name_norm: dict[str, dict] = {}
    for p in projections_today:
        nm = _norm(getattr(p, "name", "") or "")
        if nm:
            by_name_norm[nm] = p

    out: list[dict] = []
    missing: list[str] = []
    for raw_name in req.names:
        query = _norm(raw_name)
        if not query:
            continue
        match = None
        # 1) Exact normalized match
        if query in by_name_norm:
            match = by_name_norm[query]
        else:
            # 2) Last-name + first-initial fuzzy match
            for nm, p in by_name_norm.items():
                # check substring both ways with at-least 5-char overlap
                if len(query) >= 5 and (query in nm or nm in query):
                    match = p
                    break
                # Last name match: split query by spaces, take last token
                q_last = query.split()[-1] if query.split() else query
                p_last = nm.split()[-1] if nm.split() else nm
                if len(q_last) >= 4 and q_last == p_last:
                    match = p
                    break
        if match:
            out.append({
                "query": raw_name,
                "name": match.name,
                "projected_points": match.projected_points,
                "role": match.role,
                "position": match.position,
                "components": match.components,
                "notes": match.notes,
            })
        else:
            missing.append(raw_name)
    out.sort(key=lambda x: -(x.get("projected_points") or 0))
    return {
        "date": d.isoformat(),
        "matched": out,
        "missing": missing,
        "total_slate_projections": len(projections_today),
    }


@app.get("/api/league_averages")
def get_league_averages(season: int | None = None, refresh: bool = False):
    """Surfaces the current league baselines (barrel%, hh%, xERA, xwOBA,
    sweet-spot%) that drive the projection algo. Cached 24h, auto-refreshes
    on first request after expiry. Pass ?refresh=true to force a re-fetch
    from Statcast right now."""
    from . import savant
    s = season or Date.today().year
    if refresh:
        savant._LG_CACHE.pop(s, None)
    lg = savant.league_averages(s)
    cached = savant._LG_CACHE.get(s)
    fetched_at = cached[0] if cached else None
    age_seconds = (time.time() - fetched_at) if fetched_at else None
    return {
        "season": s,
        "averages": lg,
        "fetched_at": fetched_at,
        "age_seconds": age_seconds,
        "ttl_seconds": savant._LG_TTL,
    }


@app.post("/api/admin/draft/{draft_id}/normalize_ooo")
def admin_normalize_ooo(draft_id: str):
    """Replay a draft's pick sequence under the CURRENT OOO rules and
    rewrite each pick's out_of_order flag. Fixes drafts where picks were
    stored under an older (now-reverted) rule that incorrectly flagged
    natural-turn picks as OOO, which makes on_the_clock skip drafters."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    original = list(dr.picks)
    fixed_count = 0
    # Replay: clear all picks, then re-add one at a time and recompute
    # what the OOO flag SHOULD be at that step.
    dr.picks = []
    for orig in original:
        info = dr.on_the_clock()
        if info is None:
            # Draft was full but original had more picks — keep the rest as-is
            dr.picks.append(orig)
            continue
        on_clock_drafter, _slot = info
        # Under current rules: pick is OOO iff drafter != on_clock_drafter
        # (and the off-turn picker was the lone-SP-needer or non-SP free-for-all).
        # If drafter matches on_clock, pick is natural-turn → OOO=False.
        should_be_ooo = (orig.drafter != on_clock_drafter)
        if orig.out_of_order != should_be_ooo:
            fixed_count += 1
        # Build a corrected Pick. The dataclass is mutable, so just set the flag.
        corrected = draft_mod.Pick(
            drafter=orig.drafter, slot=orig.slot,
            player_id=orig.player_id, name=orig.name,
            position=orig.position, role=orig.role,
            projected_points=orig.projected_points,
            pick_number=orig.pick_number,
            game_pk=orig.game_pk,
            out_of_order=should_be_ooo,
        )
        dr.picks.append(corrected)
    draft_mod.save_draft(dr)
    return {
        "draft_id": draft_id,
        "total_picks": len(dr.picks),
        "flags_fixed": fixed_count,
    }


@app.post("/api/admin/cache_gc")
def admin_cache_gc():
    """Force-evict the disk cache down to the LRU target. No auth — there's
    nothing destructive here (cache entries are reproducible), but limit to
    POST so accidental GET-loads don't trigger it."""
    from . import disk_cache
    return disk_cache.gc(force=True)


# ---------- Daily MLB trivia ----------

@app.get("/api/trivia/{date}")
def get_trivia(date: str):
    from . import trivia as trivia_mod
    return trivia_mod.public_view(date)


@app.get("/api/trivia/{date}/result/{drafter}")
def get_trivia_result(date: str, drafter: str):
    """A drafter's own past submission (with full reveal). Only intended for
    the drafter themselves to view after submitting — there's no auth, so
    anyone could request another drafter's result, but the UI never does."""
    from . import trivia as trivia_mod
    result = trivia_mod.result_for(date, drafter)
    if not result:
        raise HTTPException(404, "no submission for this drafter on this date")
    return result


class TriviaSubmission(BaseModel):
    drafter: str
    # v4: answers carry MC option indices (int) AND numeric-guess values
    # (int or float), so the dict value type is loose.
    answers: dict[str, object]   # {"q1": 0, "q2": 250, ...}


@app.post("/api/trivia/{date}/answer")
def post_trivia_answer(date: str, req: TriviaSubmission):
    from . import trivia as trivia_mod
    if not req.drafter:
        raise HTTPException(400, "drafter required")
    try:
        return trivia_mod.submit_answer(date, req.drafter, req.answers)
    except trivia_mod.TriviaNotYetAvailable as e:
        # 425 Too Early — slate signals (probables / lineups / Vegas) aren't
        # ready yet, so there's no valid quiz to score against.
        raise HTTPException(425, str(e))


@app.get("/api/trivia/leaderboard/season")
def get_trivia_leaderboard(season: int | None = None):
    from . import trivia as trivia_mod
    return {"leaderboard": trivia_mod.leaderboard(season)}


# ---------- Hall of Fame (all-time records across imported seasons) ----------

@app.get("/api/records")
def get_records(top_n: int = 10, season: int | None = None):
    """Aggregate all-time records & history. Pass ?season=2024 to filter to
    a single season, otherwise returns combined across all imported seasons."""
    from . import records as records_mod
    return records_mod.all_records(top_n=top_n, season=season)


@app.get("/api/changelog")
def get_changelog():
    """Model + product changelog — surfaces improvements to the projection
    engine AND product since the project shipped on 2026-04-30. Read by the
    in-app Changelog button. Entries newest → oldest."""
    return {
        "current": projections.MODEL_REV,
        "entries": [
            {
                "version": "Dynasty v1.4 — 2026-05-25",
                "title": "Dynasty: multi-year durability tendency",
                "changes": [
                    "Durability factor from games-played over the two prior completed seasons — turns the injury signal from a current-status snapshot into a real track record. Hitters: ≥145 G/yr ×1.0 down to ×0.91 for <110; pitchers by starts/yr. Examples: Trout ×0.91 (~80 G/yr), Acuña ×0.91 (~72 post-ACL), Buxton ×0.95, Cole ×0.91 (~17 starts); iron men (José Ramírez, Judge) stay ×1.0.",
                    "Complements the current-injury factor: a chronically banged-up star gets a standing dynasty discount even when healthy today — something the consensus is slow to price. Neutral for players without 2 yrs of MLB history.",
                    "373 players assessed; per-player yearByYear cached 24h + parallelized.",
                ],
            },
            {
                "version": "Dynasty v1.3 — 2026-05-25",
                "title": "Dynasty: multi-year Statcast skill + trajectory + gentler pitcher aging",
                "changes": [
                    "Skill is now MULTI-YEAR (current + 2 prior seasons), each year weighted by sample × recency. Fixes the core flaw: a 40-IP injured 2026 was burying established aces — Skubal (consensus #5) had fallen to #45 below Jeremy Peña on one down sample. His 3-yr weighted xERA is 2.76 (elite), so he's now #20 above Peña #49, with the trajectory factor noting his mild YoY dip.",
                    "Trajectory factor (±4%): this season's xwOBA/xERA vs the prior baseline — ascending true talent (a dynasty buy) gets a nudge up, sliding skill a nudge down. Catches what the consensus is slow to price.",
                    "Confidence-weighted blend: the skill weight scales with sample, so a thin/early sample leans on the consensus (career proxy) rather than overriding it.",
                    "Gentler pitcher age curve (0.040 → 0.028/yr decline, peak 26→27) — the old curve buried aces in their early 30s harder than the market does.",
                ],
            },
            {
                "version": "Dynasty v1.2 — 2026-05-25",
                "title": "Dynasty: injury risk, prospect ETA, multi-pos & young-upside factors",
                "changes": [
                    "Injury-risk discount baked into dynasty value from the ESPN feed's current status: 60-day IL ×0.90, 15-day ×0.95, 10-day ×0.97, day-to-day ×0.99. Pitchers carry an extra ×0.97 standing arm-risk haircut (kept light — the steeper pitcher age curve already prices some of it).",
                    "Prospect ETA factor: the CSV ETA column is a signal the consensus rank under-weights — a 2026 arrival is worth more than a 2028 lottery ticket (sooner value + less time for bust risk). Already-up = ×1.0, ~−5% per year out, floor ×0.82.",
                    "Multi-position eligibility premium: 2-position ×1.03, 3+ ×1.06 (DH/util don't count). Lineup flexibility covers injuries and unlocks roster construction.",
                    "Young-ascending bonus: the age curve assumes a fixed peak from current production, but a ≤23yo already posting elite Statcast is likely still climbing — up to +8% for a 20yo with +1.5z skill.",
                    "All five new levers show in the click-to-expand breakdown's 'why they're here' list.",
                ],
            },
            {
                "version": "Dynasty v1.1 — 2026-05-25",
                "title": "Dynasty: skill model + minor-league prospect stats",
                "changes": [
                    "Our data now DRIVES the board, not just nudges it. base_value = 50% FantraxHQ consensus + 50% our Statcast skill-rank (xwOBA/xSLG/barrel%/hard-hit% for hitters; xERA/xwOBA-against/barrels-allowed for pitchers), z-scored and pooled. 100% consensus fallback when no qualifying sample.",
                    "Minor-league prospects get a real read: we resolve their MLB id, detect their ACTUAL current level (robust to promotions — a 'AA' prospect now in the majors is valued on his MLB line), and convert MiLB production to MLB-equivalent (level haircut + age-vs-level bonus, the dominant prospect signal).",
                    "Luck-logic audit: sample-gated (120 PA / 80 BF), magnitude halved to ±5%, role-aware (two-way bat no longer clobbered by a tiny IP line), and elite underlying skill no longer mislabeled 'sell-high'.",
                    "Click any ranking row → full breakdown: consensus×skill blend math, the Statcast/MiLB skill table with league context + z-scores, the 6-year projected value path, and a plain-English why.",
                ],
            },
            {
                "version": "Dynasty v1.0 — 2026-05-25",
                "title": "Dynasty rankings, multi-year value & trade analyzer",
                "changes": [
                    "New 👑 Dynasty tab: our rankings re-shape the FantraxHQ consensus with explicit age curves (6-year discounted value path), position scarcity (C/SS/2B premium, 1B/DH/RP discount, two-way premium), and a Statcast luck tilt. Shows our-rank vs consensus-rank disagreement (Δ).",
                    "Trade analyzer: per-side dynasty value, fairness verdict, consolidation premium (best player in a capped-roster deal > raw sum), and win-now-vs-rebuild context from the age profiles.",
                    "Endpoints: GET /api/dynasty/rankings, GET /api/dynasty/player/{name}, POST /api/dynasty/trade.",
                ],
            },
            {
                "version": "v9.17 — 2026-05-25",
                "title": "Fix order_factor double-count",
                "changes": [
                    "Batting-order PA factor was an absolute per-spot multiplier (leadoff ×1.10), but base_pg is points-per-GAME which already embeds a hitter's typical PA volume — so a career leadoff hitter got an unearned +10%. Corrected: normalize today's expected PA (by lineup spot) against the player's own season PA/game, clamped ±12%. A leadoff regular now reads ~1.0; a usual-#7 hitter slotted leadoff gets the real boost.",
                ],
            },
            {
                "version": "v9.12 — 2026-05-19",
                "title": "Tighten pitcher COLD + add ELITE form_tag boost",
                "changes": [
                    "Pitcher COLD post-matchup ×0.80 → ×0.70. 9-day audit (n=48, through 5/18) showed bias still -4.81 after v9.10's tightening — projecting 5.95, scoring 1.14. Cold pitchers' xERA/QoC anchors keep inflating them toward season skill they're not showing. ×0.70 closes more of the residual without overcorrecting on pitcher single-start variance",
                    "ELITE form_tag boost added: hitter ×1.10, pitcher ×1.07. These are the 'always-on' players (consistent across L3/L7/L14 AND L14 ≥ 9 pts/G — Judge/Acuña/Ohtani-class). 9-day audit (n=23) showed +4.87 bias: they score 17.2 vs proj 12.4. The chain pulls toward season mean but their ceiling is sticky-high. Modest boost since sample is small",
                    "Tooltip's post-matchup row now labels ELITE form correctly",
                    "Skipped re-tuning ELITE/POOR QoC weights — 5/18's bad ELITE-QoC reading (-3.30) was on a Vegas-dead day so it's not a clean signal; full-sample direction (-0.92→-1.09) is small and within noise",
                ],
            },
            {
                "version": "v9.11 — 2026-05-18",
                "title": "rolling_factor: replace broken Savant range with K-rate shift",
                "changes": [
                    "DETECTED BUG: rolling_factor was a silent no-op for every player since the day it shipped. We pulled Savant's /expected_statistics?start_date=...&end_date=... but Savant ignores those date params and returns season-wide xwOBA for every window. Probed 11 alternate param names; none filter. MLB Stats API's expectedStatistics group has the same bug",
                    "REPLACEMENT: rolling K-rate shift from MLB Stats API byDateRange (which DOES honor dates for standard stats). K% is luck-stripped (process not outcome), stabilizes in ~60 PAs, doesn't overlap with pts/G HOT/COLD the way OPS would. Capped ±8%",
                    "DETECTED BUG: ODDS_API_KEY returned 401 Unauthorized for hours of projections silently going Vegas-less (0 team_totals, 0 K-prop lines for the entire day). Used to be caught silently — now logs loudly and tracks _LAST_ERROR per endpoint",
                    "New /api/diag/odds endpoint surfaces the key state so a dead key can't burn another day undetected. Caught the v9.11 key rotation in <1min via this",
                ],
            },
            {
                "version": "v9.10 — 2026-05-18",
                "title": "Full-sample re-tune (n=2389, 8 days)",
                "changes": [
                    "Pitcher COLD post-matchup ×0.80. Mirror of the v9.7 hitter shrink. 8-day audit (n=43 cold pitchers) showed a -5.9 bias — projecting ~17, scoring ~11 (6.2σ from zero). Multiplicative shrink after the factor chain closes about half the gap without overshooting",
                    "Pitcher HOT post-matchup ×1.05 added for symmetry (lighter than hitter ×1.07 because pitcher form swings are noisier per-start)",
                    "ELITE/POOR STATCAST_WEIGHT 0.25 → 0.20. 8-day audit showed ELITE-QoC still over-projected -0.92 (n=463) and POOR-QoC under-projected +0.93 (n=383); symmetric residuals signal the Statcast prior is pulling too hard on both extremes. Lower weight lets rolling carry more residual half",
                    "Pre-v9.10 full-sample headline: overall bias +0.04, MAE 4.29 on n=2389 — already well-centered. This tune addresses the two residual buckets that haven't moved in three sample windows",
                    "MODEL_REV → 2026-05-18-v9.10; cache invalidated, projections regenerate",
                ],
            },
            {
                "version": "v9.8 — 2026-05-17",
                "title": "Catcher framing on pitcher projection",
                "changes": [
                    "Pitcher projections now adjust for the starting catcher's framing skill (Savant rv_tot from /leaderboard/catcher-framing). Elite framers (Realmuto, Kelly, Heim) generate ~0.3-0.5 extra K per start; anti-framers cost the same. Multiplier: ×(1 + rv_tot × 0.005) capped at ±3%",
                    "Catcher detection: when lineups are posted, finds the player whose primaryPosition == 'C' for each team. Pre-lineup: neutral (no signal yet)",
                    "Components dict now exposes framing_factor + catcher_framing_rv so the breakdown tooltip shows the math",
                    "Estimated MAE drop: ~0.2-0.3 pts on pitcher projections; bridges most of the 6.90 → ~6.6 gap toward the structural floor",
                ],
            },
            {
                "version": "v9.7 — 2026-05-17",
                "title": "14-day calibration re-tune",
                "changes": [
                    "COLD post-matchup multiplier ×0.85 → ×0.80. 14-day audit (n=1195) showed COLD hitters were still being over-projected by -0.67 pts (5σ). Tighter shrink closes about half the remaining residual",
                    "ELITE/POOR STATCAST_WEIGHT 0.30 → 0.25. ELITE-QoC was over-projected by -1.02 pts (n=804, 6σ); the Statcast prior was pulling these hitters TOWARD true talent too aggressively. Lower weight lets the rolling base carry more signal",
                    "Overall pre-tune calibration was already strong (bias +0.00, MAE 4.36 on n=4130) — these tweaks address the two residual signals that survived the n=18-day audit at v9.0",
                    "MODEL_REV bumped to 2026-05-17-v9.7 — cache invalidated, projections regenerate",
                ],
            },
            {
                "version": "v9.5 — 2026-05-14",
                "title": "Vegas K-props in pitcher projection",
                "changes": [
                    "Wired the actual betting market's pitcher_strikeouts lines (from the-odds-api.com, the same source the K Props tab uses) into project_pitcher as a damped delta. K is the biggest single fantasy event for a pitcher (1.5 pts/K × 7-10 Ks/start), so the sharpest available K-rate signal is the multi-book Vegas line",
                    "Math: delta_K = vegas_line − rolling-implied K (k9 × ip_per_start/9). Convert to pts at 1.5/K, damp to 0.5 weight, cap ±3 pts. Doesn't replace the projection — augments it",
                    "Skipped: our internal K Prop Tester score (user flagged as garbage). Only the live Vegas-market lines flow into the algo",
                    "Tooltip shows the Vegas line and the adjustment so the source is auditable",
                    "MODEL_REV bumped to 2026-05-14-v9.5 so cached pitcher projections regenerate with the K-prop signal",
                ],
            },
            {
                "version": "v9.4.1 — 2026-05-13",
                "title": "Late-day polish",
                "changes": [
                    "Most-Picked-Player record now splits two-way Ohtani by role — his 113 hitter picks + 11 pitcher picks were merging into one 124-count entry, drowning out the actual all-time leader Jose Ramirez (118 as hitter). Now shows 'Shohei Ohtani (hitter)' / '(pitcher)' separately",
                    "Position override: Brent Rooker → OF (joins Ohtani + Schwarber) so he's draftable to OF slots instead of DH-locked",
                    "Lone-SP-needer's SP picks never burn the snake turn — Stock can take both his SPs back-to-back AND keep his natural hitter/UTIL turns. Previously OOO only fired for off-turn picks; now also fires when it IS your natural turn but you're the only one with open SP slots",
                    "Dynasty matcher: stricter fallback. Was using last-name-only which collided ('Endy Rodríguez' inherited Julio Rodriguez's #9 rank). Now uses (first-letter + last-name) — keeps 'B Witt' → 'Bobby Witt Jr.' working without false positives like Endy/Julio",
                    "Dynasty data: imported the full FantraxHQ Top-500 CSV (was a hand-curated 70 names)",
                    "Restricted-undo on draft: only the drafter of the last pick can undo it (their button label flips to '↩️ Undo my pick (X)'; others see 'locked'). Prevents accidental undo of someone else's pick in a shared session",
                ],
            },
            {
                "version": "v9.4 — 2026-05-13",
                "title": "In-match live projections + UX polish",
                "changes": [
                    "Live (in-match) projections on the Live Score tab — new 'Live' column shows actual + remaining estimate. Hitters use completed-PA share of the 4.3 PA/game expectation; pitchers use ip_per_start * 3 outs as the expected denominator. Standings line shows 'Stock 47.0 → live 92.4' so you see projected final, not just where everyone is right now",
                    "Stats tab: every player-table column header is click-to-sort with ▲/▼ indicators. Click 'Stock Avg' to see who Stock crushes vs the field, click 'Total' to flip to volume, etc.",
                    "Ask Algo tab — paste any roster, get back ranked projections with form/tier/IP/K9 context + START/CONSIDER/SIT badges. Uses the same cached projection slate as /api/projections so first-call is fast",
                    "Hall of Fame tab — all-time records, season champions, head-to-head matrix, biggest blowouts, worst single picks, most-picked players. Merges live scored drafts with imported historic seasons (2023/2024/2025/2026)",
                    "Daily MLB trivia (Draft tab) — 3 auto-generated questions per day from the slate + Statcast, gated behind 'Who are you?' picker so no spoilers, hardened distractors so all 4 options are legit leaders, season leaderboard",
                    "Multi-season historic import: 2023 (112 days) + 2024 (66) + 2025 (102) + 2026 (live) — 302 days, 9084 picks across the record book. New all-time best hitter game: Kyle Schwarber 66.0 (Stock, 2025-08-28)",
                    "Restricted-undo on draft: button now disables when the last pick wasn't yours, with 'X's pick — locked' label. Prevents accidental undo of someone else's pick in a shared session",
                    "Position override: Kyle Schwarber → OF (joined Ohtani) so he's draftable to OF slots instead of being DH-locked",
                    "League baselines footer on every tab — surfaces the live Statcast averages (brl%/hh%/xERA/xwOBA) with their age. 'refresh' link forces a re-pull on demand",
                    "Disk cache LRU eviction: bounded at 400MB so /data volume can't fill up again (the 5/13 trivia outage was caused by 958MB of unbounded cache growth)",
                    "Dynasty rankings: imported the real FantraxHQ Top-500 CSV (replaces the hand-curated 70-name list); matching is now accent / suffix / middle-initial tolerant with last-name fallback so far more players resolve to a rank",
                ],
            },
            {
                "version": "v9.3 — 2026-05-12",
                "title": "Advanced projection factors",
                "changes": [
                    "K/9 added to opposing-SP factor for hitters — strikeout pitchers now suppress hitter projections more accurately (K is the biggest single fantasy event for hitters)",
                    "TTO penalty for starters — pitchers averaging 5.5+ IP/start get -2.5% for the 3rd time through the order; 4.5–5.5 get -1% (well-documented ~30-pt wOBA jump)",
                    "Team defense factor (±3%) — pitcher projections adjust for team fielding quality (DRS/OAA proxy via team fielding%)",
                    "Opener detection — pitchers averaging <2.5 IP/start are flagged as openers and clamped to a 9-pt ceiling",
                    "ISO form factor (±4%) — recent SLG-AVG vs season ISO catches HR-streak variance that smoothed pts/G under-weights",
                    "SB modeling v1 — established SB threats (>10/100G pace) get up to +4% vs poor-pickoff pitchers",
                    "Changelog button + /api/changelog endpoint",
                ],
            },
            {
                "version": "v9.2 — 2026-05-12",
                "title": "Dynamic league baselines",
                "changes": [
                    "League averages (barrel%, hard-hit%, xERA, etc.) now pulled live from Statcast leaderboards with 24h disk cache instead of hardcoded constants — no more annual drift, no cron needed",
                    "Tooltip now reads live league averages (was showing stale `lg 6.5` / `lg 38`)",
                ],
            },
            {
                "version": "v9.1 — 2026-05-11/12",
                "title": "Operational hardening + Bayesian post-matchup",
                "changes": [
                    "HOT post-matchup ×1.07, COLD ×0.85 — closes Bayesian-audit residuals (HOT under +1.11±0.35; COLD over -1.08±0.10)",
                    "Cache-stampede protection via per-key threading.Lock — prevents concurrent OOM kills",
                    "Fly RAM 512MB→1GB (was OOM-killing on cold-cache concurrent projection computes)",
                    "Startup prewarm of today's projections + LRU eviction for in-memory cache",
                    "IL/callup manual pool-adds (Mookie Betts activation handling)",
                ],
            },
            {
                "version": "v8 — 2026-05-10",
                "title": "Date-leak fix + 5 audit-driven fixes",
                "changes": [
                    "FIXED mlb_api.player_stats date-leak — `as_of` parameter prevents past-date queries from using today's stats (was corrupting all backward calibration data)",
                    "MODEL_REV cache stamp — stale cached projections from prior model versions are automatically rejected",
                    "Fixed circular feedback in lineup_factor (was averaging hitter projected_points which already include vegas_factor → feedback loop). Now uses base_pg",
                    "Tightened lineup_factor damping (^0.30→^0.18, clamp 0.94–1.07) to avoid Vegas overlap",
                    "Opposing-lineup-quality factor for pitcher projection (today's posted lineup avg pts/G vs league avg)",
                    "HP umpire k-factor wired into pitcher projection (UmpScorecards favor → K-rate boost)",
                    "Weather temp wired into park HR factor; status='out' zeroes projection (×0.05)",
                    "Streak override iterations 0.70 → 0.80 → 0.85 + adaptive per-tier Statcast weight (HOT/COLD=0.15, ELITE/POOR=0.30, else=0.40)",
                    "Mobile tooltip: bottom-sheet on small screens instead of inline overlay",
                ],
            },
            {
                "version": "v7 — 2026-05-07/08",
                "title": "Draft UX overhaul + dynasty list",
                "changes": [
                    "Edit/Save pattern for game pool to prevent accidental in-draft changes",
                    "Draft OOO: 'SP anytime' indicator for held drafter; snake skips lone-SP-needer for non-SP picks",
                    "Curated 2026 Dynasty Top list (~57 names) + Dyn column in pool, sortable",
                    "Full 'all drafted players' projection table when complete",
                    "Tags ❓ button + inline guide for HOT/COLD/STEADY/ELITE + Statcast tiers",
                    "COLD post-matchup shrink ×0.85 (later refined by v9 Bayesian audit)",
                    "Direct-swap bench promotion (no chain reshuffling) for predictable behavior",
                    "Fixed flashing/double-fetch on date change; auto-load next draft",
                    "2-decimal Proj/Actual so per-pick column sums correctly to team total",
                ],
            },
            {
                "version": "v6 — 2026-05-05/06",
                "title": "Fantrax integration + Cat-aware lineup tool",
                "changes": [
                    "Fantrax cookie-paste auth + 1-click 'Pull from Fantrax' to autofill roster",
                    "Slot-aware lineup optimizer (C/1B/2B/SS/3B/MI/CI/3xOF/2xUT/4xSP/3xRP/2xP)",
                    "Leverage-aware Cat z-score ranking — multiplies each cat's z-contribution by weekly leverage (close cats 1.5×, decided 0.5×)",
                    "Real reliever projections (K/9 + ERA + WHIP + SV+H rates × daily usage prob)",
                    "Force-bench / Force-minors / Allow-call-ups overrides",
                    "Action labels (KEEP / PROMOTE↑ / BENCH↓ / MOVE / STAY BN) based on current Fantrax slot vs recommendation",
                    "Out-of-order SP pick doesn't advance the snake — others can finish hitters",
                    "Optimal hitter assignment (Hungarian-style) replacing two-phase greedy",
                ],
            },
            {
                "version": "v5 — 2026-05-04",
                "title": "Statcast prior + matchup-aware projections",
                "changes": [
                    "Statcast-implied true-talent baseline (barrel%/HH%/sweet-spot%) blended with rolling base — anchors backups and protects against streaks",
                    "Adaptive Statcast blend weight per form tag (HOT/COLD lower, steady higher)",
                    "Streak-trust override — HOT/COLD lean 0.70 (later 0.85) on L3 directly",
                    "Vegas implied team totals, opposing bullpen ERA, hitter platoon, pitcher throws-handedness, rolling 14-day xwOBA from Savant",
                    "Batting-order PA adjustment (leadoff ×1.10 → #9 ×0.88)",
                    "Park factor with per-handedness HR bias (NYY +18% LHB, etc.)",
                    "Drop SP/opp factor entirely when Vegas implied total available — Vegas supersedes",
                    "Tooltip: structured multi-row breakdown for every factor; ±1σ floor/ceiling",
                    "Accuracy tab — daily projection vs actual, bias/MAE by role/form/Statcast tier",
                    "Mobile: tap-to-toggle tooltips, bottom-sheet positioning, compact tables",
                ],
            },
            {
                "version": "v4 — 2026-05-03",
                "title": "Form tags + weighted rolling windows",
                "changes": [
                    "Rolling L3/L7/L14 windows + HOT/COLD/STEADY/ELITE form tags",
                    "Weighted non-overlapping bucket projection (sample-size × recency)",
                    "Season-as-prior bucket so backups w/ 1 hot game don't blow up",
                    "Hover tooltip with Statcast tier + pitfalls per player",
                    "Statcast QoC multiplier + surface barrel/xera in pool",
                    "Live Gameday-style slate cards (score, inning, count, runners diamond)",
                    "Replace modal filters by position eligibility regardless of whose turn",
                    "Hover projection breakdown in draft state + draft rosters",
                    "Cache projections 6h on disk; full-slate cache shared across team filters",
                ],
            },
            {
                "version": "v3 — 2026-05-01",
                "title": "K Props + Vegas + Weather + Umpires",
                "changes": [
                    "New K Props tab — auto-fetch odds from the-odds-api.com, book comparison, EV calculator",
                    "Vegas team totals integrated (5/5 models)",
                    "NWS weather forecast + HR factor (wind + temp) per park",
                    "HP umpire HP-favor + season k-factor from UmpScorecards (3/5)",
                    "Savant Statcast: xwOBA/xERA, barrel/hardhit/sweetspot, opp-allowed metrics",
                    "Insights tab merging totals + weather + ump per game",
                    "Disk cache (24h) for umpscorecards + savant CSVs",
                    "Vertical draft strip with slate start time + on-clock drafter",
                ],
            },
            {
                "version": "v2 — 2026-04-30",
                "title": "Multi-user draft + scoring + season history",
                "changes": [
                    "Multi-user identity + live polling (everyone sees the draft state)",
                    "Doubleheader-aware scoring + G1/G2 chooser",
                    "OOL bench promotion + Replace search + 'best of' bench swap",
                    "Move button — slide bench/UTIL into a starter slot, auto-swap",
                    "Pre-game bench fix, slot-sorted live score, position-aware bench swap",
                    "Stats tab + full season history import + Ohtani (P/OF) two-way handling",
                    "Schedule tab + Sunday-chip picker for picking slate dates",
                    "Roster cells: name+proj line 1, badge+Replace line 2",
                ],
            },
            {
                "version": "v1 — 2026-04-30",
                "title": "Initial release",
                "changes": [
                    "MLB DFS snake-draft app wired to live MLB Stats API",
                    "Per-day game picker + draft board pick log",
                    "'All available players' view with cache-bust on deploy",
                    "Fixed 10-slot roster grid (C/1B/2B/SS/3B/CI/MI/3xOF/2xUT/4xSP/3xRP/2xP)",
                    "Drafter's choice of slot order (not enforced)",
                    "Hitter-only bench, replaces a starter only if it outscores them",
                    "Deployed to Fly.io (mlb-dfs-doron)",
                ],
            },
        ],
    }


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


@app.get("/api/diag/odds")
def diag_odds(date: str | None = None):
    """Surface odds-api state — was the key working? How many lines today?
    Added 2026-05-18 after the API key returned 401 for an entire day with
    every Vegas factor silently degrading to ×1.00. Hit this when you see
    'no Vegas line' on the tooltip for many players."""
    from . import odds_api
    d = date or Date.today().isoformat()
    state = {
        "configured": odds_api.is_configured(),
        "date": d,
        "last_errors": odds_api.last_errors(),
    }
    # Live re-fetch team_totals so the user sees the actual current state, not
    # cached. This burns 1 odds-api credit per hit but is the whole point of
    # the diag endpoint.
    try:
        tt = odds_api.get_team_totals(d) or {}
        state["team_totals_count"] = len(tt)
        state["team_totals_sample"] = dict(list(tt.items())[:3])
    except Exception as e:
        state["team_totals_error"] = str(e)
    # Saved k-prop file on disk
    saved = odds_api.saved_odds(d)
    state["saved_kprops_count"] = len(saved.get("pitchers", {})) if saved else 0
    state["saved_kprops_fetched_at"] = saved.get("fetched_at") if saved else None
    return state


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
    try:
        fantrax.save_cookie(req.cookie)
    except ValueError as e:
        # Missing session token / empty dump — surface the actionable message
        # to the UI instead of a 500.
        raise HTTPException(400, str(e))
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
    # Block the replacement if every game the candidate's team plays today
    # has already started or finished — once first pitch happens you can't
    # add a new player to your roster for that day.
    if proj.team_id:
        team_pks = _team_to_slate_gamepks(dr).get(proj.team_id, [])
        states = _game_state_map(dr)
        if team_pks and not any(states.get(pk, "pre") == "pre" for pk in team_pks):
            game_states_summary = ", ".join(f"{pk}={states.get(pk,'?')}" for pk in team_pks)
            raise HTTPException(
                400,
                f"{proj.name}'s game(s) for today have already started or finished "
                f"({game_states_summary}). Pick a player whose game hasn't started yet."
            )
    try:
        dr.replace_pick(pick_number, proj, game_pk=game_pk)
    except ValueError as e:
        raise HTTPException(400, str(e))
    draft_mod.save_draft(dr)
    return _draft_state(dr)


class UpdateDraftersRequest(BaseModel):
    drafters: list[str]


@app.post("/api/drafts/{draft_id}/drafters")
def update_drafters(draft_id: str, req: UpdateDraftersRequest):
    """Reorder the drafters of an existing draft (changes the snake-order
    round 1 starting position). Refuses if picks already exist — reordering
    after picks have been made would corrupt the snake math."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    if dr.picks:
        raise HTTPException(
            400,
            f"draft already has {len(dr.picks)} pick(s); reorder before any picks are made"
        )
    if sorted(req.drafters) != sorted(dr.drafters):
        raise HTTPException(
            400,
            f"new drafters {req.drafters} must be the same set as existing {dr.drafters}"
        )
    if len(req.drafters) < 2:
        raise HTTPException(400, "need at least 2 drafters")
    dr.drafters = list(req.drafters)
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
    locked_days: str | None = None,
    skipped_days: str | None = None,
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

    # Locked days: {date: set(game_pks)} — used as-is. The greedy filler
    # honors them, and they still contribute to the team-count progression
    # so downstream days re-balance around the user's swap.
    locked_map: dict[str, set[int]] = {}
    if locked_days:
        try:
            import json as _json
            for entry in _json.loads(locked_days):
                if isinstance(entry, dict) and entry.get("date"):
                    locked_map[entry["date"]] = set(int(p) for p in (entry.get("game_pks") or []))
        except Exception as e:
            raise HTTPException(400, f"locked_days must be JSON [{{date,game_pks}}]: {e}")

    # Skipped days: dates the user explicitly removed from this week's
    # schedule. We still emit them in the response (so the frontend can show
    # a 'restore' affordance) but with no games selected and no contribution
    # to team counts — the remaining days rebalance to absorb the slack.
    skipped_set: set[str] = set()
    if skipped_days:
        try:
            import json as _json
            parsed = _json.loads(skipped_days)
            if isinstance(parsed, list):
                skipped_set = {str(d) for d in parsed if d}
        except Exception as e:
            raise HTTPException(400, f"skipped_days must be JSON array of ISO dates: {e}")

    # Auto-lock past days: any date BEFORE today that already has a saved
    # draft is hard-locked to that draft's game_pks. Those games are already
    # finished — the user shouldn't be able to retroactively edit them, and
    # the team-count balancer must include their teams in the totals so the
    # remaining days balance around them. Overrides any user-supplied lock
    # for the same date (a stale frontend can't accidentally rewrite history).
    auto_locked_past: set[str] = set()
    today = Date.today()
    for did in draft_mod.list_drafts():
        try:
            ddate = Date.fromisoformat(did)
        except Exception:
            continue
        if ddate >= today or ddate < s or ddate > e:
            continue
        try:
            past_dr = draft_mod.load_draft(did)
        except Exception:
            continue
        if past_dr.game_pks:
            locked_map[did] = set(past_dr.game_pks)
            auto_locked_past.add(did)

    counts: Counter[str] = Counter()
    if seed_from_existing:
        # Seed from CURRENT-SEASON data. Two sources, deduped by date:
        #   1. historic.team_counts() — refreshed from the 2026 spreadsheet
        #      'Team How Often' sheet; per-team total across early-season
        #      days that were tracked in the sheet before the live system
        #      took over. canonical_team() folds aliases (OAK→ATH, etc).
        #   2. Live saved drafts on the volume — for dates AFTER the
        #      spreadsheet cutoff. Filtered to current season and only
        #      dates not already covered by historic.standings() so a
        #      day isn't double-counted.
        today = Date.today()
        current_year = s.year
        for team, n in historic.team_counts().items():
            counts[team] += int(n)
        historic_dates = {e.get("date") for e in historic.standings()
                          if (e.get("date") or "").startswith(f"{current_year}-")}
        for did in draft_mod.list_drafts():
            try:
                dr = draft_mod.load_draft(did)
            except Exception:
                continue
            try:
                ddate = Date.fromisoformat(dr.date)
            except Exception:
                continue
            if ddate >= s or ddate >= today or ddate.year != current_year:
                continue
            if dr.date in historic_dates:
                continue  # already counted via historic.team_counts() above
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
                if aa: counts[historic.canonical_team(aa)] += 1
                if ha: counts[historic.canonical_team(ha)] += 1

    # The friend league plays Sun-Thu only — skip Friday (weekday 4) and
    # Saturday (weekday 5) when proposing slates.
    SKIP_WEEKDAYS = {4, 5}

    def _is_day_game(g):
        # MLB schedule returns gameDate as ISO UTC. We need to convert to ET
        # before checking the hour — a 9:40 PM ET game starts at 01:40 UTC the
        # NEXT day, so 'hour < 22 UTC' would falsely flag it as a day game
        # (UTC hour 01 < 22 == True). Use zoneinfo for DST-correct conversion.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        iso = g.get("gameDate") or ""
        if not iso:
            return False
        try:
            dt_utc = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
            return dt_et.hour < 17   # before 5pm ET = day game
        except Exception:
            return False

    # ---- two-pass build (v9.6) ----
    # Pass A: walk every Sun-Thu day in the range and lock in either the
    #   user-pinned games (locked_days) OR the day-game-only subset (up to
    #   slate_size). Team counts update after each day so subsequent days'
    #   day-game picks already see the budget pressure.
    # Pass B: walk again and fill any day that didn't reach slate_size with
    #   that day's night games, sorted by team count using the post-Pass-A
    #   totals — so days short on day games (typically Mon/Tue) naturally
    #   absorb the rebalancing for whatever Wed/Thu locked in via day games.
    days_meta: list[dict] = []
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
        days_meta.append({"date": cur, "games": games})
        cur += timedelta(days=1)

    def _matchup_key(g) -> frozenset:
        """Order-insensitive matchup key — TOR@NYY and NYY@TOR collide so a
        3-game series at one stadium plus a swap-stadium game later in the
        week still register as 'the same matchup'. Used to penalize
        repeated night-game matchups (per user request: day games on
        Wed/Thu rarely overlap, so we don't penalize them)."""
        away = historic.canonical_team(g["away"]["abbr"] or "")
        home = historic.canonical_team(g["home"]["abbr"] or "")
        return frozenset((away, home))

    # Track every matchup picked across the week so Pass B can deprioritize
    # repeats. Includes Pass A's day-game picks because if a series happens
    # to play a day game on Wed AND a night game on Tue/Mon, that's still a
    # repeat we want to spread out. Counter (not set) so a thrice-repeated
    # matchup sorts after a twice-repeated one — first-times come first,
    # then least-repeated, etc.
    picked_matchups: Counter[frozenset] = Counter()

    # Pass A — fill day games + locks
    pass_a_chosen: dict[str, list[dict]] = {}
    for meta in days_meta:
        cur_iso = meta["date"].isoformat()
        games = meta["games"]
        valid = [g for g in games if g["away"]["abbr"] and g["home"]["abbr"]]
        if cur_iso in skipped_set:
            # Day is intentionally removed from the week — no games picked,
            # no team-count contribution. Pass B also short-circuits below.
            pass_a_chosen[cur_iso] = []
            continue
        locked_pks_for_day = locked_map.get(cur_iso)
        if locked_pks_for_day:
            chosen = [g for g in valid if g.get("gamePk") in locked_pks_for_day]
            # If they locked fewer than slate_size, leave the remainder for
            # Pass B to fill — keeps cascading rebalance intact.
        else:
            day_games = sorted(
                [g for g in valid if _is_day_game(g)],
                key=lambda g: (
                    counts[historic.canonical_team(g["away"]["abbr"] or "")]
                    + counts[historic.canonical_team(g["home"]["abbr"] or "")],
                    hash((g.get("gamePk", 0), cur_iso)) & 0xFFFF,
                ),
            )
            chosen = day_games[:slate_size]
        for g in chosen:
            counts[historic.canonical_team(g["away"]["abbr"])] += 1
            counts[historic.canonical_team(g["home"]["abbr"])] += 1
            picked_matchups[_matchup_key(g)] += 1
        pass_a_chosen[cur_iso] = chosen

    # Pass B — fill remainder with night games. Sort key:
    #   1. day-game preference (matinees fill before night when both possible)
    #   2. matchup-uniqueness penalty: a matchup already picked elsewhere in
    #      the week sorts AFTER first-time matchups, so 3-night series get
    #      thinned out
    #   3. team-count balance
    #   4. hash tiebreak
    days = []
    for meta in days_meta:
        cur_iso = meta["date"].isoformat()
        games = meta["games"]
        valid = [g for g in games if g["away"]["abbr"] and g["home"]["abbr"]]
        chosen = list(pass_a_chosen.get(cur_iso, []))
        chosen_pks = {g.get("gamePk") for g in chosen}
        is_skipped = cur_iso in skipped_set
        if is_skipped:
            chosen = []  # explicit: no fill for skipped days
        elif len(chosen) < slate_size:
            night_pool = [g for g in valid
                          if g.get("gamePk") not in chosen_pks
                          and not _is_day_game(g)]
            # If still under cap and the locked-day set was partial, also
            # consider unpicked day games as filler (rare; e.g. user pinned
            # 2 specific games on a day that has 8 day games available).
            day_pool = [g for g in valid
                        if g.get("gamePk") not in chosen_pks
                        and _is_day_game(g)]
            filler_sorted = sorted(
                night_pool + day_pool,
                key=lambda g: (
                    # Day-game preference still applies to filler ordering
                    0 if _is_day_game(g) else 1,
                    # Repeat-matchup count (only applies to night games per
                    # user observation that day games rarely overlap series).
                    # 0 = first time this week, 1 = first repeat, 2 = second
                    # repeat, etc. Lower sorts first, so the algorithm
                    # exhausts unique matchups before tapping the same series
                    # twice and the same series 3x last.
                    picked_matchups.get(_matchup_key(g), 0) if not _is_day_game(g) else 0,
                    counts[historic.canonical_team(g["away"]["abbr"] or "")]
                    + counts[historic.canonical_team(g["home"]["abbr"] or "")],
                    hash((g.get("gamePk", 0), cur_iso)) & 0xFFFF,
                ),
            )
            needed = slate_size - len(chosen)
            extras = filler_sorted[:needed]
            for g in extras:
                counts[historic.canonical_team(g["away"]["abbr"])] += 1
                counts[historic.canonical_team(g["home"]["abbr"])] += 1
                picked_matchups[_matchup_key(g)] += 1
            chosen = chosen + extras
        days.append({
            "date": cur_iso,
            "selected_games": [
                {
                    "gamePk": g["gamePk"],
                    "away_abbr": g["away"]["abbr"],
                    "home_abbr": g["home"]["abbr"],
                    "away_sp": (g["away"]["probablePitcher"] or {}).get("name", "TBD"),
                    "home_sp": (g["home"]["probablePitcher"] or {}).get("name", "TBD"),
                    "status": g.get("detailedStatus", ""),
                    "gameDate": g.get("gameDate"),
                }
                for g in chosen
            ],
            "all_games": [
                {
                    "gamePk": g["gamePk"],
                    "away_abbr": g["away"]["abbr"],
                    "home_abbr": g["home"]["abbr"],
                    "away_sp": (g["away"]["probablePitcher"] or {}).get("name", "TBD"),
                    "home_sp": (g["home"]["probablePitcher"] or {}).get("name", "TBD"),
                    "status": g.get("detailedStatus", ""),
                    "gameDate": g.get("gameDate"),
                }
                for g in valid
            ],
            "locked": cur_iso in locked_map,
            "past": cur_iso in auto_locked_past,
            "skipped": is_skipped,
            "team_counts_after": dict(counts),
        })

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
    # If a draft already exists for one of these dates AND has picks, skip
    # it (and surface the conflict) unless force_overwrite is True. Without
    # this guard the unconditional save_draft used to silently wipe a real
    # draft-in-progress when the user re-ran the schedule builder.
    force_overwrite: bool = False


@app.post("/api/schedule_builder/apply")
def apply_schedule(req: ApplyScheduleRequest):
    """Bulk-create one draft per day with the chosen slate. Drafter order is
    randomized per day if requested (each draft gets its own snake order).

    Conflict policy:
      - date has NO existing draft → create
      - date has existing draft with 0 picks → overwrite (slate/drafters
        update is the whole point of running the builder again)
      - date has existing draft with picks AND force_overwrite=False → skip
        with reason 'already has N picks' so the UI can prompt the user
      - force_overwrite=True → always overwrite (UI explicitly confirmed)
    """
    if len(req.drafters) < 2:
        raise HTTPException(400, "need at least 2 drafters")
    existing_ids = set(draft_mod.list_drafts())
    created, skipped, overwritten = [], [], []
    for entry in req.days:
        try:
            d = Date.fromisoformat(entry["date"])
        except Exception:
            skipped.append({"date": entry.get("date"), "reason": "bad date"})
            continue
        date_iso = d.isoformat()
        had_picks = 0
        if date_iso in existing_ids:
            try:
                existing = draft_mod.load_draft(date_iso)
                had_picks = len(existing.picks)
            except Exception:
                had_picks = 0
            if had_picks > 0 and not req.force_overwrite:
                skipped.append({
                    "date": date_iso,
                    "reason": f"already has {had_picks} picks — pass force_overwrite=true to replace",
                    "had_picks": had_picks,
                })
                continue
        order = list(req.drafters)
        if req.randomize_order:
            random.shuffle(order)
        try:
            dr = draft_mod.new_draft(d, order, game_pks=entry.get("game_pks") or [])
            draft_mod.save_draft(dr)
            if had_picks > 0:
                overwritten.append({"date": date_iso, "drafters": order, "lost_picks": had_picks})
            else:
                created.append({"date": date_iso, "drafters": order})
        except Exception as ex:
            skipped.append({"date": date_iso, "reason": str(ex)})
    return {"created": created, "overwritten": overwritten, "skipped": skipped}


@app.get("/api/stats/standings")
def stats_standings(season: int | None = None):
    """All-time standings + per-day breakdown.

    Combines two sources:
      1. Historic standings (data/historic/standings.json) — imported once
         from the spreadsheet; covers the season prior to the live system.
      2. Current saved drafts on the volume — scored live so today's totals
         update as games progress.

    Per-day rows are unioned by date; a date present in both prefers the
    live computation (fresher).
    """
    # Default to current season — the Stats tab is for tracking the live season,
    # not for cross-season comparisons (that's what the 🏆 Hall of Fame tab is for).
    # Pass ?season=YYYY to scope to a specific year, or ?season=0 for all-time.
    current_year = Date.today().year
    if season is None:
        season = current_year

    drafts_data = []
    seen_dates: set[str] = set()
    for did in draft_mod.list_drafts():
        try:
            dr = draft_mod.load_draft(did)
        except Exception:
            continue
        if not dr.picks:
            continue
        # Filter by season unless season=0 (show all)
        if season and not dr.date.startswith(f"{season}-"):
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
        if season and entry.get("season") != season:
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
def stats_players(top_n: int = 50, season: int | None = None):
    """Player aggregate stats across all saved drafts AND historic picks:
    pick counts per drafter, average points per pick (overall + per drafter).

    Keyed by player name (historic data has no MLB player_id), so a player
    drafted both in the historic CSV and in a current saved draft will be
    correctly aggregated as long as the names match.
    """
    # Default to current season so the Stats tab tracks the live year.
    # Pass ?season=0 to span all-time (matches /api/stats/standings behavior).
    current_year = Date.today().year
    if season is None:
        season = current_year

    by_name: dict[str, dict] = {}
    for did in draft_mod.list_drafts():
        try:
            dr = draft_mod.load_draft(did)
        except Exception:
            continue
        if not dr.picks:
            continue
        if season and not dr.date.startswith(f"{season}-"):
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
        if season and h.get("season") != season:
            continue
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
def undo_last_pick(draft_id: str, drafter: str | None = None):
    """Pop the most recent pick. If `drafter` is provided, refuse unless that
    drafter actually made the last pick — prevents one user accidentally undoing
    another user's pick during a live draft. Without `drafter`, behavior is
    unrestricted (admin / legacy callers)."""
    try:
        dr = draft_mod.load_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(404, f"draft {draft_id} not found")
    if not dr.picks:
        raise HTTPException(400, "no picks to undo")
    last = dr.picks[-1]
    if drafter and last.drafter != drafter:
        raise HTTPException(403, f"the last pick was by {last.drafter}, not you ({drafter}). Only the drafter who made the pick can undo it.")
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
    # When the snake is in non-SP free-for-all mode, anyone with an open slot
    # can pick — the pool's "open slots" should reflect the UNION across all
    # drafters, not just the on-clock drafter's. Otherwise hitters show 'no
    # slot left' because the on-clock drafter (the lone SP-needer) only needs
    # SPs. Frontend filters per-user from there.
    if dr.non_sp_free_for_all() or dr.hitter_free_drafter():
        # Either free-for-all mode is active. The pool should show pills for
        # the union of every drafter's open slots so the OOO drafter can see
        # picks; frontend filters per-drafter using remaining_by_drafter.
        remaining_set: set[str] = set()
        for d in dr.drafters:
            remaining_set.update(dr.remaining_slots(d))
        remaining = list(remaining_set)
    else:
        remaining = dr.remaining_slots(on_clock[0]) if on_clock else []
    # Per-drafter remaining slots so the frontend can filter pills to slots the
    # currently-identified user can actually fill (avoids show-then-reject).
    remaining_by_drafter = {d: dr.remaining_slots(d) for d in dr.drafters}
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
    game_states = _game_state_map(dr)
    for p in pool:
        ls = lineups.get(p["player_id"])
        p["lineup_status"] = ls.get("status") if ls else "pending"
        slate_games = team_games.get(p.get("team_id") or 0, [])
        p["team_games_in_slate"] = [
            {
                "game_pk": gpk,
                "label": labels.get(gpk, ""),
                "state": game_states.get(gpk, "pre"),
            }
            for gpk in slate_games
        ]
        # Replaceable iff at least one of this player's slate games hasn't
        # started yet. Once every game's first-pitch has happened the player
        # can no longer be added to a roster for the day.
        p["replaceable"] = any(g["state"] == "pre" for g in p["team_games_in_slate"]) \
            if p["team_games_in_slate"] else False
    return {
        "on_the_clock": on_clock,
        "remaining_slots": remaining,
        "remaining_by_drafter": remaining_by_drafter,
        "non_sp_free": dr.non_sp_free_for_all(),
        "hitter_free_drafter": dr.hitter_free_drafter(),
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


def _live_projection(role: str, pre_game_proj: float, actual: float | None,
                      raw: dict | None, game_state: str | None,
                      components: dict | None) -> tuple[float, float]:
    """Returns (live_projection, remaining_fraction).

    live_projection = actual_so_far + remaining_estimate
      where remaining_estimate = pre_game_proj * (1 - completed_share_of_game)

    Hitters: completed_PAs / expected ~4.3 PAs gives the share consumed.
    Pitchers: completed_outs / expected_outs (from avg IP/start, default 16.5
    outs = 5.5 IP). For pre-game / no game / scratched players, remaining
    fraction is 1.0 so live = pre_game. For Final, remaining is 0 so live
    = actual.
    """
    state = (game_state or "").lower()
    is_final = "final" in state
    pre = pre_game_proj or 0.0
    act = actual or 0.0
    raw = raw or {}
    if is_final:
        return round(act, 2), 0.0
    # No actuals yet — pre-game or scheduled, projection stands
    if actual is None or not state or "scheduled" in state or "not in" in state or "pre" in state:
        return round(pre, 2), 1.0

    if role == "pitcher":
        # Pre-game expected outs (from components if available, else 5.5 IP)
        ip_per_start = (components or {}).get("ip_per_start") or 5.5
        expected_outs = max(6, int(round(ip_per_start * 3)))   # at least 2 IP
        completed_outs = int(raw.get("outs") or 0)
        remaining = max(0.0, (expected_outs - completed_outs) / expected_outs)
    else:
        # Hitter: expected ~4.3 PAs over the full game. Prefer the MLB API's
        # plateAppearances count (captures every PA including pure outs),
        # falling back to AB + BB + HBP if only AB-based stats are present,
        # falling back to event-summed PAs as a last resort.
        expected_pa = 4.3
        pa = int(raw.get("PA") or 0)
        if not pa:
            ab = int(raw.get("AB") or 0)
            if ab:
                pa = ab + int(raw.get("BB", 0)) + int(raw.get("HBP", 0))
            else:
                pa = int(raw.get("1B", 0) + raw.get("2B", 0) + raw.get("3B", 0)
                         + raw.get("HR", 0) + raw.get("BB", 0) + raw.get("HBP", 0)
                         + raw.get("K", 0))
        remaining = max(0.0, (expected_pa - pa) / expected_pa)
    live = act + pre * remaining
    return round(live, 2), round(remaining, 2)


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

    def _pick_row(p, ps):
        pre_proj = (live_proj_by_id[p.player_id].projected_points
                    if p.player_id in live_proj_by_id
                    else (p.projected_points or 0.0))
        actual = (ps.points if ps and ps.played else None)
        components = (live_proj_by_id[p.player_id].components
                      if p.player_id in live_proj_by_id else None)
        live_proj, remaining_frac = _live_projection(
            role=(ps.role if ps else p.role),
            pre_game_proj=pre_proj,
            actual=actual,
            raw=(ps.raw if ps else None),
            game_state=(ps.game_state if ps else None),
            components=components,
        )
        return {
            "slot": p.slot,
            "name": p.name,
            "player_id": p.player_id,
            "pick_number": p.pick_number,
            "drafter": p.drafter,
            "projected": pre_proj,
            "live_projection": live_proj,
            "remaining_fraction": remaining_frac,
            "actual": actual,
            "raw": (ps.raw if ps else None),
            "game_state": (ps.game_state if ps else None),
            "counted": (ps.counted_in_total if ps else False),
            "played": (ps.played if ps else False),
            "lineup_status": (ps.lineup_status if ps else "pending"),
            "promoted": (ps.promoted_from_bench if ps else False),
            "breakdown": (ps.breakdown if ps else []),
        }

    return {
        "draft_id": draft_id,
        "standings": [
            {
                "drafter": s.drafter,
                "rank": s.rank,
                "total": round(s.total, 2),
                "full_total": round(s.full_total, 2),
                # Live projected total = sum of each counted pick's live_projection
                "live_projected_total": round(sum(
                    _live_projection(
                        role=(ps.role if ps else p.role),
                        pre_game_proj=(live_proj_by_id[p.player_id].projected_points
                                       if p.player_id in live_proj_by_id
                                       else (p.projected_points or 0.0)),
                        actual=(ps.points if ps and ps.played else None),
                        raw=(ps.raw if ps else None),
                        game_state=(ps.game_state if ps else None),
                        components=(live_proj_by_id[p.player_id].components
                                    if p.player_id in live_proj_by_id else None),
                    )[0]
                    for p, ps in s.picks if (ps is None or ps.counted_in_total)
                ), 2),
                "picks": [_pick_row(p, ps) for p, ps in s.picks],
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
        "non_sp_free": dr.non_sp_free_for_all(),
        "next_ooo_drafter": dr.next_ooo_drafter(),
        "hitter_free_drafter": dr.hitter_free_drafter(),
        "game_pks": list(dr.game_pks),
        "selected_games": _selected_games_summary(dr),
        "rosters": {
            d: [_pick_dict(p) for p in dr.roster_for(d)]
            for d in dr.drafters
        },
    }


def _game_state_map(dr) -> dict[int, str]:
    """gamePk -> 'pre' | 'live' | 'final'. Used to block replacement candidates
    whose game has already started — once first pitch happens you can't add
    a new player to a roster for that day."""
    selected = set(dr.game_pks) if dr.game_pks else None
    out: dict[int, str] = {}
    for g in mlb_api.schedule(Date.fromisoformat(dr.date)):
        pk = g.get("gamePk")
        if pk is None or (selected is not None and pk not in selected):
            continue
        abstract = (g.get("status") or {}).get("abstractGameState") or ""
        if abstract == "Live":
            out[pk] = "live"
        elif abstract == "Final":
            out[pk] = "final"
        else:
            # Preview / Scheduled / Pre-Game / Warmup / Postponed / etc.
            out[pk] = "pre"
    return out


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
