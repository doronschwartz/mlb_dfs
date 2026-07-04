"""Trade-Deadline Draft — a prediction game for the MLB trade deadline.

League-mates snake-draft players they think will be TRADED before the
deadline, each with a predicted destination team. Scoring (auto, from the
MLB transactions API):

    +1.0  the player is traded (any MLB team → MLB team)
    +1.0  the destination team matches the drafter's prediction
    +0.5  the player was ever an All-Star            (pays only if traded)
    +0.5  the player ever finished top-3 in MVP/CY   (pays only if traded)

Bonus flags ride on the candidate pool (research-compiled, editable JSON at
data/deadline_candidates.json); trades are detected live from
/api/v1/transactions (typeCode TR), matched by MLB player id.
"""
from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
import urllib.request
from datetime import date as Date

DATA_DIR = os.environ.get(
    "MLB_DFS_DRAFT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "drafts"),
)
DRAFT_PATH = os.path.join(os.path.dirname(DATA_DIR), "deadline_draft.json")
CANDIDATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "deadline_candidates.json",
)

_TX_CACHE: dict = {"at": 0.0, "trades": []}
_TX_TTL = 900  # 15 min


def norm(n: str) -> str:
    d = unicodedata.normalize("NFD", n or "")
    a = "".join(c for c in d if not unicodedata.combining(c)).lower()
    a = a.replace(".", "").replace("'", "").replace(",", "")
    return " ".join(t for t in a.split() if t not in ("jr", "sr", "ii", "iii", "iv"))


def load_candidates() -> dict:
    try:
        return json.load(open(CANDIDATES_PATH))
    except Exception:
        return {"as_of": None, "candidates": []}


def load_draft() -> dict | None:
    try:
        return json.load(open(DRAFT_PATH))
    except Exception:
        return None


def save_draft(dr: dict) -> None:
    os.makedirs(os.path.dirname(DRAFT_PATH), exist_ok=True)
    tmp = DRAFT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dr, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DRAFT_PATH)


def new_draft(drafters: list[str], rounds: int = 6, deadline: str = "2026-08-03") -> dict:
    if len(drafters) < 2:
        raise ValueError("need at least 2 drafters")
    dr = {
        "created": Date.today().isoformat(),
        "deadline": deadline,
        "drafters": drafters,
        "rounds": max(1, min(int(rounds), 20)),
        "picks": [],
    }
    save_draft(dr)
    return dr


def on_the_clock(dr: dict) -> str | None:
    n = len(dr["picks"])
    D = len(dr["drafters"])
    if n >= D * dr["rounds"]:
        return None
    rnd, idx = divmod(n, D)
    order = dr["drafters"] if rnd % 2 == 0 else list(reversed(dr["drafters"]))
    return order[idx]


def _resolve_player_id(name: str) -> int | None:
    try:
        q = urllib.request.quote(name)
    except AttributeError:
        from urllib.parse import quote as q2
        q = q2(name)
    try:
        d = json.load(urllib.request.urlopen(
            f"https://statsapi.mlb.com/api/v1/people/search?q={q}", timeout=15))
        ppl = d.get("people", [])
        # prefer exact normalized match
        for p in ppl:
            if norm(p.get("fullName")) == norm(name):
                return p.get("id")
        return ppl[0].get("id") if ppl else None
    except Exception:
        return None


def make_pick(dr: dict, drafter: str, player_name: str, predicted_team: str) -> dict:
    turn = on_the_clock(dr)
    if turn is None:
        raise ValueError("deadline draft is complete")
    if drafter != turn:
        raise ValueError(f"not your turn — {turn} is on the clock")
    if any(norm(p["player_name"]) == norm(player_name) for p in dr["picks"]):
        raise ValueError(f"{player_name} is already drafted")
    cands = {norm(c["name"]): c for c in load_candidates().get("candidates", [])}
    c = cands.get(norm(player_name))
    pick = {
        "pick_number": len(dr["picks"]) + 1,
        "drafter": drafter,
        "player_name": (c or {}).get("name") or player_name,
        "player_id": _resolve_player_id(player_name),
        "position": (c or {}).get("position"),
        "team": (c or {}).get("team"),
        "tier": (c or {}).get("tier"),
        "predicted_team": (predicted_team or "").upper()[:3],
        "has_allstar": bool((c or {}).get("has_allstar")),
        "has_top3_voting": bool((c or {}).get("has_top3_voting")),
    }
    dr["picks"].append(pick)
    save_draft(dr)
    return pick


