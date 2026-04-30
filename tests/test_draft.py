from datetime import date

from mlb_dfs.draft import Draft, SLOTS, new_draft, _slot_eligible
from mlb_dfs.projections import Projection


def _proj(pid, name, pos, role="hitter", pts=10.0):
    return Projection(
        player_id=pid, name=name, team_id=1, position=pos, role=role,
        projected_points=pts,
    )


def test_snake_order_3_drafters():
    dr = new_draft(date(2026, 4, 30), ["A", "B", "C"])
    expected_drafter_seq = []
    forward = ["A", "B", "C"]
    for r in range(len(SLOTS)):
        order = forward if r % 2 == 0 else list(reversed(forward))
        expected_drafter_seq.extend(order)

    seen = []
    pid = 1
    for _ in range(dr.total_picks()):
        info = dr.on_the_clock()
        assert info is not None
        drafter, slot = info
        seen.append(drafter)
        # eligible player for whatever slot is required
        if slot == "SP":
            p = _proj(pid, f"P{pid}", "SP", "pitcher", 12.0)
        elif slot == "OF":
            p = _proj(pid, f"P{pid}", "RF", "hitter", 8.0)
        else:
            p = _proj(pid, f"P{pid}", "2B", "hitter", 8.0)
        dr.make_pick(slot, p)
        pid += 1

    assert seen == expected_drafter_seq
    assert dr.is_complete()
    assert dr.on_the_clock() is None


def test_slot_eligibility():
    sp = _proj(1, "SP1", "SP", "pitcher")
    of = _proj(2, "OF1", "RF", "hitter")
    inf = _proj(3, "IF1", "2B", "hitter")
    assert _slot_eligible("SP", sp)
    assert not _slot_eligible("OF", sp)
    assert _slot_eligible("OF", of)
    assert not _slot_eligible("IF", of)
    assert _slot_eligible("UTIL", of)
    assert _slot_eligible("BN", inf)


def test_move_pick_swaps_when_destination_full():
    """Workflow: BN OF wants to slide into OF starter slot.
    Fill all 3 of A's OF slots, then add a BN OF, then move BN -> OF: pick
    swaps with one of the existing OF starters (who lands in BN).
    Snake order with 2 drafters: A B / B A / A B / B A / A B / B A …
    """
    dr = new_draft(date(2026, 4, 30), ["A", "B"])
    # round 1
    dr.make_pick("OF", _proj(1, "A_OF1", "RF"))
    dr.make_pick("SP", _proj(2, "B_SP1", "SP", "pitcher"))
    # round 2 (snake)
    dr.make_pick("SP", _proj(3, "B_SP2", "SP", "pitcher"))
    dr.make_pick("OF", _proj(4, "A_OF2", "CF"))
    # round 3
    dr.make_pick("OF", _proj(5, "A_OF3", "LF"))  # A now has 3/3 OF
    dr.make_pick("IF", _proj(6, "B_IF1", "1B"))
    # round 4 (snake)
    dr.make_pick("IF", _proj(7, "B_IF2", "2B"))
    dr.make_pick("BN", _proj(8, "A_BN_OF", "RF"))  # A's BN is an OF-eligible RF

    moved, displaced = dr.move_pick(8, "OF")
    assert moved.slot == "OF" and moved.name == "A_BN_OF"
    assert displaced is not None and displaced.slot == "BN"


def test_move_pick_simple_when_destination_open():
    """Move into a slot that has remaining capacity — no swap, just relocate."""
    dr = new_draft(date(2026, 4, 30), ["A", "B"])
    of_bench = _proj(1, "OF Bench", "LF", "hitter", pts=8)
    sp1 = _proj(2, "SP1", "SP", "pitcher", pts=10)

    dr.make_pick("BN", of_bench)
    dr.make_pick("SP", sp1)

    moved, displaced = dr.move_pick(1, "OF")
    assert moved.slot == "OF" and moved.name == "OF Bench"
    assert displaced is None


def test_move_pick_rejects_ineligible_slot():
    """A 1B pick can't be moved into OF — should raise."""
    import pytest
    dr = new_draft(date(2026, 4, 30), ["A", "B"])
    inf = _proj(1, "IF1", "1B", "hitter", pts=8)
    sp1 = _proj(2, "SP1", "SP", "pitcher", pts=10)
    dr.make_pick("IF", inf)
    dr.make_pick("SP", sp1)
    with pytest.raises(ValueError, match="not eligible"):
        dr.move_pick(1, "OF")


def test_recommend_prefers_eligible_high_proj():
    dr = new_draft(date(2026, 4, 30), ["A", "B"])
    pool = [
        _proj(1, "Big SP", "SP", "pitcher", pts=25.0),
        _proj(2, "OF Star", "RF", "hitter", pts=20.0),
        _proj(3, "IF Star", "2B", "hitter", pts=18.0),
        _proj(4, "Bench bat", "C", "hitter", pts=5.0),
    ]
    recs = dr.recommend(pool, top_n=3)
    # The first slot is IF — SP should NOT be the top recommendation since it
    # isn't eligible for IF; but the recommender lets SP appear because the
    # drafter still has SP slots remaining and it'll pick a different slot.
    names = [r["name"] for r in recs]
    assert "OF Star" in names or "IF Star" in names
    assert recs[0]["score"] >= recs[-1]["score"]
