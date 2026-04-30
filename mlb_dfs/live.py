"""Live scoring of a saved draft against today's box scores."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from . import mlb_api
from .draft import Draft, Pick, _slot_eligible
from .scoring import HitterLine, PitcherLine


HITTER_STARTING_SLOTS = ("IF", "OF", "UTIL")


@dataclass
class PlayerScore:
    player_id: int
    name: str
    role: str
    points: float
    raw: dict = field(default_factory=dict)
    game_state: str = ""
    # True if this player's points were counted toward the drafter's Total.
    # For hitters/BN: top-N by score (where N = # starting hitter slots).
    # For SP: always True (bench can't replace pitching).
    counted_in_total: bool = False


@dataclass
class DrafterScore:
    drafter: str
    total: float
    full_total: float  # raw sum of every drafted player's score
    rank: int = 0
    picks: list[tuple[Pick, PlayerScore | None]] = field(default_factory=list)


def compute_totals(picks_with_scores: list[tuple[Pick, "PlayerScore | None"]]) -> tuple[float, float]:
    """Returns (total, full_total) and mutates each PlayerScore.counted_in_total.

    Bench-swap rule (position-aware):
      - SPs always count; the bench can never replace pitching.
      - Every IF/OF/UTIL starter counts by default.
      - The BN player promotes into a starting slot if and only if (a) they
        are position-eligible for that slot type and (b) they outscore the
        weakest starter at any of those eligible slots. Concretely:
          IF on the bench  -> can replace the worst of the 3 IF + 1 UTIL slots
          OF on the bench  -> can replace the worst of the 3 OF + 1 UTIL slots
          (UTIL slot accepts any hitter, hence both groups can reach it.)
      - When a swap happens, the displaced starter is marked benched-out.

    Full total: sum of every drafted player's score, regardless of swap.
    """
    starters: list[tuple[Pick, PlayerScore | None]] = []   # IF / OF / UTIL
    bench: list[tuple[Pick, PlayerScore | None]] = []      # BN (hitter-only)
    sps:    list[tuple[Pick, PlayerScore | None]] = []     # SP
    full_total = 0.0

    for pick, ps in picks_with_scores:
        pts = ps.points if ps else 0.0
        full_total += pts
        if pick.slot in HITTER_STARTING_SLOTS:
            starters.append((pick, ps))
        elif pick.slot == "BN":
            bench.append((pick, ps))
        elif pick.slot == "SP":
            sps.append((pick, ps))

    # Defaults: every starter and every SP counts; bench does not.
    for _, ps in starters:
        if ps is not None:
            ps.counted_in_total = True
    for _, ps in sps:
        if ps is not None:
            ps.counted_in_total = True
    for _, ps in bench:
        if ps is not None:
            ps.counted_in_total = False

    # Try the swap: which starter slots can each bench player promote into?
    for bn_pick, bn_ps in bench:
        bn_pts = bn_ps.points if bn_ps else 0.0
        eligible_targets = [
            (sp_pick, sp_ps) for sp_pick, sp_ps in starters
            if _slot_eligible(sp_pick.slot, bn_pick)
            and (sp_ps is None or sp_ps.counted_in_total)
        ]
        if not eligible_targets:
            continue
        worst_pick, worst_ps = min(
            eligible_targets,
            key=lambda t: (t[1].points if t[1] else 0.0),
        )
        worst_pts = worst_ps.points if worst_ps else 0.0
        if bn_pts > worst_pts:
            if worst_ps is not None:
                worst_ps.counted_in_total = False
            if bn_ps is not None:
                bn_ps.counted_in_total = True

    counted_total = sum(
        ps.points
        for _, ps in picks_with_scores
        if ps is not None and ps.counted_in_total
    )
    return counted_total, full_total


def score_draft(draft: Draft, *, on_date: Date | None = None) -> list[DrafterScore]:
    on_date = on_date or Date.fromisoformat(draft.date)
    game_filter = set(draft.game_pks) if draft.game_pks else None
    box_index = _index_boxscores(on_date, game_pks=game_filter)

    drafter_scores: dict[str, DrafterScore] = {}
    for d in draft.drafters:
        picks_with_scores: list[tuple[Pick, PlayerScore | None]] = []
        for pick in (p for p in draft.picks if p.drafter == d):
            lines = box_index.get(pick.player_id)
            ps = _score_player(pick, lines) if lines else None
            picks_with_scores.append((pick, ps))
        total, full_total = compute_totals(picks_with_scores)
        drafter_scores[d] = DrafterScore(
            drafter=d, total=total, full_total=full_total, picks=picks_with_scores,
        )

    ranked = sorted(drafter_scores.values(), key=lambda d: d.total, reverse=True)
    for i, d in enumerate(ranked, start=1):
        d.rank = i
    return ranked


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
    """Score every game line for the player and sum the results.

    For doubleheaders this means each game contributes its own points
    (including its own QS / CG / SHO / NH bonuses if earned that game),
    rather than the second game overwriting the first.
    """
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

    return PlayerScore(
        player_id=pick.player_id,
        name=pick.name,
        role=role,
        points=round(total_pts, 2),
        raw=raw_totals,
        game_state=last_state,
    )
