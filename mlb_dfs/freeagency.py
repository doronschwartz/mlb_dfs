"""Free-Agency Draft — a prediction game for the MLB offseason.

League-mates snake-draft upcoming free agents and predict, for each, the
signing team, the contract length and the average annual value. Scoring:

    +1  destination team correct
    +1  contract years exactly right
    +1  AAV within 10% of the actual AAV

The candidate pool is research-compiled from free-agent prediction articles
(MLBTR / FanGraphs / ESPN / The Athletic) and stored on the volume
(data/fa_candidates.json, updatable via POST /api/fa/candidates).

Signings: the destination is detected live from MLB's transactions feed
(typeCode SFA, major-league deals only). MLB's feed carries no contract terms,
so years + AAV are recorded per signing (POST /api/fa/signing), which also
overrides the detected team (re-signings / accepted qualifying offers).
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.request
from datetime import date as Date

from .deadline import norm, _resolve_player, DATA_DIR

FA_DRAFT_PATH = os.path.join(os.path.dirname(DATA_DIR), "fa_draft.json")
FA_CANDIDATES_PATH = os.path.join(os.path.dirname(DATA_DIR), "fa_candidates.json")
FA_SIGNINGS_PATH = os.path.join(os.path.dirname(DATA_DIR), "fa_signings.json")

MLB_TEAMS = ["ATH", "ATL", "AZ", "BAL", "BOS", "CHC", "CIN", "CLE", "COL", "CWS",
             "DET", "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY",
             "PHI", "PIT", "SD", "SEA", "SF", "STL", "TB", "TEX", "TOR", "WSH"]
_ALIASES = {"ARI": "AZ", "OAK": "ATH", "CHW": "CWS", "KCR": "KC", "SDP": "SD",
            "SFG": "SF", "TBR": "TB", "WSN": "WSH", "WAS": "WSH", "ANA": "LAA"}
AAV_TOLERANCE = 0.10


def team_abbr(t: str | None) -> str:
    t = (t or "").strip().upper()
    return _ALIASES.get(t, t)


def _load(path: str, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def _save(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---- candidate pool ---------------------------------------------------------

def load_candidates() -> dict:
    return _load(FA_CANDIDATES_PATH, {"as_of": None, "sources": [], "candidates": []})


def save_candidates(payload: dict) -> int:
    cands = payload.get("candidates")
    if not isinstance(cands, list) or len(cands) < 5:
        raise ValueError("payload must contain a candidates list (5+)")
    for c in cands:
        if not c.get("name"):
            raise ValueError(f"candidate missing name: {c}")
        c["team"] = team_abbr(c.get("team"))
        cons = c.get("consensus") or {}
        if cons.get("team"):
            cons["team"] = team_abbr(cons["team"])
        for p in c.get("predictions") or []:
            p["team"] = team_abbr(p.get("team"))
    _save(FA_CANDIDATES_PATH, payload)
    return len(cands)


# ---- draft ------------------------------------------------------------------

def load_draft() -> dict | None:
    return _load(FA_DRAFT_PATH, None)


def save_draft(dr: dict) -> None:
    _save(FA_DRAFT_PATH, dr)


def new_draft(drafters: list[str], rounds: int = 10) -> dict:
    drafters = [d.strip() for d in drafters if d.strip()]
    if len(drafters) < 2:
        raise ValueError("need at least 2 drafters")
    if len(set(drafters)) != len(drafters):
        raise ValueError("duplicate drafter names")
    order = drafters[:]
    random.shuffle(order)  # random snake order
    dr = {"created": Date.today().isoformat(), "drafters": order,
          "rounds": max(1, min(int(rounds), 30)), "picks": []}
    save_draft(dr)
    return dr


def on_the_clock(dr: dict) -> str | None:
    n, D = len(dr["picks"]), len(dr["drafters"])
    if n >= D * dr["rounds"]:
        return None
    rnd, idx = divmod(n, D)
    order = dr["drafters"] if rnd % 2 == 0 else list(reversed(dr["drafters"]))
    return order[idx]


def make_pick(dr: dict, drafter: str, player_name: str, team: str,
              years: int, aav_m: float) -> dict:
    turn = on_the_clock(dr)
    if turn is None:
        raise ValueError("free-agency draft is complete")
    if drafter != turn:
        raise ValueError(f"not your turn — {turn} is on the clock")
    if any(norm(p["player_name"]) == norm(player_name) for p in dr["picks"]):
        raise ValueError(f"{player_name} is already drafted")
    team = team_abbr(team)
    if team not in MLB_TEAMS:
        raise ValueError(f"unknown team '{team}'")
    try:
        years, aav_m = int(years), float(aav_m)
    except (TypeError, ValueError):
        raise ValueError("years must be a whole number and AAV a number ($M)")
    if not 1 <= years <= 15:
        raise ValueError("years must be 1-15")
    if not 0.5 <= aav_m <= 100:
        raise ValueError("AAV must be between $0.5M and $100M")
    # A player who has already signed can't score — reject the pick.
    sg = signings_by_name().get(norm(player_name))
    if sg:
        raise ValueError(f"{player_name} already signed with {sg['team']} — pick someone else")
    cands = {norm(c["name"]): c for c in load_candidates().get("candidates", [])}
    c = cands.get(norm(player_name))
    if c is not None:
        disp, pos, cur = c["name"], c.get("position"), c.get("team")
        pid = c.get("player_id") or (_resolve_player(c["name"]) or {}).get("id")
    else:
        r = _resolve_player(player_name)
        if not r:
            raise ValueError(f"couldn't find an MLB player named '{player_name}'")
        disp, pos, cur, pid = r["fullName"], r.get("position"), team_abbr(r.get("team")), r["id"]
    pick = {"pick_number": len(dr["picks"]) + 1, "picked_at": Date.today().isoformat(),
            "drafter": drafter, "player_name": disp, "player_id": pid,
            "position": pos, "team_2026": cur,
            "pred_team": team, "pred_years": years, "pred_aav_m": round(aav_m, 2)}
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


# ---- signings ---------------------------------------------------------------

_TX_CACHE: dict = {"at": 0.0, "rows": []}
_TX_TTL = 900


def mlb_signings(start: str) -> list[dict]:
    """Major-league free-agent signings (typeCode SFA) since `start`, cached
    15 min. Team only — MLB's feed carries no contract terms."""
    now = time.time()
    if now - _TX_CACHE["at"] < _TX_TTL and _TX_CACHE["rows"]:
        return _TX_CACHE["rows"]
    from .projections import _TEAM_ABBR
    end = Date.today().isoformat()
    try:
        d = json.load(urllib.request.urlopen(
            f"https://statsapi.mlb.com/api/v1/transactions?startDate={start}&endDate={end}&sportId=1",
            timeout=30))
    except Exception as e:
        logging.warning("FA transactions fetch failed: %s", e)
        return _TX_CACHE["rows"]
    rows = []
    for t in d.get("transactions", []):
        if t.get("typeCode") != "SFA":
            continue
        desc = t.get("description") or ""
        if "minor league" in desc.lower():
            continue
        to_id = (t.get("toTeam") or {}).get("id")
        if to_id not in _TEAM_ABBR or not (t.get("person") or {}).get("id"):
            continue
        rows.append({"player": t["person"].get("fullName"), "player_id": t["person"]["id"],
                     "team": team_abbr(_TEAM_ABBR[to_id]), "date": t.get("date"),
                     "years": None, "aav_m": None, "source": "mlb", "desc": desc})
    _TX_CACHE.update(at=now, rows=rows)
    return rows


