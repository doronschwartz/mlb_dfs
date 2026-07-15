"""Farm Report — live MiLB production for dynasty prospects.

Two views (rule per Doron, 2026-07-15):
  1. MY FARM: every minor-leaguer on the Fantrax roster with current-season
     stats at each level + a cut/keep verdict — 'who is cuttable or not'.
  2. ADD TARGETS: ranked prospects (uploadable rankings list, TJStats top-100
     seed) who are UNOWNED in the league, with the same live stats — 'who is
     highly ranked and doing well'.

Data: MLB statsapi per-level MiLB splits (FanGraphs is Cloudflare-walled).
wRC+ isn't exposed, so we show the honest computable set: OPS/ISO/K%/BB%
for bats; K-BB%, ERA and FIP-lite for arms. Cached 6h per player-level.
"""
from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import fantrax

DATA_DIR = os.environ.get(
    "MLB_DFS_DRAFT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "drafts"),
)
RANKINGS_PATH = os.path.join(os.path.dirname(DATA_DIR), "prospect_rankings.json")

SPORT_LEVELS = [(11, "AAA"), (12, "AA"), (13, "A+"), (14, "A"), (16, "ROK")]
_CACHE: dict = {}
_TTL = 6 * 3600


def norm(n: str) -> str:
    d = unicodedata.normalize("NFD", n or "")
    a = "".join(c for c in d if not unicodedata.combining(c)).lower()
    a = a.replace(".", "").replace("'", "").replace(",", "")
    return " ".join(t for t in a.split() if t not in ("jr", "sr", "ii", "iii", "iv"))


def _get(url: str):
    key = ("u", url)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        d = json.load(urllib.request.urlopen(url, timeout=20))
    except Exception:
        return None
    _CACHE[key] = (now, d)
    return d


def resolve_pid(name: str) -> int | None:
    from urllib.parse import quote
    d = _get(f"https://statsapi.mlb.com/api/v1/people/search?names={quote(name)}")
    ppl = (d or {}).get("people", [])
    best = next((p for p in ppl if norm(p.get("fullName")) == norm(name)),
                ppl[0] if ppl else None)
    return best.get("id") if best else None


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def milb_lines(pid: int, season: int = 2026) -> dict:
    """{'bat': [level rows], 'arm': [level rows]} across all MiLB levels."""
    bat, arm = [], []
    for sport_id, label in SPORT_LEVELS:
        d = _get(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                 f"?stats=season&season={season}&group=hitting,pitching&sportId={sport_id}")
        for s in (d or {}).get("stats", []):
            group = (s.get("group") or {}).get("displayName")
            for sp in s.get("splits", []):
                st = sp.get("stat", {})
                if group == "hitting" and _f(st.get("plateAppearances")) > 0:
                    pa = _f(st.get("plateAppearances"))
                    bat.append({
                        "level": label, "team": (sp.get("team") or {}).get("name"),
                        "pa": int(pa), "avg": st.get("avg"), "obp": st.get("obp"),
                        "slg": st.get("slg"), "ops": _f(st.get("ops")),
                        "iso": round(_f(st.get("slg")) - _f(st.get("avg")), 3),
                        "k_pct": round(100 * _f(st.get("strikeOuts")) / pa, 1),
                        "bb_pct": round(100 * _f(st.get("baseOnBalls")) / pa, 1),
                        "hr": int(_f(st.get("homeRuns"))), "sb": int(_f(st.get("stolenBases"))),
                    })
                elif group == "pitching" and _f(st.get("inningsPitched")) > 0:
                    ip = _f(st.get("inningsPitched"))
                    bf = max(_f(st.get("battersFaced")), 1)
                    k = _f(st.get("strikeOuts")); bb = _f(st.get("baseOnBalls"))
                    hr = _f(st.get("homeRuns"))
                    fip = round((13 * hr + 3 * bb - 2 * k) / ip + 3.10, 2) if ip else None
                    arm.append({
                        "level": label, "team": (sp.get("team") or {}).get("name"),
                        "ip": st.get("inningsPitched"), "era": st.get("era"),
                        "k_pct": round(100 * k / bf, 1), "bb_pct": round(100 * bb / bf, 1),
                        "kbb_pct": round(100 * (k - bb) / bf, 1), "fip_lite": fip,
                        "whip": st.get("whip"),
                    })
    return {"bat": bat, "arm": arm}


