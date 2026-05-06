"""Draft state, snake order, and the smart pick recommender."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from typing import Iterable

from .projections import Projection

# Roster slots, in spreadsheet order. 10 picks per drafter, 3 drafters.
SLOTS = ["IF", "IF", "IF", "OF", "OF", "OF", "UTIL", "BN", "SP", "SP"]
PICKS_PER_DRAFTER = len(SLOTS)


@dataclass
class Pick:
    drafter: str
    slot: str
    player_id: int
    name: str
    position: str | None
    role: str  # "hitter" or "pitcher"
    projected_points: float
    pick_number: int  # 1-indexed across the entire draft
    # Specific gamePk this player is drafted for. Required when the player's
    # team has more than one slate game on the date (doubleheader). Auto-
    # resolved to the only slate game otherwise. None on legacy picks.
    game_pk: int | None = None
    # True for SP picks taken out-of-order via drafter_override — these don't
    # advance the snake position, so the on-the-clock drafter still gets their
    # natural turn. Only the lone-SP-needer can do this (see can_pick_sp_out_of_order).
    out_of_order: bool = False


@dataclass
class Draft:
    draft_id: str
    date: str  # ISO date
    drafters: list[str]  # snake order, e.g. ["Stock","JL","Meech"]
    picks: list[Pick] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # When non-empty, only players from these gamePks are eligible for the draft.
    # Empty = whole slate (back-compat).
    game_pks: list[int] = field(default_factory=list)

    # ---- core mechanics --------------------------------------------------

    def total_picks(self) -> int:
        return PICKS_PER_DRAFTER * len(self.drafters)

    def is_complete(self) -> bool:
        return len(self.picks) >= self.total_picks()

    def can_pick_sp_out_of_order(self, drafter: str) -> bool:
        """True iff `drafter` is the only one with SP slots still open. They
        can grab an SP whenever, since snake-order has nothing to gate."""
        if drafter not in self.drafters:
            return False
        cap = SLOTS.count("SP")
        self_needs = False
        for d in self.drafters:
            taken_sp = sum(1 for p in self.picks if p.drafter == d and p.slot == "SP")
            if taken_sp < cap:
                if d == drafter:
                    self_needs = True
                else:
                    return False
        return self_needs

    def on_the_clock(self) -> tuple[str, str] | None:
        """Returns (drafter, slot) for the next pick, or None if complete."""
        if self.is_complete():
            return None
        # Snake position counts only in-order picks. Out-of-order SP picks (the
        # lone-SP-needer convenience) don't steal anyone's turn — the drafter
        # who was on the clock when the OOO pick happened is still on the clock.
        n = sum(1 for p in self.picks if not getattr(p, "out_of_order", False))
        round_idx = n // len(self.drafters)
        idx_in_round = n % len(self.drafters)
        order = self.drafters if round_idx % 2 == 0 else list(reversed(self.drafters))

        # If the natural drafter has already filled every slot (e.g. they took
        # one out-of-order then completed their snake picks earlier), advance
        # to the next drafter in the round who still has an open slot.
        for skip in range(len(self.drafters)):
            d = order[(idx_in_round + skip) % len(self.drafters)]
            taken_slots = [p.slot for p in self.picks if p.drafter == d]
            for s in SLOTS:
                if taken_slots.count(s) < SLOTS.count(s):
                    return d, s
        return order[idx_in_round], "BN"

    def picked_ids(self) -> set[int]:
        return {p.player_id for p in self.picks}

    def move_pick(self, pick_number: int, new_slot: str) -> tuple[Pick, Pick | None]:
        """Move an existing pick to a different slot.

        If the destination slot is already at capacity for this drafter, the
        moved pick swaps with one of the picks currently in that slot
        (preferring a swap partner who is themselves eligible for the moved
        pick's old slot, so both ends remain valid).

        Returns (moved_pick, displaced_pick_or_None). Use this to slide a
        bench/UTIL player into a starting slot when the original starter is
        OOL — the displaced starter ends up on the bench/UTIL where you can
        Replace them with a fresh pickup.
        """
        idx = next((i for i, p in enumerate(self.picks) if p.pick_number == pick_number), None)
        if idx is None:
            raise ValueError(f"no pick #{pick_number}")
        moving = self.picks[idx]
        if new_slot == moving.slot:
            return moving, None
        if new_slot not in {"IF", "OF", "UTIL", "BN", "SP"}:
            raise ValueError(f"unknown slot {new_slot}")
        if not _slot_eligible(new_slot, moving):
            raise ValueError(f"{moving.name} ({moving.position}) is not eligible for {new_slot}")

        same_drafter_in_dest = [
            (i, p) for i, p in enumerate(self.picks)
            if p.drafter == moving.drafter and p.slot == new_slot and p.pick_number != pick_number
        ]
        cap = SLOTS.count(new_slot)
        if len(same_drafter_in_dest) < cap:
            self.picks[idx] = _replace_slot(moving, new_slot)
            return self.picks[idx], None

        # Destination is full — find a swap partner. Prefer one whose own
        # original slot the moving pick can fill back into (so we don't leave
        # the partner stranded). Falls back to any partner if needed; if even
        # the fallback can't be made eligible, error out.
        partner = next(
            ((i, p) for i, p in same_drafter_in_dest if _slot_eligible(moving.slot, p)),
            None,
        )
        if partner is None:
            partner = same_drafter_in_dest[0]  # forced swap; will validate below

        partner_idx, partner_pick = partner
        if not _slot_eligible(moving.slot, partner_pick):
            raise ValueError(
                f"can't swap: {partner_pick.name} ({partner_pick.position}) "
                f"isn't eligible for {moving.slot}"
            )
        self.picks[idx] = _replace_slot(moving, new_slot)
        self.picks[partner_idx] = _replace_slot(partner_pick, moving.slot)
        return self.picks[idx], self.picks[partner_idx]

    def eligible_target_slots(self, pick_number: int) -> list[str]:
        """Slots this pick could legally be moved into (excluding its current
        slot). Used by the UI to populate the Move dropdown."""
        idx = next((i for i, p in enumerate(self.picks) if p.pick_number == pick_number), None)
        if idx is None:
            return []
        p = self.picks[idx]
        out = []
        for s in ["IF", "OF", "UTIL", "BN", "SP"]:
            if s == p.slot:
                continue
            if not _slot_eligible(s, p):
                continue
            out.append(s)
        return out

    def replace_pick(
        self, pick_number: int, projection: Projection, *,
        game_pk: int | None = None,
    ) -> Pick:
        """Swap the player at `pick_number` for `projection`. Slot stays the same.

        `game_pk` is the specific gamePk the new player counts in (required if
        their team has a doubleheader in the slate; auto-resolved upstream).
        """
        idx = next((i for i, p in enumerate(self.picks) if p.pick_number == pick_number), None)
        if idx is None:
            raise ValueError(f"no pick #{pick_number}")
        old = self.picks[idx]
        if projection.player_id == old.player_id:
            raise ValueError("new player is the same as the old one")
        if projection.player_id in self.picked_ids():
            raise ValueError(f"{projection.name} is already drafted")
        if not _slot_eligible(old.slot, projection):
            raise ValueError(f"{projection.name} ({projection.position}) is not eligible for {old.slot}")
        self.picks[idx] = Pick(
            drafter=old.drafter,
            slot=old.slot,
            player_id=projection.player_id,
            name=projection.name,
            position=projection.position,
            role=projection.role,
            projected_points=projection.projected_points,
            pick_number=old.pick_number,
            game_pk=game_pk,
            out_of_order=getattr(old, "out_of_order", False),
        )
        return self.picks[idx]

    def make_pick(self, slot: str, projection: Projection, *, game_pk: int | None = None, drafter_override: str | None = None) -> Pick:
        info = self.on_the_clock()
        if info is None:
            raise RuntimeError("draft already complete")
        drafter, _suggested_slot = info
        # Out-of-order SP pick — allowed only when the requesting drafter is
        # the LAST one with an open SP slot (everyone else is done with SPs).
        # Marked out_of_order=True so the snake position doesn't advance and
        # the drafter who was on the clock keeps their natural turn.
        is_ooo = False
        if drafter_override and drafter_override != drafter:
            if slot != "SP":
                raise ValueError("out-of-order pick allowed only for SP slot")
            if not self.can_pick_sp_out_of_order(drafter_override):
                raise ValueError(
                    f"out-of-order SP pick: {drafter_override} isn't the last drafter with an open SP slot"
                )
            drafter = drafter_override
            is_ooo = True
        if projection.player_id in self.picked_ids():
            raise ValueError(f"{projection.name} is already drafted")
        if not _slot_eligible(slot, projection):
            raise ValueError(f"{projection.name} ({projection.position}) is not eligible for {slot}")

        # Capacity check: drafter must have at least one remaining slot of this type.
        taken_count = sum(1 for p in self.picks if p.drafter == drafter and p.slot == slot)
        slot_cap = SLOTS.count(slot)
        if taken_count >= slot_cap:
            raise ValueError(f"{drafter} has no remaining {slot} slot(s)")

        pick = Pick(
            drafter=drafter,
            slot=slot,
            player_id=projection.player_id,
            name=projection.name,
            position=projection.position,
            role=projection.role,
            projected_points=projection.projected_points,
            pick_number=len(self.picks) + 1,
            game_pk=game_pk,
            out_of_order=is_ooo,
        )
        self.picks.append(pick)
        return pick

    def roster_for(self, drafter: str) -> list[Pick]:
        return [p for p in self.picks if p.drafter == drafter]

    def remaining_slots(self, drafter: str) -> list[str]:
        taken = [p.slot for p in self.picks if p.drafter == drafter]
        out: list[str] = []
        for s in SLOTS:
            if taken.count(s) < SLOTS.count(s):
                out.append(s)
        return out

    # ---- recommender -----------------------------------------------------

    def recommend(self, projections: list[Projection], top_n: int = 8) -> list[dict]:
        """Rank the best available picks for whoever is on the clock.

        Considers:
          - position eligibility for the next *required* slot
          - scarcity: penalize spending elite picks on slots you can fill later
          - opportunity cost vs the next time this drafter picks
        """
        info = self.on_the_clock()
        if info is None:
            return []
        drafter, next_slot = info

        already = self.picked_ids()
        avail = [p for p in projections if p.player_id not in already]

        remaining = self.remaining_slots(drafter)
        # Compute a need-weight per role
        need_hitter = sum(1 for s in remaining if s in {"IF", "OF", "UTIL", "BN"})
        need_sp = sum(1 for s in remaining if s == "SP")

        remaining_unique = list(dict.fromkeys(remaining))  # dedupe, preserve order

        scored: list[dict] = []
        for proj in avail:
            eligible_slots = [s for s in remaining_unique if _slot_eligible(s, proj)]
            if not eligible_slots:
                continue

            # Scarcity bonus: SPs are scarce relative to hitters, so weight by need ratio.
            if proj.role == "pitcher":
                scarcity = (need_sp / max(len(remaining), 1)) * 1.15
            else:
                scarcity = (need_hitter / max(len(remaining), 1))

            score = proj.projected_points * (0.85 + 0.30 * scarcity)
            best_slot = sorted(eligible_slots, key=lambda s: _slot_priority(s))[0]

            scored.append({
                "player_id": proj.player_id,
                "name": proj.name,
                "position": proj.position,
                "role": proj.role,
                "projected_points": proj.projected_points,
                "recommend_slot": best_slot,        # default suggestion
                "eligible_slots": eligible_slots,   # all open slots the drafter can use
                "score": round(score, 2),
                "notes": list(proj.notes),
                "components": dict(proj.components),
            })

        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_n]

    # ---- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "draft_id": self.draft_id,
            "date": self.date,
            "drafters": list(self.drafters),
            "picks": [asdict(p) for p in self.picks],
            "created_at": self.created_at,
            "game_pks": list(self.game_pks),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Draft":
        return cls(
            draft_id=data["draft_id"],
            date=data["date"],
            drafters=list(data["drafters"]),
            picks=[Pick(**p) for p in data.get("picks", [])],
            created_at=data.get("created_at", time.time()),
            game_pks=list(data.get("game_pks", [])),
        )


def _replace_slot(pick: Pick, new_slot: str) -> Pick:
    return Pick(
        drafter=pick.drafter,
        slot=new_slot,
        player_id=pick.player_id,
        name=pick.name,
        position=pick.position,
        role=pick.role,
        projected_points=pick.projected_points,
        pick_number=pick.pick_number,
        game_pk=pick.game_pk,
        out_of_order=getattr(pick, "out_of_order", False),
    )


def _slot_priority(slot: str) -> int:
    return {"SP": 0, "IF": 1, "OF": 2, "UTIL": 3, "BN": 4}.get(slot, 5)


def _slot_eligible(slot: str, projection: Projection) -> bool:
    pos = (projection.position or "").upper()
    role = projection.role
    if slot == "SP":
        return role == "pitcher"
    if role == "pitcher":
        return False  # pitchers only fill SP
    if slot == "IF":
        return pos in {"1B", "2B", "3B", "SS", "C", "IF"}
    if slot == "OF":
        return pos in {"LF", "CF", "RF", "OF"}
    if slot in {"UTIL", "BN"}:
        return True
    return False


# ---- on-disk store ------------------------------------------------------------

DRAFT_DIR = os.environ.get(
    "MLB_DFS_DRAFT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "drafts"),
)


def _ensure_dir() -> None:
    os.makedirs(DRAFT_DIR, exist_ok=True)


def save_draft(draft: Draft) -> str:
    """Atomic, durable write to disk.

    Writes to a temp file in the same directory, fsyncs, then renames over
    the destination. Fly volumes get unmounted on every deploy; without an
    explicit fsync, recent writes may sit in the page cache and be lost when
    the volume goes away.
    """
    _ensure_dir()
    path = os.path.join(DRAFT_DIR, f"{draft.draft_id}.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(draft.to_dict(), f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Sync the directory entry so the rename itself is durable.
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return path


def load_draft(draft_id: str) -> Draft:
    path = os.path.join(DRAFT_DIR, f"{draft_id}.json")
    with open(path) as f:
        return Draft.from_dict(json.load(f))


def list_drafts() -> list[str]:
    _ensure_dir()
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(DRAFT_DIR)
        if f.endswith(".json")
    )


def delete_draft(draft_id: str) -> bool:
    """Delete the on-disk file. Returns True if removed, False if it didn't exist."""
    path = os.path.join(DRAFT_DIR, f"{draft_id}.json")
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def new_draft(
    date: Date,
    drafters: Iterable[str],
    *,
    game_pks: Iterable[int] | None = None,
) -> Draft:
    drafters = list(drafters)
    if len(drafters) < 2:
        raise ValueError("need at least 2 drafters")
    return Draft(
        draft_id=date.isoformat(),
        date=date.isoformat(),
        drafters=drafters,
        game_pks=sorted(set(game_pks or [])),
    )