def load_manual_signings() -> dict:
    return _load(FA_SIGNINGS_PATH, {})


def record_signing(player: str, team: str | None, years: int | None,
                   aav_m: float | None, date: str | None = None) -> dict:
    """Record/override a signing's terms (and team, e.g. re-signings or an
    accepted qualifying offer). Partial updates merge with what's stored."""
    m = load_manual_signings()
    k = norm(player)
    cur = m.get(k, {"player": player})
    if team:
        t = team_abbr(team)
        if t not in MLB_TEAMS:
            raise ValueError(f"unknown team '{team}'")
        cur["team"] = t
    if years is not None:
        cur["years"] = int(years)
    if aav_m is not None:
        cur["aav_m"] = round(float(aav_m), 2)
    if date:
        cur["date"] = date
    cur.setdefault("date", Date.today().isoformat())
    cur["source"] = "manual"
    m[k] = cur
    _save(FA_SIGNINGS_PATH, m)
    return cur


def delete_signing(player: str) -> bool:
    m = load_manual_signings()
    if m.pop(norm(player), None) is None:
        return False
    _save(FA_SIGNINGS_PATH, m)
    return True


def signings_by_name(start: str | None = None) -> dict[str, dict]:
    """Merged view: MLB-detected team + manually recorded terms (manual wins
    field-by-field)."""
    dr = load_draft()
    start = start or (dr or {}).get("created") or "2026-10-30"
    out = {norm(r["player"]): dict(r) for r in mlb_signings(start)}
    for k, v in load_manual_signings().items():
        base = out.get(k, {})
        out[k] = {**base, **{f: x for f, x in v.items() if x is not None}}
    return out


# ---- scoring ----------------------------------------------------------------

def score(dr: dict) -> dict:
    sg = signings_by_name(dr.get("created"))
    totals = {d: 0 for d in dr["drafters"]}
    picks = []
    for p in dr["picks"]:
        s = sg.get(norm(p["player_name"]))
        # a signing dated before the pick can't score (no free points)
        if s and s.get("date") and s["date"] < p.get("picked_at", ""):
            s = None
        team_pt = years_pt = aav_pt = None
        if s and s.get("team"):
            team_pt = int(s["team"] == p["pred_team"])
        if s and s.get("years"):
            years_pt = int(int(s["years"]) == int(p["pred_years"]))
        if s and s.get("aav_m"):
            aav_pt = int(abs(p["pred_aav_m"] - s["aav_m"]) <= AAV_TOLERANCE * s["aav_m"])
        pts = (team_pt or 0) + (years_pt or 0) + (aav_pt or 0)
        totals[p["drafter"]] = totals.get(p["drafter"], 0) + pts
        picks.append({**p, "signed": bool(s), "actual": s,
                      "team_pt": team_pt, "years_pt": years_pt, "aav_pt": aav_pt, "points": pts})
    standings = sorted(({"drafter": d, "points": v,
                         "signed": sum(1 for x in picks if x["drafter"] == d and x["signed"]),
                         "picks": sum(1 for x in picks if x["drafter"] == d)}
                        for d, v in totals.items()), key=lambda r: -r["points"])
    return {"picks": picks, "standings": standings,
            "signings_known": len(sg)}
