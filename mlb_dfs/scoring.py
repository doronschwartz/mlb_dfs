"""Scoring rules ported verbatim from the spreadsheet's 'Scoring Key' tab."""

from __future__ import annotations

from dataclasses import dataclass

HITTER_POINTS = {
    "single": 3.0,
    "double": 5.0,
    "triple": 8.0,
    "homeRun": 10.0,
    "run": 2.0,
    "rbi": 2.0,
    "baseOnBalls": 2.0,
    "hitByPitch": 2.0,
    "stolenBase": 3.0,
    "groundIntoDoublePlay": -1.5,
    "strikeOut": -1.0,
}

PITCHER_POINTS = {
    "out": 0.75,
    "strikeOut": 1.5,
    "qualityStart": 4.0,
    "completeGame": 2.5,
    "shutout": 2.5,
    "noHitter": 5.0,
    "earnedRun": -2.0,
    "hitAllowed": -0.6,
    "hitBatsman": -0.6,
    "walkIssued": -0.6,
}


@dataclass(frozen=True)
class HitterLine:
    """A hitter's stat line for one game (or one season window)."""
    singles: int = 0
    doubles: int = 0
    triples: int = 0
    home_runs: int = 0
    runs: int = 0
    rbi: int = 0
    walks: int = 0
    hbp: int = 0
    stolen_bases: int = 0
    gidp: int = 0
    strikeouts: int = 0

    @classmethod
    def from_mlb_stats(cls, hitting: dict) -> "HitterLine":
        """Build from MLB Stats API `stats.hitting` block (boxscore or season)."""
        h = int(hitting.get("hits", 0) or 0)
        d = int(hitting.get("doubles", 0) or 0)
        t = int(hitting.get("triples", 0) or 0)
        hr = int(hitting.get("homeRuns", 0) or 0)
        return cls(
            singles=max(h - d - t - hr, 0),
            doubles=d,
            triples=t,
            home_runs=hr,
            runs=int(hitting.get("runs", 0) or 0),
            rbi=int(hitting.get("rbi", 0) or 0),
            walks=int(hitting.get("baseOnBalls", 0) or 0),
            hbp=int(hitting.get("hitByPitch", 0) or 0),
            stolen_bases=int(hitting.get("stolenBases", 0) or 0),
            gidp=int(hitting.get("groundIntoDoublePlay", 0) or 0),
            strikeouts=int(hitting.get("strikeOuts", 0) or 0),
        )

    def points(self) -> float:
        p = HITTER_POINTS
        return (
            self.singles * p["single"]
            + self.doubles * p["double"]
            + self.triples * p["triple"]
            + self.home_runs * p["homeRun"]
            + self.runs * p["run"]
            + self.rbi * p["rbi"]
            + self.walks * p["baseOnBalls"]
            + self.hbp * p["hitByPitch"]
            + self.stolen_bases * p["stolenBase"]
            + self.gidp * p["groundIntoDoublePlay"]
            + self.strikeouts * p["strikeOut"]
        )


@dataclass(frozen=True)
class PitcherLine:
    """A starting pitcher's line for one game."""
    outs: int = 0
    strikeouts: int = 0
    earned_runs: int = 0
    hits_allowed: int = 0
    walks_issued: int = 0
    hit_batsmen: int = 0
    complete_game: bool = False
    shutout: bool = False
    no_hitter: bool = False

    @classmethod
    def from_mlb_stats(cls, pitching: dict) -> "PitcherLine":
        ip = pitching.get("inningsPitched", "0.0")
        outs = _ip_to_outs(ip)
        return cls(
            outs=outs,
            strikeouts=int(pitching.get("strikeOuts", 0) or 0),
            earned_runs=int(pitching.get("earnedRuns", 0) or 0),
            hits_allowed=int(pitching.get("hits", 0) or 0),
            walks_issued=int(pitching.get("baseOnBalls", 0) or 0),
            hit_batsmen=int(pitching.get("hitBatsmen", 0) or 0),
            complete_game=bool(pitching.get("completeGames", 0) or 0),
            shutout=bool(pitching.get("shutouts", 0) or 0),
            no_hitter=bool(pitching.get("noHitters", 0) or 0),
        )

    def is_quality_start(self) -> bool:
        # MLB definition: 6+ IP and <=3 ER, only awarded to a starter.
        return self.outs >= 18 and self.earned_runs <= 3

    def points(self) -> float:
        p = PITCHER_POINTS
        total = (
            self.outs * p["out"]
            + self.strikeouts * p["strikeOut"]
            + self.earned_runs * p["earnedRun"]
            + self.hits_allowed * p["hitAllowed"]
            + self.walks_issued * p["walkIssued"]
            + self.hit_batsmen * p["hitBatsman"]
        )
        if self.is_quality_start():
            total += p["qualityStart"]
        if self.complete_game:
            total += p["completeGame"]
        if self.shutout:
            total += p["shutout"]
        if self.no_hitter:
            total += p["noHitter"]
        return total


def _ip_to_outs(ip: str | float | int) -> int:
    """MLB stat API reports IP like '6.2' meaning 6 innings + 2 outs."""
    if ip is None:
        return 0
    s = str(ip)
    if "." in s:
        whole, frac = s.split(".", 1)
        return int(whole) * 3 + int(frac[:1] or 0)
    return int(s) * 3
