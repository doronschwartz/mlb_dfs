from mlb_dfs.draft import Pick
from mlb_dfs.live import PlayerScore, _score_player, compute_totals


def _pick(slot: str, role: str = "hitter") -> Pick:
    return Pick(
        drafter="A", slot=slot, player_id=1, name="X",
        position="1B", role=role, projected_points=0.0, pick_number=1,
    )


def _score(pts: float, role: str = "hitter", *, played: bool = True, lineup_status: str = "in") -> PlayerScore:
    return PlayerScore(
        player_id=1, name="X", role=role, points=pts,
        played=played, lineup_status=lineup_status,
    )


def test_bench_replaces_worst_hitter_matches_sheet_sample():
    """Spreadsheet sample (Stock, 2026-04-30):
    IF: 7, -2.5, -2  | OF: 9, 15, 12  | UTIL: 2  | BN: 21  | SP: 19.5, 22.45
    Sheet says Total = 105.95, Full Total = 103.45.
    Bench (21) replaces the worst starter (-2.5).
    """
    pairs = [
        (_pick("IF"),    _score(7.0)),
        (_pick("IF"),    _score(-2.5)),
        (_pick("IF"),    _score(-2.0)),
        (_pick("OF"),    _score(9.0)),
        (_pick("OF"),    _score(15.0)),
        (_pick("OF"),    _score(12.0)),
        (_pick("UTIL"),  _score(2.0)),
        (_pick("BN"),    _score(21.0)),
        (_pick("SP", "pitcher"), _score(19.5,  "pitcher")),
        (_pick("SP", "pitcher"), _score(22.45, "pitcher")),
    ]
    total, full_total = compute_totals(pairs)
    assert round(total, 2) == 105.95
    assert round(full_total, 2) == 103.45


def test_bench_does_not_replace_when_worse():
    """If the bench player scores below every starter, total == full hitter sum."""
    pairs = [
        (_pick("IF"),    _score(10.0)),
        (_pick("IF"),    _score(8.0)),
        (_pick("IF"),    _score(7.0)),
        (_pick("OF"),    _score(9.0)),
        (_pick("OF"),    _score(11.0)),
        (_pick("OF"),    _score(6.0)),
        (_pick("UTIL"),  _score(5.0)),
        (_pick("BN"),    _score(0.0)),  # worst
        (_pick("SP", "pitcher"), _score(20.0, "pitcher")),
        (_pick("SP", "pitcher"), _score(15.0, "pitcher")),
    ]
    total, full_total = compute_totals(pairs)
    # 10+8+7+9+11+6+5 + 20+15 = 56 + 35 = 91
    assert round(total, 2) == 91.0
    # full = 91 + bench(0) = 91
    assert round(full_total, 2) == 91.0


def test_counted_in_total_flags_correct_players():
    """The bench should be marked counted; the worst starter should be marked benched."""
    worst = _score(-5.0)
    bench = _score(20.0)
    pairs = [
        (_pick("IF"),    _score(10.0)),
        (_pick("IF"),    _score(8.0)),
        (_pick("IF"),    worst),
        (_pick("OF"),    _score(9.0)),
        (_pick("OF"),    _score(11.0)),
        (_pick("OF"),    _score(6.0)),
        (_pick("UTIL"),  _score(5.0)),
        (_pick("BN"),    bench),
        (_pick("SP", "pitcher"), _score(15.0, "pitcher")),
        (_pick("SP", "pitcher"), _score(15.0, "pitcher")),
    ]
    compute_totals(pairs)
    assert bench.counted_in_total is True
    assert worst.counted_in_total is False


def _bn(position: str, pts: float, *, lineup_status: str = "in") -> tuple[Pick, PlayerScore]:
    """Build a (Pick, PlayerScore) pair for a BN slot at a specific position."""
    pick = Pick(
        drafter="A", slot="BN", player_id=99, name="Bench",
        position=position, role="hitter", projected_points=0.0, pick_number=8,
    )
    return pick, PlayerScore(
        player_id=99, name="Bench", role="hitter", points=pts,
        played=True, lineup_status=lineup_status,
    )


