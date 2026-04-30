"""Live scoring of a saved draft against today's box scores."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from . import mlb_api
from .draft import Draft, Pick, _slot_eligible
from .scoring import HitterLine, PitcherLine, HITTER_POINTS, PITCHER_POINTS


HITTER_STARTING_SLOTS = ("IF", "OF", "UTIL")

# Game states that mean the game hasn't actually started yet — players in the
# box score with all-zero stats during these states should NOT count as 'played'
# (so the bench-swap UI doesn't strike them through pre-game).
PRE_GAME_STATES = frozenset({
    "Scheduled", "Pre-Game", "Warmup", "Delayed Start", "TBA",
    "Postponed", "Cancelled", "",
})


@dataclass
class PlayerScore:
    player_id: int
    name: str
    role: str
    points: float
    raw: dict = field(default_factory=dict)
    game_state: str = ""
    # True if this player's points were counted toward the drafter's Total.
    counted_in_total: bool = False
    # True if the player has actual game data for the day (line in box score).
    # False = pre-game / no data yet. Used so compute_totals can do its
    # score-based bench swap only on real numbers.
    played: bool = False
    # Lineup status for this pick on the day: "in" / "out" / "pending".
    lineup_status: str = "pending"
    # True if this BN player got promoted into a starting slot because the
    # starter was OOL (out of lineup). Surfaces a "PROMOTED" tag in the UI.
    promoted_from_bench: bool = False
    # Per-stat breakdown of how this player's score was assembled, for UI
    # tooltips. Each entry: {label, count, points_each, total}.
    breakdown: list[dict] = field(default_factory=list)


@dataclass
class DrafterScore:
    drafter: str
    total: float
    full_total: float  # raw sum of every drafted player's score
    rank: int = 0
    picks: list[tuple[Pick, PlayerScore | None]] = field(default_factory=list)


def compute_totals(picks_with_scores: list[tuple[Pick, "PlayerScore"]]) -> tuple[float, float]:
    """Returns (total, full_total) and mutates each PlayerScore.counted_in_total.

    Two-phase bench swap (position-aware):

    Phase 1 — OOL promotion (works pre-game):
      If a starter is "out" of the lineup AND a position-eligible bench
      player is in/pending, the bench takes that slot. The OOL starter is
      benched-out. This applies even with no game data yet — once MLB
      posts the lineup card and the player isn't in it, the swap fires.

    Phase 2 — score-based swap (post-game / mid-game):
      For any bench player still on the bench whose game has actual data,
      if they outscore the weakest position-eligible starter, swap them
      in. Only compares played-vs-played scores so a benched player who
      hasn't played yet doesn't get promoted ahead of a 0-pt-but-played
      starter.

    SPs always count; the bench can never replace pitching.
    Full total: sum of every drafted player's score, ignoring swaps.
    """
    starters: list[tuple[Pick, PlayerScore]] = []   # IF / OF / UTIL
    bench: list[tuple[Pick, PlayerScore]] = []      # BN (hitter-only)
    sps: list[tuple[Pick, PlayerScore]] = []        # SP
    full_total = 0.0

    for pick, ps in picks_with_scores:
        full_total += ps.points
        if pick.slot in HITTER_STARTING_SLOTS:
            starters.append((pick, ps))
        elif pick.slot == "BN":
            bench.append((pick, ps))
        elif pick.slot == "SP":
            sps.append((pick, ps))

    # Defaults
    for _, ps in starters:
        ps.counted_in_total = True
    for _, ps in sps:
        ps.counted_in_total = True
    for _, ps in bench:
        ps.counted_in_total = False
        ps.promoted_from_bench = False

    # ---- Phase 1: OOL bench promotion ------------------------------------
    for bn_pick, bn_ps in bench:
        if bn_ps.lineup_status == "out":
            continue  # bench is also out, can't help anyone
        ool_targets = [
            (sp_pick, sp_ps) for sp_pick, sp_ps in starters
            if sp_ps.counted_in_total                        # not already swapped out
            and _slot_eligible(sp_pick.slot, bn_pick)
            and sp_ps.lineup_status == "out"
        ]
        if not ool_targets:
            continue
        # Replace the weakest OOL starter (lowest points; usually 0 pre-game).
        worst_pick, worst_ps = min(ool_targets, key=lambda t: t[1].points)
        worst_ps.counted_in_total = False
        bn_ps.counted_in_total = True
        bn_ps.promoted_from_bench = True

    # ---- Phase 2: score-based swap (live, "best of" rule) ---------------
    # At any point in time the higher-scoring of (bench, eligible starter)
    # is the one that counts. Pre-game players score 0; that's a valid
    # comparison input — if a starter has gone negative and the bench
    # hasn't played yet, the bench's 0 wins.
    for bn_pick, bn_ps in bench:
        if bn_ps.counted_in_total:
            continue  # already promoted in phase 1
        eligible = [
            (sp_pick, sp_ps) for sp_pick, sp_ps in starters
            if sp_ps.counted_in_total
            and _slot_eligible(sp_pick.slot, bn_pick)
        ]
        if not eligible:
            continue
        worst_pick, worst_ps = min(eligible, key=lambda t: t[1].points)
        if bn_ps.points > worst_ps.points:
            worst_ps.counted_in_total = False
            bn_ps.counted_in_total = True

    counted_total = sum(ps.points for _, ps in picks_with_scores if ps.counted_in_total)
    return counted_total, full_total


def score_draft(draft: Draft, *, on_date: Date | None = None) -> list[DrafterScore]:
    on_date = on_date or Date.fromisoformat(draft.date)
    game_filter = set(draft.game_pks) if draft.game_pks else None
    box_index = _index_boxscores(on_date, game_pks=game_filter)
    try:
        lineups_map = mlb_api.lineups_by_date(on_date, game_pks=game_filter)
    except Exception:
        lineups_map = {}

    drafter_scores: dict[str, DrafterScore] = {}
    for d in draft.drafters:
        picks_with_scores: list[tuple[Pick, PlayerScore]] = []
        for pick in (p for p in draft.picks if p.drafter == d):
            lines = box_index.get(pick.player_id)
            if lines:
                ps = _score_player(pick, lines)
                # Only treat the player as 'played' once their game has
                # actually started — otherwise pre-game zero stat lines
                # would trigger benched-out styling for every BN pick.
                last_state = lines[-1].get("state", "")
                ps.played = last_state not in PRE_GAME_STATES
            else:
                ps = PlayerScore(
                    player_id=pick.player_id, name=pick.name, role=pick.role,
                    points=0.0, raw={}, game_state="", played=False,
                )
            ls = lineups_map.get(pick.player_id)
            ps.lineup_status = (ls.get("status", "pending") if ls else "pending")
            picks_with_scores.append((pick, ps))
        total, full_total = compute_totals(picks_with_scores)
        drafter_scores[d] = DrafterScore(
            drafter=d, total=total, full_total=full_total, picks=picks_with_scores,
        )

    ranked = sorted(drafter_scores.values(), key=lambda d: d.total, reverse=True)
    for i, d in enumerate(ranked, start=1):
        d.rank = i
    return ranked


_HITTER_BREAKDOWN_PAIRS = (
    ("1B", "single"), ("2B", "double"), ("3B", "triple"), ("HR", "homeRun"),
    ("R", "run"), ("RBI", "rbi"), ("BB", "baseOnBalls"), ("HBP", "hitByPitch"),
    ("SB", "stolenBase"), ("GIDP", "groundIntoDoublePlay"), ("K", "strikeOut"),
)
_PITCHER_BREAKDOWN_PAIRS = (
    ("Outs", "out", "outs"),
    ("K", "strikeOut", "K"),
    ("ER", "earnedRun", "ER"),
    ("H allowed", "hitAllowed", "H"),
    ("BB issued", "walkIssued", "BB"),
    ("HBP", "hitBatsman", "HBP"),
)
_PITCHER_BONUS_PAIRS = (
    ("QS bonus", "qualityStart", "QS"),
    ("CG bonus", "completeGame", "CG"),
    ("SHO bonus", "shutout", "SHO"),
    ("NH bonus", "noHitter", "NH"),
)


def _hitter_breakdown(raw: dict) -> list[dict]:
    out = []
    for label, key in _HITTER_BREAKDOWN_PAIRS:
        n = int(raw.get(label, 0) or 0)
        if not n:
            continue
        out.append({
            "label": label, "count": n,
            "points_each": HITTER_POINTS[key],
            "total": round(n * HITTER_POINTS[key], 2),
        })
    return out


def _pitcher_breakdown(raw: dict) -> list[dict]:
    out = []
    for label, points_key, raw_key in _PITCHER_BREAKDOWN_PAIRS:
        n = int(raw.get(raw_key, 0) or 0)
        if not n:
            continue
        out.append({
            "label": label, "count": n,
            "points_each": PITCHER_POINTS[points_key],
            "total": round(n * PITCHER_POINTS[points_key], 2),
        })
    for label, points_key, raw_key in _PITCHER_BONUS_PAIRS:
        n = int(raw.get(raw_key, 0) or 0)
        if not n:
            continue
        out.append({
            "label": label, "count": n,
            "points_each": PITCHER_POINTS[points_key],
            "total": round(n * PITCHER_POINTS[points_key], 2),
        })
    return out


def _index_boxscores(d: Date, *, game_pks: set[int] | None = None) -> dict[int, list[dict]]:
    """Returns {player_id: [game_line, ...]}.

    A player gets one entry per game they appeared in *that is included in the
    slate*. If `game_pks` is provided (the draft's selected games), games
    outside that set are skipped — so on a doubleheader day where only one
    game is in the slate, only that game's stats are scored.

    Without a filter, all games on the date are included; doubleheaders
    yield two entries that are summed by the scorer.
    """
    out: dict[int, list[dict]] = {}
    for game in mlb_api.schedule(d):
        pk = game.get("gamePk")
        if game_pks is not None and pk not in game_pks:
            continue
        state = (game.get("status") or {}).get("detailedState", "")
        try:
            box = mlb_api.boxscore(pk)
        except Exception:
            continue
        for person, stats in mlb_api.iter_boxscore_batters(box):
            pid = person.get("id")
            if pid:
                out.setdefault(pid, []).append(
                    {"role": "hitter", "stats": stats, "state": state, "game_pk": pk}
                )
        for person, stats in mlb_api.iter_boxscore_pitchers(box):
            pid = person.get("id")
            if pid and person.get("isStarter"):
                # If a player both batted and started, the starter line
                # supersedes the batting line for SP-slotted players. Replace
                # any earlier hitter entry for that pid with the pitcher line.
                existing = out.get(pid, [])
                out[pid] = [e for e in existing if e["role"] != "hitter"] + [
                    {"role": "pitcher", "stats": stats, "state": state, "game_pk": pk}
                ]
    return out


def _score_player(pick: Pick, lines: list[dict]) -> PlayerScore:
    """Score the line(s) the pick is associated with.

    If `pick.game_pk` is set (the DH chooser case), only the line whose
    game_pk matches is scored — even if the player appeared in another
    slate game. Without a game_pk (typical case), all of the player's
    lines that the slate filter let through are summed.
    """
    if pick.game_pk is not None:
        lines = [ln for ln in lines if ln.get("game_pk") == pick.game_pk]
    if not lines:
        return PlayerScore(
            player_id=pick.player_id, name=pick.name, role=pick.role,
            points=0.0, raw={}, game_state="not in selected game",
            played=False,
        )
    total_pts = 0.0
    raw_totals: dict = {}
    last_state = ""
    role = lines[0]["role"]

    for line in lines:
        last_state = line.get("state", "")
        if pick.slot == "SP" or line["role"] == "pitcher":
            role = "pitcher"
            pl = PitcherLine.from_mlb_stats(line["stats"])
            total_pts += pl.points()
            for k, v in {
                "outs": pl.outs, "K": pl.strikeouts, "ER": pl.earned_runs,
                "H": pl.hits_allowed, "BB": pl.walks_issued, "HBP": pl.hit_batsmen,
            }.items():
                raw_totals[k] = raw_totals.get(k, 0) + v
            for k, v in {
                "QS": int(pl.is_quality_start()), "CG": int(pl.complete_game),
                "SHO": int(pl.shutout), "NH": int(pl.no_hitter),
            }.items():
                raw_totals[k] = raw_totals.get(k, 0) + v
        else:
            role = "hitter"
            hl = HitterLine.from_mlb_stats(line["stats"])
            total_pts += hl.points()
            for k, v in {
                "1B": hl.singles, "2B": hl.doubles, "3B": hl.triples, "HR": hl.home_runs,
                "R": hl.runs, "RBI": hl.rbi, "BB": hl.walks, "HBP": hl.hbp,
                "SB": hl.stolen_bases, "GIDP": hl.gidp, "K": hl.strikeouts,
            }.items():
                raw_totals[k] = raw_totals.get(k, 0) + v

    if len(lines) > 1:
        raw_totals["games"] = len(lines)
        last_state = f"{last_state} (DH x{len(lines)})"

    breakdown = (
        _pitcher_breakdown(raw_totals) if role == "pitcher"
        else _hitter_breakdown(raw_totals)
    )
    return PlayerScore(
        player_id=pick.player_id,
        name=pick.name,
        role=role,
        points=round(total_pts, 2),
        raw=raw_totals,
        game_state=last_state,
        breakdown=breakdown,
    )