_LEVEL_ORDER = {"AAA": 0, "AA": 1, "A+": 2, "A": 3, "ROK": 4}


def _verdict(lines: dict) -> tuple[str, str]:
    """(verdict, reason). green=keep, yellow=watch, red=cuttable.

    Aggregate across levels, BUT the highest level with a real sample rules:
    a guy getting shelled at AAA can't be green off rookie-ball rehab innings
    (the Bloss bug — 7.06 FIP-lite at AAA hid behind a shiny aggregate)."""
    bats, arms = lines["bat"], lines["arm"]
    if bats:
        pa = sum(b["pa"] for b in bats)
        ops = sum(b["ops"] * b["pa"] for b in bats) / max(pa, 1)
        kp = sum(b["k_pct"] * b["pa"] for b in bats) / max(pa, 1)
        if pa < 50:
            return "yellow", f"small sample ({pa} PA)"
        top = min((b for b in bats if b["pa"] >= 50),
                  key=lambda b: _LEVEL_ORDER.get(b["level"], 9), default=None)
        if top and (top["ops"] < 0.650 or top["k_pct"] > 35):
            return "red", (f"struggling at {top['level']} — {top['ops']:.3f} OPS, "
                           f"{top['k_pct']:.0f}% K ({top['pa']} PA)")
        if ops >= 0.850 and (not top or top["ops"] >= 0.750):
            return "green", f"raking — {ops:.3f} OPS over {pa} PA"
        if ops < 0.700 or kp > 32:
            return "red", f"struggling — {ops:.3f} OPS, {kp:.0f}% K over {pa} PA"
        return "yellow", f"{ops:.3f} OPS over {pa} PA"
    if arms:
        ip = sum(_f(a["ip"]) for a in arms)
        kbb = sum(a["kbb_pct"] * _f(a["ip"]) for a in arms) / max(ip, 1)
        fips = [a["fip_lite"] for a in arms if a["fip_lite"] is not None]
        fip = sum(fips) / len(fips) if fips else None
        if ip < 15:
            return "yellow", f"small sample ({ip:.0f} IP — injured/rehabbing?)"
        top = min((a for a in arms if _f(a["ip"]) >= 15),
                  key=lambda a: _LEVEL_ORDER.get(a["level"], 9), default=None)
        if top and (top["kbb_pct"] < 8 or (top["fip_lite"] is not None and top["fip_lite"] > 5.5)):
            return "red", (f"struggling at {top['level']} — {top['kbb_pct']:.0f}% K-BB, "
                           f"{top['fip_lite']} FIP-lite ({top['ip']} IP)")
        if kbb >= 15 and (fip is None or fip < 4.2) and (not top or top["kbb_pct"] >= 10):
            return "green", f"dealing — {kbb:.0f}% K-BB, {round(fip,2) if fip else fip} FIP-lite over {ip:.0f} IP"
        if kbb < 8 or (fip is not None and fip > 5.0):
            return "red", f"struggling — {kbb:.0f}% K-BB, {round(fip,2) if fip else fip} FIP-lite over {ip:.0f} IP"
        return "yellow", f"{kbb:.0f}% K-BB, {round(fip,2) if fip else fip} FIP-lite over {ip:.0f} IP"
    return "red", "no 2026 MiLB stats found — inactive or long-term injured"


def _player_row(name: str) -> dict:
    pid = resolve_pid(name)
    lines = milb_lines(pid) if pid else {"bat": [], "arm": []}
    verdict, reason = _verdict(lines)
    return {"name": name, "player_id": pid, "verdict": verdict, "reason": reason, **lines}