def test_outfield_bench_cannot_replace_infielder():
    """An OF on the bench can only swap with OF or UTIL — not IF."""
    bad_if = _score(-3.0)
    of_bench_pick, of_bench_ps = _bn("RF", 15.0)
    pairs = [
        (_pick("IF"),    bad_if),       # worst overall, but bench can't reach
        (_pick("IF"),    _score(8.0)),
        (_pick("IF"),    _score(7.0)),
        (_pick("OF"),    _score(9.0)),
        (_pick("OF"),    _score(11.0)),
        (_pick("OF"),    _score(6.0)),
        (_pick("UTIL"),  _score(5.0)),  # weakest among OF+UTIL targets
        (of_bench_pick,  of_bench_ps),
        (_pick("SP", "pitcher"), _score(10.0, "pitcher")),
        (_pick("SP", "pitcher"), _score(10.0, "pitcher")),
    ]
    total, _ = compute_totals(pairs)
    # IF -3 stays in (RF bench can't reach IF).
    # OF/UTIL pool: 9, 11, 6, 5 -> worst is UTIL 5; bench 15 > 5 -> swap.
    # Total: -3 + 8 + 7 + 9 + 11 + 6 + 15 + 20 = 73
    assert round(total, 2) == 73.0
    assert bad_if.counted_in_total is True       # bench couldn't replace it
    assert of_bench_ps.counted_in_total is True


def test_infield_bench_cannot_replace_outfielder():
    """An IF on the bench can only swap with IF or UTIL — not OF."""
    bad_of = _score(-3.0)
    if_bench_pick, if_bench_ps = _bn("1B", 15.0)
    pairs = [
        (_pick("IF"),    _score(10.0)),
        (_pick("IF"),    _score(8.0)),
        (_pick("IF"),    _score(7.0)),
        (_pick("OF"),    bad_of),       # worst overall, IF bench can't reach
        (_pick("OF"),    _score(11.0)),
        (_pick("OF"),    _score(6.0)),
        (_pick("UTIL"),  _score(5.0)),  # weakest among IF+UTIL targets
        (if_bench_pick,  if_bench_ps),
        (_pick("SP", "pitcher"), _score(10.0, "pitcher")),
        (_pick("SP", "pitcher"), _score(10.0, "pitcher")),
    ]
    total, _ = compute_totals(pairs)
    # OF -3 stays in (1B bench can't reach OF).
    # IF/UTIL pool: 10, 8, 7, 5 -> worst is UTIL 5; bench 15 > 5 -> swap.
    # Total: 10 + 8 + 7 + -3 + 11 + 6 + 15 + 20 = 74
    assert round(total, 2) == 74.0
    assert bad_of.counted_in_total is True       # bench couldn't reach
    assert if_bench_ps.counted_in_total is True


def test_ool_starter_promotes_eligible_bench_pregame():
    """Pre-game (no actual data) IF starter is OOL, IF bench is in lineup
    -> bench gets promoted, total reflects bench's projected/actual instead.
    """
    bad_if = PlayerScore(player_id=2, name="OOL Starter", role="hitter",
                         points=0.0, played=False, lineup_status="out")
    if_bench_pick, if_bench_ps = _bn("1B", 0.0, lineup_status="in")  # also pre-game
    pairs = [
        (_pick("IF"), bad_if),
        (_pick("IF"), _score(0.0, played=False)),
        (_pick("IF"), _score(0.0, played=False)),
        (_pick("OF"), _score(0.0, played=False)),
        (_pick("OF"), _score(0.0, played=False)),
        (_pick("OF"), _score(0.0, played=False)),
        (_pick("UTIL"), _score(0.0, played=False)),
        (if_bench_pick, if_bench_ps),
        (_pick("SP", "pitcher"), _score(0.0, "pitcher", played=False)),
        (_pick("SP", "pitcher"), _score(0.0, "pitcher", played=False)),
    ]
    compute_totals(pairs)
    assert if_bench_ps.counted_in_total is True
    assert if_bench_ps.promoted_from_bench is True
    assert bad_if.counted_in_total is False


