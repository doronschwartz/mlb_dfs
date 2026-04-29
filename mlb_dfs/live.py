"""Live scoring of a saved draft against today's box scores."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from . import mlb_api
from .draft import Draft, Pick
from .scoring import HitterLine, PitcherLine


@dataclass
class PlayerScore:
    player_id: int
    name: str
    role: str
    points: float
    raw: dict = field(default_factory=dict)
    game_state: str = ""


@dataclass
class DrafterScore:
    drafter: str
    total: float
    full_total: float  # includes BN
    rank: int = 0
    picks: list[tuple[Pick, PlayerScore | None]] = field(default_factory=list)


def score_draft(draft: Draft, *, on_date: Date | None = None) -> list[DrafterScore]:
    on_date = on_date or Date.fromisoformat(draft.date)
    box_index = _index_boxscores(on_date)

    drafter_scores: dict[str, DrafterScore] = {
        d: DrafterScore(drafter=d, total=0.0, full_total=0.0) for d in draft.drafters
    }

    for pick in draft.picks:
        line = box_index.get(pick.player_id)
        ps: PlayerScore | None = None
        if line is not None:
            ps = _score_player(pick, line)

        ds = drafter_scores[pick.drafter]
        ds.picks.append((pick, ps))
        if ps is not None:
            ds.full_total += ps.points
            if pick.slot != "BN":
                ds.total += ps.points

    ranked = sorted(drafter_scores.values(), key=lambda d: d.total, reverse=True)
    for i, d in enumerate(ranked, start=1):
        d.rank = i
    return ranked


def _index_boxscores(d: Date) -> dict[int, dict]:
    """player_id -> {'role': 'hitter'|'pitcher', 'stats': dict, 'state': str}"""
    out: dict[int, dict] = {}
    for game in mlb_api.schedule(d):
        pk = game.get("gamePk")
        state = (game.get("status") or {}).get("detailedState", "")
        try:
            box = mlb_api.boxscore(pk)
        except Exception:
            continue
        for person, stats in mlb_api.iter_boxscore_batters(box):
            pid = person.get("id")
            if pid:
                out[pid] = {"role": "hitter", "stats": stats, "state": state}
        for person, stats in mlb_api.iter_boxscore_pitchers(box):
            pid = person.get("id")
            if pid and person.get("isStarter"):
                # starter line goes under "pitcher"; if a player also batted, the
                # pitcher entry takes precedence for SP-slotted players.
                out[pid] = {"role": "pitcher", "stats": stats, "state": state}
    return out


def _score_player(pick: Pick, line: dict) -> PlayerScore:
    if pick.slot == "SP" or line["role"] == "pitcher":
        pl = PitcherLine.from_mlb_stats(line["stats"])
        pts = pl.points()
        raw = {
            "outs": pl.outs, "K": pl.strikeouts, "ER": pl.earned_runs,
            "H": pl.hits_allowed, "BB": pl.walks_issued, "HBP": pl.hit_batsmen,
            "QS": pl.is_quality_start(), "CG": pl.complete_game,
            "SHO": pl.shutout, "NH": pl.no_hitter,
        }
    else:
        hl = HitterLine.from_mlb_stats(line["stats"])
        pts = hl.points()
        raw = {
            "1B": hl.singles, "2B": hl.doubles, "3B": hl.triples, "HR": hl.home_runs,
            "R": hl.runs, "RBI": hl.rbi, "BB": hl.walks, "HBP": hl.hbp,
            "SB": hl.stolen_bases, "GIDP": hl.gidp, "K": hl.strikeouts,
        }
    return PlayerScore(
        player_id=pick.player_id,
        name=pick.name,
        role=line["role"],
        points=round(pts, 2),
        raw=raw,
        game_state=line.get("state", ""),
    )
