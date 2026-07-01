"""Property-based simulation tests for the draft snake + out-of-order logic.

Why this exists: three real bugs in this logic were found BY LEAGUE USERS in
two days (2026-06-23..25) — the SP-jumper UI unlock, the free-for-all
mis-model, and next_ooo_drafter ignoring the snake's round reversal — because
nothing here was covered by tests. These sims drive thousands of randomized
drafts through every pick path and assert the invariants that were violated.

Run modes:
    pytest tests/test_draft_simulation.py            # quick (300 sims)
    DRAFT_SIM_N=10000 pytest tests/test_draft_simulation.py  # the long soak
"""
import os
import random
from datetime import date as Date
from types import SimpleNamespace

import pytest

from mlb_dfs import draft as dm

SIM_N = int(os.environ.get("DRAFT_SIM_N", "300"))

_PID = [10_000_000]  # fake ids far above any real MLB id / override table


def player(role: str, slot: str | None = None):
    """Fake projection-like object. Position matches the target slot so
    _slot_eligible passes; non-DH positions early-return from
    resolve_position so sims never touch the network."""
    _PID[0] += 1
    if role == "pitcher":
        pos = "P"
    elif slot == "IF":
        pos = random.choice(["SS", "1B", "2B", "3B", "C"])
    elif slot == "OF":
        pos = random.choice(["LF", "CF", "RF"])
    else:  # UTIL / BN take any hitter
        pos = random.choice(["SS", "1B", "2B", "3B", "C", "LF", "CF", "RF"])
    return SimpleNamespace(
        player_id=_PID[0],
        name=f"sim{_PID[0]}",
        position=pos,
        role=role,
        projected_points=round(random.uniform(1, 20), 2),
    )


def eligible_roles_for(slot: str):
    return ["pitcher"] if slot == "SP" else ["hitter"]


def oracle_next_ooo(dr) -> str | None:
    """Independent reimplementation of the intended rule: when the snake is
    only waiting on the lone SP-needer's pitchers, the next drafter in TRUE
    snake order (round reversal respected) with an open non-SP slot may pick.
    This is the oracle that would have caught the 6/25 round-reversal bug."""
    if not dr.non_sp_free_for_all():
        return None
    lone = None
    for d in dr.drafters:
        sp_taken = sum(1 for p in dr.picks if p.drafter == d and p.slot == "SP")
        if sp_taken < dm.SLOTS.count("SP"):
            lone = d
            break
    D = len(dr.drafters)
    n_in = sum(1 for p in dr.picks if not getattr(p, "out_of_order", False))
    for pos in range(n_in, D * dm.PICKS_PER_DRAFTER):
        r, i = divmod(pos, D)
        order = dr.drafters if r % 2 == 0 else list(reversed(dr.drafters))
        d = order[i]
        if d == lone:
            continue
        if any(s != "SP" for s in dr.remaining_slots(d)):
            return d
    return None


def check_invariants(dr):
    # slot caps never exceeded
    for d in dr.drafters:
        taken = [p.slot for p in dr.picks if p.drafter == d]
        for s in set(dm.SLOTS):
            assert taken.count(s) <= dm.SLOTS.count(s), f"{d} over cap on {s}"
    # no duplicate players (role-aware)
    keys = [(p.player_id, p.role) for p in dr.picks]
    assert len(keys) == len(set(keys)), "duplicate player drafted"
    # on_the_clock sanity
    info = dr.on_the_clock()
    if dr.is_complete():
        assert info is None
    else:
        assert info is not None, "incomplete draft but nobody on the clock"
        who, slot = info
        assert slot in dr.remaining_slots(who), "on-clock suggested a filled slot"
    # SP-jump: allowed iff exactly one drafter still needs SP
    sp_needers = [
        d for d in dr.drafters
        if sum(1 for p in dr.picks if p.drafter == d and p.slot == "SP") < dm.SLOTS.count("SP")
    ]
    for d in dr.drafters:
        expected = len(sp_needers) == 1 and sp_needers[0] == d
        assert dr.can_pick_sp_out_of_order(d) == expected, f"SP-jump wrong for {d}"
    # next_ooo matches the true-snake oracle (the 6/25 bug class)
    assert dr.next_ooo_drafter() == oracle_next_ooo(dr), "next_ooo diverges from snake oracle"
    # a full-roster drafter can never OOO anything (the 6/24 bug class)
    for d in dr.drafters:
        if not dr.remaining_slots(d):
            for s in ("SP", "IF", "OF", "UTIL", "BN"):
                assert not dr.can_pick_out_of_order(d, s), f"done drafter {d} can OOO {s}"