def test_ool_promotion_respects_position_eligibility():
    """OF starter is OOL but bench is IF (1B) — bench can't replace OF, no swap."""
    bad_of = PlayerScore(player_id=2, name="OOL OF Starter", role="hitter",
                        points=0.0, played=False, lineup_status="out")
    if_bench_pick, if_bench_ps = _bn("1B", 0.0, lineup_status="in")
    pairs = [
        (_pick("IF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("IF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("IF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("OF"), bad_of),  # OOL but bench can't reach
        (_pick("OF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("OF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("UTIL"), _score(0.0, played=False, lineup_status="in")),
        (if_bench_pick, if_bench_ps),
        (_pick("SP", "pitcher"), _score(0.0, "pitcher", played=False)),
        (_pick("SP", "pitcher"), _score(0.0, "pitcher", played=False)),
    ]
    compute_totals(pairs)
    # IF bench (1B) cannot replace an OOL OF starter; no promotion.
    assert if_bench_ps.counted_in_total is False
    assert if_bench_ps.promoted_from_bench is False
    # The OOL OF starter still "counts" (his 0) — there's no eligible replacement.
    assert bad_of.counted_in_total is True


def test_ool_promotion_into_util_when_starter_position_blocked():
    """OF starter OOL, IF bench can't reach OF, but UTIL is also OOL —
    bench fills UTIL instead."""
    ool_util = PlayerScore(player_id=3, name="OOL UTIL", role="hitter",
                           points=0.0, played=False, lineup_status="out")
    if_bench_pick, if_bench_ps = _bn("1B", 0.0, lineup_status="in")
    pairs = [
        (_pick("IF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("IF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("IF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("OF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("OF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("OF"), _score(0.0, played=False, lineup_status="in")),
        (_pick("UTIL"), ool_util),
        (if_bench_pick, if_bench_ps),
        (_pick("SP", "pitcher"), _score(0.0, "pitcher", played=False)),
        (_pick("SP", "pitcher"), _score(0.0, "pitcher", played=False)),
    ]
    compute_totals(pairs)
    assert if_bench_ps.counted_in_total is True
    assert if_bench_ps.promoted_from_bench is True
    assert ool_util.counted_in_total is False


def test_doubleheader_hitter_sums_points_across_both_games():
    """A hitter who plays both games of a doubleheader gets points for each.
    Game 1: 1 single (3 pts). Game 2: 1 HR + 1 RBI (10 + 2 = 12 pts). Total 15.
    """
    pick = _pick("IF")
    lines = [
        {"role": "hitter", "stats": {"hits": 1, "doubles": 0, "triples": 0, "homeRuns": 0, "rbi": 0},
         "state": "Final", "game_pk": 1},
        {"role": "hitter", "stats": {"hits": 1, "doubles": 0, "triples": 0, "homeRuns": 1, "rbi": 1},
         "state": "Final", "game_pk": 2},
    ]
    ps = _score_player(pick, lines)
    assert ps.points == 15.0
    assert ps.raw["games"] == 2
    assert ps.raw["1B"] == 1
    assert ps.raw["HR"] == 1
    assert ps.raw["RBI"] == 1


def test_doubleheader_slate_filter_excludes_other_game():
    """If only Game 2 is in the slate, _index_boxscores must skip Game 1.
    We don't hit the network here — just verify _score_player behavior on
    a single-line input still produces the single-game score, which is what
    a slate filter would yield in real use.
    """
    pick = _pick("IF")
    lines = [
        # Only Game 2 made it through the slate filter
        {"role": "hitter", "stats": {"hits": 1, "doubles": 0, "triples": 0, "homeRuns": 1, "rbi": 1},
         "state": "Final", "game_pk": 2},
    ]
    ps = _score_player(pick, lines)
    assert ps.points == 12.0  # HR(10) + RBI(2)
    assert "games" not in ps.raw  # single game, no DH marker


def test_sp_always_counts_regardless_of_score():
    """Even a disastrous SP outing is counted; the bench can't replace pitching."""
    bad_sp = _score(-12.0, "pitcher")
    great_bench = _score(30.0)
    pairs = [
        (_pick("IF"),    _score(10.0)),
        (_pick("IF"),    _score(8.0)),
        (_pick("IF"),    _score(7.0)),
        (_pick("OF"),    _score(9.0)),
        (_pick("OF"),    _score(11.0)),
        (_pick("OF"),    _score(6.0)),
        (_pick("UTIL"),  _score(5.0)),
        (_pick("BN"),    great_bench),  # high but it can't replace bad_sp
        (_pick("SP", "pitcher"), bad_sp),
        (_pick("SP", "pitcher"), _score(10.0, "pitcher")),
    ]
    total, _ = compute_totals(pairs)
    # Hitter pool: 30, 11, 10, 9, 8, 7, 6, 5  -> top 7: 30+11+10+9+8+7+6 = 81
    # SP: -12 + 10 = -2
    assert round(total, 2) == 79.0
    assert bad_sp.counted_in_total is True  # SPs always count
    assert great_bench.counted_in_total is True  # bench replaced UTIL(5)
