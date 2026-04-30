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

    def on_the_clock(self) -> tuple[str, str] | None:
        """Returns (drafter, slot) for the next pick, or None if complete."""
        if self.is_complete():
            return None
        n = len(self.picks)
        round_idx = n // len(self.drafters)
        idx_in_round = n % len(self.drafters)
        order = self.drafters if round_idx % 2 == 0 else list(reversed(self.drafters))
        drafter = order[idx_in_round]

        # Slot for that drafter is the next empty slot in their roster.
        taken_slots = [p.slot for p in self.picks if p.drafter == drafter]
        for s in SLOTS:
            if taken_slots.count(s) < SLOTS.count(s):
                return drafter, s
        return drafter, "BN"

    def picked_ids(self) -> set[int]:
        return {p.player_id for p in self.picks}

    def make_pick(self, slot: str, projection: Projection) -> Pick:
        info = self.on_the_clock()
        if info is None:
            raise RuntimeError("draft already complete")
        drafter, expected_slot = info
        if projection.player_id in self.picked_ids():
            raise ValueError(f"{projection.name} is already drafted")
        if not _slot_eligible(slot, projection):
            raise ValueError(f"{projection.name} ({projection.position}) is not eligible for {slot}")

        pick = Pick(
            drafter=drafter,
            slot=slot,
            player_id=projection.player_id,
            name=projection.name,
            position=projection.position,
            role=projection.role,
            projected_points=projection.projected_points,
            pick_number=len(self.picks) + 1,
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

        scored: list[dict] = []
        for proj in avail:
            eligible_slots = [s for s in remaining if _slot_eligible(s, proj)]
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
                "recommend_slot": best_slot,
                "score": round(score, 2),
                "notes": list(proj.notes),
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
    _ensure_dir()
    path = os.path.join(DRAFT_DIR, f"{draft.draft_id}.json")
    with open(path, "w") as f:
        json.dump(draft.to_dict(), f, indent=2)
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