def my_farm(league_id: str, team_id: str) -> list[dict]:
    """Roster players with 2026 MiLB activity (and roster players with NO
    stats anywhere — the invisible stashes are the most cuttable of all)."""
    if not fantrax.is_authenticated():
        raise fantrax.FantraxAuthError(
            "Fantrax cookie expired — re-auth on the Fantrax tab, then reload")
    roster = fantrax.get_roster(league_id, team_id)
    names = [p["name"] for p in roster.get("players", []) if p.get("name")]
    if not names:
        raise fantrax.FantraxAuthError(
            f"Fantrax returned no roster ({roster.get('error') or 'auth likely expired'}) — re-auth and retry")
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(_player_row, names))
    # farm = has MiLB stats, OR has no MLB presence this season (stash).
    # Exclude MLB regulars on rehab stints — a big-leaguer with 30 MiLB PA
    # isn't a farm asset (Stowers/Ramos-class noise in v1).
    out = []
    for r in rows:
        if r["bat"] or r["arm"]:
            if r["player_id"]:
                mlb = _get(f"https://statsapi.mlb.com/api/v1/people/{r['player_id']}/stats"
                           f"?stats=season&season=2026&group=hitting,pitching&sportId=1")
                sig_mlb = False
                for s in (mlb or {}).get("stats", []):
                    for sp in s.get("splits", []):
                        st = sp.get("stat", {})
                        if _f(st.get("plateAppearances")) > 60 or _f(st.get("inningsPitched")) > 20:
                            sig_mlb = True
                if sig_mlb:
                    continue  # rehabbing MLB regular, not a prospect stash
            out.append(r)
        elif r["player_id"]:
            mlb = _get(f"https://statsapi.mlb.com/api/v1/people/{r['player_id']}/stats"
                       f"?stats=season&season=2026&group=hitting,pitching&sportId=1")
            has_mlb = any(s.get("splits") for s in (mlb or {}).get("stats", []))
            if not has_mlb:
                out.append(r)
    order = {"red": 0, "yellow": 1, "green": 2}
    out.sort(key=lambda r: order.get(r["verdict"], 1))
    return out


def load_rankings() -> dict:
    try:
        return json.load(open(RANKINGS_PATH))
    except Exception:
        return {"as_of": None, "prospects": []}


def save_rankings(payload: dict) -> int:
    pros = payload.get("prospects")
    if not isinstance(pros, list) or len(pros) < 10:
        raise ValueError("payload must contain a prospects list (10+)")
    os.makedirs(os.path.dirname(RANKINGS_PATH), exist_ok=True)
    tmp = RANKINGS_PATH + ".tmp"
    json.dump(payload, open(tmp, "w"))
    os.replace(tmp, RANKINGS_PATH)
    return len(pros)


def add_targets(league_id: str, limit: int = 25, scan: int = 120) -> list[dict]:
    """Ranked prospects unowned by ANY league team who are PLAYING WELL —
    only green (and strong-yellow bats ≥ .800 OPS) survive; red/inactive
    guys are noise, not targets (rule per Doron 2026-07-15). Scans the top
    `scan` unowned by rank, returns up to `limit` performers, best first."""
    if not fantrax.is_authenticated():
        raise fantrax.FantraxAuthError(
            "Fantrax cookie expired — re-auth on the Fantrax tab, then reload")
    ranks = load_rankings().get("prospects", [])
    owned: set[str] = set()
    for t in fantrax.list_teams(league_id):
        r = fantrax.get_roster(league_id, t["team_id"])
        for p in r.get("players", []):
            if p.get("name"):
                owned.add(norm(p["name"]))
    free = [p for p in ranks if norm(p.get("name", "")) not in owned][:scan]
    def enrich(p):
        row = _player_row(p["name"])
        return {**p, **{k: row[k] for k in ("player_id", "verdict", "reason", "bat", "arm")}}
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(enrich, free))
    def good(r):
        if r["verdict"] == "green":
            return True
        if r["verdict"] == "yellow" and r["bat"]:
            pa = sum(b["pa"] for b in r["bat"])
            ops = sum(b["ops"] * b["pa"] for b in r["bat"]) / max(pa, 1)
            return pa >= 100 and ops >= 0.800
        return False
    keep = [r for r in rows if good(r)]
    order = {"green": 0, "yellow": 1}
    keep.sort(key=lambda r: (order.get(r["verdict"], 1), r.get("rank", 999)))
    return keep[:limit]