def undo_pick(dr: dict, drafter: str) -> dict:
    if not dr["picks"]:
        raise ValueError("no picks to undo")
    last = dr["picks"][-1]
    if last["drafter"] != drafter:
        raise ValueError(f"last pick was by {last['drafter']}, not you")
    dr["picks"].pop()
    save_draft(dr)
    return last


def mlb_trades(start: str, end: str) -> list[dict]:
    """Trades (typeCode TR) between MLB teams in the window, cached 15 min.
    Returns [{person_id, person_name, from, to, to_abbr, date, desc}]."""
    now = time.time()
    if now - _TX_CACHE["at"] < _TX_TTL and _TX_CACHE["trades"]:
        return _TX_CACHE["trades"]
    from .projections import _TEAM_ABBR
    try:
        d = json.load(urllib.request.urlopen(
            f"https://statsapi.mlb.com/api/v1/transactions?startDate={start}&endDate={end}",
            timeout=30))
    except Exception as e:
        logging.warning("transactions fetch failed: %s", e)
        return _TX_CACHE["trades"]
    out = []
    for t in d.get("transactions", []):
        if t.get("typeCode") != "TR":
            continue
        person = t.get("person") or {}
        to_team = t.get("toTeam") or {}
        from_team = t.get("fromTeam") or {}
        to_id = to_team.get("id")
        # MLB clubs only (transactions feed includes partner leagues)
        if to_id not in _TEAM_ABBR or (from_team.get("id") not in _TEAM_ABBR):
            continue
        if not person.get("id"):
            continue
        out.append({
            "person_id": person["id"],
            "person_name": person.get("fullName"),
            "from": from_team.get("name"),
            "to": to_team.get("name"),
            "to_abbr": _TEAM_ABBR.get(to_id, ""),
            "date": t.get("date"),
            "desc": (t.get("description") or "")[:200],
        })
    _TX_CACHE["at"] = now
    _TX_CACHE["trades"] = out
    return out


def score(dr: dict) -> dict:
    """Live scores. Bonus points pay ONLY when the player is actually traded
    (otherwise the optimal strategy is drafting famous names regardless of
    trade likelihood — not the game)."""
    trades = mlb_trades(dr["created"], dr["deadline"])
    by_pid = {}
    by_name = {}
    for t in trades:
        by_pid.setdefault(t["person_id"], t)
        by_name.setdefault(norm(t["person_name"] or ""), t)
    totals: dict[str, float] = {d: 0.0 for d in dr["drafters"]}
    detail = []
    for p in dr["picks"]:
        t = by_pid.get(p.get("player_id")) or by_name.get(norm(p["player_name"]))
        pts = 0.0
        hit_team = False
        if t:
            pts += 1.0
            if t["to_abbr"] and t["to_abbr"] == p.get("predicted_team"):
                pts += 1.0
                hit_team = True
            if p.get("has_allstar"):
                pts += 0.5
            if p.get("has_top3_voting"):
                pts += 0.5
        totals[p["drafter"]] = totals.get(p["drafter"], 0.0) + pts
        detail.append({**p, "traded": bool(t),
                       "traded_to": (t or {}).get("to_abbr"),
                       "trade_date": (t or {}).get("date"),
                       "hit_team": hit_team, "points": pts})
    return {"totals": totals, "picks": detail,
            "trades_seen": len(trades)}