def run_sim(seed: int):
    rng = random.Random(seed)
    random.seed(seed * 7 + 1)  # player() uses module random
    n_drafters = rng.randint(2, 6)
    drafters = [f"D{i}" for i in range(n_drafters)]
    dr = dm.new_draft(Date(2026, 6, 30), drafters)
    steps = 0
    while not dr.is_complete():
        steps += 1
        assert steps <= dr.total_picks() * 3, "draft failed to converge"
        check_invariants(dr)
        who, slot = dr.on_the_clock()
        action = rng.random()
        # 1) sometimes try the SP-jump (legal only for the lone SP-needer)
        if action < 0.15:
            cand = rng.choice(drafters)
            legal = dr.can_pick_sp_out_of_order(cand) and cand != who and "SP" in dr.remaining_slots(cand)
            if legal:
                dr.make_pick("SP", player("pitcher", "SP"), drafter_override=cand)
                continue
            elif cand != who:
                with pytest.raises((ValueError, RuntimeError)):
                    dr.make_pick("SP", player("pitcher", "SP"), drafter_override=cand)
                # fall through to a legal pick so the sim advances
        # 2) sometimes try a non-SP OOO (legal only for next_ooo / hitter_free)
        if 0.15 <= action < 0.30:
            cand = rng.choice(drafters)
            open_non_sp = [s for s in dr.remaining_slots(cand) if s != "SP"]
            if cand != who and open_non_sp:
                s = rng.choice(open_non_sp)
                if dr.can_pick_out_of_order(cand, s):
                    dr.make_pick(s, player(eligible_roles_for(s)[0], s), drafter_override=cand)
                    continue
                else:
                    with pytest.raises((ValueError, RuntimeError)):
                        dr.make_pick(s, player(eligible_roles_for(s)[0], s), drafter_override=cand)
        # 3) the normal on-clock pick — any open slot, not just the suggestion
        s = rng.choice(dr.remaining_slots(who))
        dr.make_pick(s, player(eligible_roles_for(s)[0], s))
    # terminal state
    check_invariants(dr)
    assert len(dr.picks) == dr.total_picks()
    for d in drafters:
        assert dr.remaining_slots(d) == [], f"{d} finished with open slots"


@pytest.mark.parametrize("seed", range(SIM_N))
def test_randomized_draft_simulation(seed):
    run_sim(seed)


# ---- pinned regressions for the three user-found bugs -----------------------

def _mk(drafters):
    return dm.new_draft(Date(2026, 6, 25), drafters)



def test_regression_20260625_round_reversal():
    """Snake Meech,Stock,JL. Reconstructs the 6/25 state: JL owes only SPs and
    his two natural turns span the round-9 reversal; Stock (not Meech) is the
    true next non-SP drafter. The old offset math returned Meech."""
    drafters = ["Meech", "Stock", "JL"]
    dr = _mk(drafters)
    # scripted 26 picks matching the real draft's slot sequence
    seq = [
        ("Meech", "IF"), ("Stock", "IF"), ("JL", "IF"),
        ("JL", "IF"), ("Stock", "IF"), ("Meech", "IF"),
        ("Meech", "OF"), ("Stock", "OF"), ("JL", "IF"),
        ("JL", "OF"), ("Stock", "OF"), ("Meech", "OF"),
        ("Meech", "OF"), ("Stock", "OF"), ("JL", "OF"),
        ("JL", "OF"), ("Stock", "SP"), ("Meech", "SP"),
        ("Meech", "SP"), ("Stock", "BN"), ("JL", "UTIL"),
        ("JL", "BN"), ("Stock", "UTIL"), ("Meech", "IF"),
        ("Meech", "UTIL"), ("Stock", "SP"),
    ]
    for who, slot in seq:
        on = dr.on_the_clock()[0]
        assert on == who, f"scripted sequence diverged: expected {who} on clock, got {on}"
        dr.make_pick(slot, player(eligible_roles_for(slot)[0], slot))
    assert dr.non_sp_free_for_all() is True
    assert dr.next_ooo_drafter() == "Stock", "round-reversal: Stock is next, not Meech"
    assert dr.can_pick_out_of_order("Stock", "IF") is True
    assert dr.can_pick_out_of_order("Meech", "BN") is False, "not a free-for-all: Meech waits"
    assert dr.can_pick_out_of_order("JL", "IF") is False, "SP-only drafter can't take hitters"


def test_regression_20260624_done_drafter_cannot_ooo():
    """A drafter with a full roster must not be offered any OOO pick."""
    drafters = ["A", "B"]
    dr = _mk(drafters)
    while not dr.is_complete():
        who, _ = dr.on_the_clock()
        s = dr.remaining_slots(who)[0]
        dr.make_pick(s, player(eligible_roles_for(s)[0], s))
    for d in drafters:
        for s in ("SP", "IF", "OF", "UTIL", "BN"):
            assert not dr.can_pick_out_of_order(d, s)


def test_sp_jumper_cannot_take_hitters():
    """The lone SP-needer's jump privilege is SP-only (the 6/24 UI bug's
    backend contract, pinned)."""
    dr = _mk(["A", "B"])
    # A fills everything except SPs; B fills everything.
    while not dr.is_complete():
        who, _ = dr.on_the_clock()
        rem = dr.remaining_slots(who)
        if who == "A":
            non_sp = [s for s in rem if s != "SP"]
            if not non_sp:
                break  # A has only SPs left — stop here
            s = non_sp[0]
        else:
            s = rem[0]
        dr.make_pick(s, player(eligible_roles_for(s)[0], s))
    if dr.remaining_slots("B"):
        # finish B via OOO/normal so only A's SPs remain
        while dr.remaining_slots("B"):
            who, _ = dr.on_the_clock()
            if who == "B":
                s = dr.remaining_slots("B")[0]
                dr.make_pick(s, player(eligible_roles_for(s)[0], s))
            else:
                s = dr.remaining_slots("B")[0]
                if dr.can_pick_out_of_order("B", s):
                    dr.make_pick(s, player(eligible_roles_for(s)[0], s), drafter_override="B")
                else:
                    s2 = dr.remaining_slots(who)[0]
                    dr.make_pick(s2, player(eligible_roles_for(s2)[0], s2))
    assert dr.can_pick_sp_out_of_order("A") is True
    for s in ("IF", "OF", "UTIL", "BN"):
        assert not dr.can_pick_out_of_order("A", s)
