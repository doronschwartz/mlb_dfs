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

    Optimal hitter assignment: pool every hitter (regardless of drafted slot),
    sort by actual points DESC, place each into the most-restrictive eligible
    starting slot (IF → OF → UTIL) with capacity remaining. OOL players still
    get placed if their slot would otherwise go empty, but they're sorted to
    the bottom on ties so a played 'in' player wins.

    This is provably optimal here because slot eligibility forms a hierarchy
    (IF and OF both feed UTIL). It naturally chains swaps the old two-phase
    greedy missed — e.g. if BN (OF-only) outscores a UTIL starter who is
    IF-eligible, the UTIL starter slides into IF, displacing a weaker IF
    starter to bench.

    SPs always count; the bench can never replace pitching.
    Full total: sum of every drafted player's score, ignoring swaps.
    """
    # Slot capacities mirror the draft template's hitter slots.
    SLOT_CAP = {"IF": 3, "OF": 3, "UTIL": 1}
    SLOT_PRIORITY = ("IF", "OF", "UTIL")

    hitters: list[tuple[Pick, PlayerScore]] = []
    sps: list[tuple[Pick, PlayerScore]] = []
    full_total = 0.0

    for pick, ps in picks_with_scores:
        full_total += ps.points
        if pick.slot == "SP":
            sps.append((pick, ps))
        else:
            hitters.append((pick, ps))

    # Defaults: SPs always count, hitters start un-counted and we'll place them.
    for _, ps in sps:
        ps.counted_in_total = True
    for _, ps in hitters:
        ps.counted_in_total = False
        ps.promoted_from_bench = False

    # Sort hitters by points DESC. Tiebreaker: lineup_status 'in/pending' beats
    # 'out' (so a 0-pt OOL starter doesn't grab a slot ahead of a 0-pt 'in'
    # player who could still play). Bench players (drafted slot=BN) get a tiny
    # tiebreak edge below 'in' starters at equal pts so existing layouts feel
    # familiar pre-game.
    def _key(t: tuple[Pick, PlayerScore]) -> tuple:
        pick, ps = t
        ool_penalty = 1 if (ps.lineup_status == "out") else 0
        bench_penalty = 1 if pick.slot == "BN" else 0
        return (-ps.points, ool_penalty, bench_penalty)

    remaining = dict(SLOT_CAP)
    for pick, ps in sorted(hitters, key=_key):
        for s in SLOT_PRIORITY:
            if remaining[s] <= 0:
                continue
            if not _slot_eligible(s, pick):
                continue
            remaining[s] -= 1
            ps.counted_in_total = True
            if pick.slot == "BN":
                ps.promoted_from_bench = True
            break

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
    # Parallel boxscore fetch — was serial, blocking on every game. On a
    # 15-game Saturday slate this could push the calibration endpoint past
    # Fly's 60s gateway timeout. ThreadPoolExecutor turns 15s into ~2s.
    games = list(mlb_api.schedule(d))
    targets = [
        (g.get("gamePk"), (g.get("status") or {}).get("detailedState", ""))
        for g in games
        if g.get("gamePk") and (game_pks is None or g.get("gamePk") in game_pks)
    ]

    from concurrent.futures import ThreadPoolExecutor
    def _fetch(pk):
        try:
            return pk, mlb_api.boxscore(pk)
        except Exception:
            return pk, None

    boxes: dict[int, dict] = {}
    if targets:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for pk, box in ex.map(_fetch, [t[0] for t in targets]):
                if box is not None:
                    boxes[pk] = box

    out: dict[int, list[dict]] = {}
    for pk, state in targets:
        box = boxes.get(pk)
        if box is None:
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
                # Two-way players (Ohtani) appear in BOTH iterators on a day they
                # bat and start. Keep both lines — _score_player filters by the
                # pick's role/slot so the right line is scored.
                out.setdefault(pid, []).append(
                    {"role": "pitcher", "stats": stats, "state": state, "game_pk": pk}
                )
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
    # Two-way player support: a pick can only score lines matching its role.
    # If the pick is in an SP slot or has role=pitcher, only score pitcher lines.
    # Otherwise (UTIL / hitter slot), only score hitter lines.
    want_pitcher = (pick.slot == "SP") or (pick.role == "pitcher")
    lines = [ln for ln in lines if (ln["role"] == "pitcher") == want_pitcher]
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
