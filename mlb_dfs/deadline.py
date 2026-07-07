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
# Candidates live NEXT TO the drafts dir — the persistent /data volume in
# prod (survives deploys, updatable via POST /api/deadline/candidates without
# a redeploy), the repo's data/ locally. The Docker image doesn't ship data/.
CANDIDATES_PATH = os.path.join(os.path.dirname(DATA_DIR), "deadline_candidates.json")


def save_candidates(payload: dict) -> int:
    cands = payload.get("candidates")
    if not isinstance(cands, list) or len(cands) < 5:
        raise ValueError("payload must contain a candidates list (5+)")
    for c in cands[:10]:
        if not c.get("name") or c.get("tier") not in ("high", "medium", "long-shot", "deep"):
            raise ValueError(f"malformed candidate: {c}")
    os.makedirs(os.path.dirname(CANDIDATES_PATH), exist_ok=True)
    tmp = CANDIDATES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, CANDIDATES_PATH)
    return len(cands)

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


def _resolve_player(name: str) -> dict | None:
    """Resolve a name to {id, fullName, position, team} via MLB search.
    Uses names= (reliable) — the q= param returns unrelated players for
    some names (observed: 'Mason Miller' → Freddie Freeman)."""
    from urllib.parse import quote
    try:
        d = json.load(urllib.request.urlopen(
            f"https://statsapi.mlb.com/api/v1/people/search?names={quote(name)}"
            f"&hydrate=currentTeam", timeout=15))
        ppl = d.get("people", [])
        best = next((p for p in ppl if norm(p.get("fullName")) == norm(name)),
                    ppl[0] if ppl else None)
        if not best:
            return None
        return {
            "id": best.get("id"),
            "fullName": best.get("fullName"),
            "position": (best.get("primaryPosition") or {}).get("abbreviation"),
            "team": (best.get("currentTeam") or {}).get("abbreviation")
                    or (best.get("currentTeam") or {}).get("name"),
        }
    except Exception:
        return None


def _resolve_player_id(name: str) -> int | None:
    p = _resolve_player(name)
    return p["id"] if p else None


# MLB awards → bonus flags for WRITE-IN picks (pool players carry researched
# flags). All-Star = ALAS/NLAS selections. Top-3 voting isn't in the API, so
# we credit MVP/CY WINNERS (a subset of top-3) — imperfect but never wrong.
_ALLSTAR_IDS = {"ALAS", "NLAS"}
_TOP3_WINNER_IDS = {"ALMVP", "NLMVP", "ALCY", "NLCY", "MLBMVP"}


_ROSTER_CACHE: dict = {"at": 0.0, "players": []}
_FLAGS_CACHE: dict[int, tuple[bool, bool]] = {}


def active_players() -> list[dict]:
    """All active MLB players (~1,300), cached 6h. Powers the unified pool
    search — type any name, rumored or not."""
    now = time.time()
    if now - _ROSTER_CACHE["at"] < 6 * 3600 and _ROSTER_CACHE["players"]:
        return _ROSTER_CACHE["players"]
    try:
        d = json.load(urllib.request.urlopen(
            "https://statsapi.mlb.com/api/v1/sports/1/players?season=2026&hydrate=currentTeam",
            timeout=60))
        from .projections import _TEAM_ABBR
        out = []
        for p in d.get("people", []):
            out.append({
                "id": p.get("id"),
                "name": p.get("fullName"),
                "norm": norm(p.get("fullName")),
                "position": (p.get("primaryPosition") or {}).get("abbreviation"),
                "team": _TEAM_ABBR.get((p.get("currentTeam") or {}).get("id"), ""),
            })
        if out:
            _ROSTER_CACHE["at"] = now
            _ROSTER_CACHE["players"] = out
    except Exception as e:
        logging.warning("active_players fetch failed: %s", e)
    return _ROSTER_CACHE["players"]


def search_players(q: str, limit: int = 8) -> list[dict]:
    """Substring search over all active MLB players, with award flags
    resolved (memoized per player). Excludes names already in the candidate
    pool — those rows are already on the board."""
    nq = norm(q)
    if len(nq) < 3:
        return []
    pool_names = {norm(c["name"]) for c in load_candidates().get("candidates", [])}
    hits = [p for p in active_players()
            if nq in p["norm"] and p["norm"] not in pool_names][:limit]
    out = []
    for p in hits:
        if p["id"] not in _FLAGS_CACHE:
            _FLAGS_CACHE[p["id"]] = _lookup_flags(p["id"])
        has_as, has_t3 = _FLAGS_CACHE[p["id"]]
        out.append({"name": p["name"], "position": p["position"], "team": p["team"],
                    "tier": "write-in", "rumored_teams": [], "context": "not on any rumor list — you still believe",
                    "has_allstar": has_as, "has_top3_voting": has_t3})
    return out


def _lookup_flags(pid: int | None) -> tuple[bool, bool]:
    if not pid:
        return False, False
    try:
        d = json.load(urllib.request.urlopen(
            f"https://statsapi.mlb.com/api/v1/people/{pid}/awards", timeout=15))
        ids = {a.get("id") for a in d.get("awards", [])}
        return bool(ids & _ALLSTAR_IDS), bool(ids & _TOP3_WINNER_IDS)
    except Exception:
        return False, False


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
    resolved = _resolve_player(player_name)
    pid = (resolved or {}).get("id")
    if c is not None:
        has_as, has_t3 = bool(c.get("has_allstar")), bool(c.get("has_top3_voting"))
        pos, team, tier = c.get("position"), c.get("team"), c.get("tier")
        disp = c.get("name")
    else:
        # WRITE-IN: anyone is tradeable, rumored or not (rule per Doron,
        # 2026-07-07). Flags from MLB awards; position/team from the lookup.
        if not pid:
            raise ValueError(f"couldn't find an MLB player named '{player_name}' — check spelling")
        has_as, has_t3 = _lookup_flags(pid)
        pos, team = (resolved or {}).get("position"), (resolved or {}).get("team")
        tier, disp = "write-in", (resolved or {}).get("fullName") or player_name
    pick = {
        "pick_number": len(dr["picks"]) + 1,
        "drafter": drafter,
        "player_name": disp or player_name,
        "player_id": pid,
        "position": pos,
        "team": team,
        "tier": tier,
        "predicted_team": (predicted_team or "").upper()[:3],
        "has_allstar": has_as,
        "has_top3_voting": has_t3,
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
