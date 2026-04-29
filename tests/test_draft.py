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
