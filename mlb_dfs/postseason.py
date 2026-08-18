"""Postseason Fantasy — the October version of the league's spreadsheet game.

Managers (any number, 2+) snake-draft playoff-team players into fixed roster
slots before the postseason; real playoff stats accumulate into rotisserie
standings across 10 categories (rank in category -> N..1 points, ties split):

    hitting:  AVG  R  HR  RBI  SB
    pitching: ERA  WHIP  K  QS  SV+H

plus MVP bonuses to every owner of the award winner: NLCS/ALCS MVP +2, WS MVP +4.

The projection model prices EXPECTED PLAYING TIME, not just talent: per-team
World Series futures odds are devigged, a Bradley-Terry strength per playoff
team is calibrated so the simulated bracket reproduces the market's WS probs,
and the bracket is then solved exactly (WC bo3 -> LDS bo5 -> LCS bo7 -> WS bo7,
seeds 1-2 bye) for each team's expected games. A player's projected PA/IP is
his regular-season per-team-game rate scaled by his team's expected games —
so a Dodgers regular projects ~45-55 PA while a weak wild-card team's star
projects ~10, which is the whole edge of the draft.

State is one JSON file per season next to the drafts dir (persistent /data
volume in prod): postseason_<season>.json.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
import unicodedata
from datetime import date as Date

from . import mlb_api

DATA_DIR = os.environ.get(
    "MLB_DFS_DRAFT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "drafts"),
)

# Roster shape from the league's 2025 spreadsheet.
DEFAULT_SLOTS = ["C", "1B", "2B", "3B", "SS", "C+IF",
                 "OF", "OF", "OF", "OF", "UT", "UT",
                 "SP", "SP", "SP", "P", "RP", "RP"]
HIT_SLOTS = {"C", "1B", "2B", "3B", "SS", "C+IF", "OF", "UT"}
PIT_SLOTS = {"SP", "P", "RP"}
HIT_CATS = ["AVG", "R", "HR", "RBI", "SB"]
PIT_CATS = ["ERA", "WHIP", "K", "QS", "SVH"]
LOW_IS_GOOD = {"ERA", "WHIP"}
MVP_POINTS = {"NLCS": 2.0, "ALCS": 2.0, "WS": 4.0}

# Which roster slots a fielding position may fill (besides UT, open to all hitters).
_POS_SLOTS = {
    "C": {"C", "C+IF"},
    "1B": {"1B", "C+IF"}, "2B": {"2B", "C+IF"}, "3B": {"3B", "C+IF"}, "SS": {"SS", "C+IF"},
    "LF": {"OF"}, "CF": {"OF"}, "RF": {"OF"}, "OF": {"OF"},
    "DH": set(),
    "P": {"SP", "P", "RP"}, "SP": {"SP", "P"}, "RP": {"RP", "P"},
    # Two-way players (Ohtani) can be drafted as a hitter (UT) or any pitcher slot
    "TWP": {"SP", "P", "RP", "UT"},
}


def norm(n: str) -> str:
    d = unicodedata.normalize("NFD", n or "")
    a = "".join(c for c in d if not unicodedata.combining(c)).lower()
    a = a.replace(".", "").replace("'", "").replace(",", "")
    return " ".join(t for t in a.split() if t not in ("jr", "sr", "ii", "iii", "iv"))


# ----- league state -----------------------------------------------------------


def _league_path(season: int) -> str:
    return os.path.join(os.path.dirname(DATA_DIR), f"postseason_{season}.json")


def load_league(season: int) -> dict | None:
    try:
        return json.load(open(_league_path(season)))
    except Exception:
        return None


def save_league(lg: dict) -> None:
    path = _league_path(int(lg["season"]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(lg, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def ensure_league(season: int) -> dict:
    """Load the season's league, or create a managerless stub so odds/model
    config can be saved before anyone commits to a draft."""
    lg = load_league(season)
    if lg:
        return lg
    lg = {
        "season": int(season), "created": Date.today().isoformat(),
        "managers": [], "slots": DEFAULT_SLOTS, "picks": [],
        "odds": {}, "mvp_awards": {},
    }
    save_league(lg)
    return lg


def new_league(season: int, managers: list[str]) -> dict:
    managers = [m.strip() for m in managers if m.strip()]
    if len(managers) < 2:
        raise ValueError("need at least 2 managers")
    if len(set(managers)) != len(managers):
        raise ValueError("duplicate manager names")
    order = managers[:]
    random.shuffle(order)  # random initial snake order, per league rules
    prior = load_league(int(season)) or {}  # keep odds saved before creation
    lg = {
        "season": int(season),
        "created": Date.today().isoformat(),
        "managers": order,
        "slots": DEFAULT_SLOTS,
        "picks": [],
        "odds": prior.get("odds") or {},   # {team_abbrev: american WS odds}
        "mvp_awards": prior.get("mvp_awards") or {},
    }
    for k in ("ws_probs", "fg_ladder", "ws_probs_source"):
        if prior.get(k):
            lg[k] = prior[k]
    save_league(lg)
    return lg


def on_the_clock(lg: dict) -> str | None:
    n = len(lg["picks"])
    mgrs = lg["managers"]
    if n >= len(mgrs) * len(lg["slots"]):
        return None
    rnd, idx = divmod(n, len(mgrs))
    order = mgrs if rnd % 2 == 0 else list(reversed(mgrs))
    return order[idx]


def open_slots(lg: dict, manager: str) -> list[str]:
    taken = [p["slot"] for p in lg["picks"] if p["manager"] == manager]
    left = list(lg["slots"])
    for s in taken:
        if s in left:
            left.remove(s)
    return left


def eligible_slots(position: str, open_: list[str]) -> list[str]:
    pos = (position or "").upper()
    allowed = _POS_SLOTS.get(pos, set())
    if pos in _POS_SLOTS and pos not in ("P", "SP", "RP", "TWP"):
        allowed = allowed | {"UT"}
    elif pos not in _POS_SLOTS:  # unknown position: let it slide as a hitter
        allowed = HIT_SLOTS
    seen, out = set(), []
    for s in open_:
        if s in allowed and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def make_pick(lg: dict, manager: str, slot: str, player_id: int, name: str,
              team_id: int, team: str, position: str, force: bool = False) -> dict:
    clock = on_the_clock(lg)
    if clock is None:
        raise ValueError("draft is complete")
    if manager != clock:
        raise ValueError(f"{clock} is on the clock, not {manager}")
    open_ = open_slots(lg, manager)
    if slot not in open_:
        raise ValueError(f"{manager} has no open {slot} slot (open: {open_})")
    if not force and slot not in eligible_slots(position, open_):
        raise ValueError(f"{position} is not eligible at {slot}")
    if any(p["player_id"] == player_id and p["role"] == _slot_role(slot) for p in lg["picks"]):
        raise ValueError(f"{name} already drafted in a {_slot_role(slot)} slot")
    pick = {
        "manager": manager, "slot": slot, "player_id": int(player_id), "name": name,
        "team_id": int(team_id), "team": team, "position": position,
        "role": _slot_role(slot), "pick_number": len(lg["picks"]) + 1,
    }
    lg["picks"].append(pick)
    save_league(lg)
    return pick


def undo_pick(lg: dict) -> dict | None:
    if not lg["picks"]:
        return None
    p = lg["picks"].pop()
    save_league(lg)
    return p


def _slot_role(slot: str) -> str:
    return "pitcher" if slot in PIT_SLOTS else "hitter"


# ----- playoff field & bracket math ------------------------------------------


_CACHE: dict = {}


def _cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


def playoff_field(season: int) -> dict:
    """{'AL': [team...seeded 1-6], 'NL': [...]} — the current would-be field
    from live standings (division leaders 1-3 by record, wild cards 4-6)."""
    def build():
        data = mlb_api._get("/standings", params={
            "leagueId": "103,104", "season": season, "standingsTypes": "regularSeason"})
        by_lg: dict[str, list[dict]] = {"AL": [], "NL": []}
        for rec in data.get("records", []):
            lg_id = ((rec.get("league") or {}).get("id"))
            lg = "AL" if lg_id == 103 else "NL"
            for tr in rec.get("teamRecords", []):
                t = tr.get("team", {})
                by_lg[lg].append({
                    "team_id": t.get("id"), "name": t.get("name"),
                    "wins": tr.get("wins", 0), "losses": tr.get("losses", 0),
                    "pct": float(tr.get("winningPercentage") or 0),
                    "games": tr.get("wins", 0) + tr.get("losses", 0),
                    "div_rank": int(tr.get("divisionRank") or 99),
                    "runs_scored": tr.get("runsScored", 0),
                    "runs_allowed": tr.get("runsAllowed", 0),
                })
        abbrevs = {t["id"]: t.get("abbreviation") for t in mlb_api.teams()}
        field = {}
        for lg, rows in by_lg.items():
            champs = sorted([r for r in rows if r["div_rank"] == 1],
                            key=lambda r: -r["pct"])[:3]
            rest = sorted([r for r in rows if r not in champs],
                          key=lambda r: -r["pct"])[:3]
            seeded = champs + rest
            for i, r in enumerate(seeded):
                r["seed"] = i + 1
                r["abbrev"] = abbrevs.get(r["team_id"], "?")
            field[lg] = seeded
        return field
    return _cached(("field", season), 1800, build)


def implied_ws_probs(odds: dict[str, float], abbrevs: list[str]) -> dict[str, float]:
    """Devig american WS futures over the playoff field. Teams in the field
    with no line get the median implied prob before normalization."""
    raw = {}
    for ab in abbrevs:
        a = odds.get(ab)
        if a is None:
            continue
        a = float(a)
        raw[ab] = 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)
    if not raw:
        return {ab: 1.0 / len(abbrevs) for ab in abbrevs}
    med = sorted(raw.values())[len(raw) // 2]
    full = {ab: raw.get(ab, med) for ab in abbrevs}
    z = sum(full.values())
    return {ab: p / z for ab, p in full.items()}


def series_win_prob(p: float, wins_needed: int) -> float:
    """P(win a best-of series) given per-game win prob p (home field ignored)."""
    n = 2 * wins_needed - 1
    total = 0.0
    for k in range(wins_needed, n + 1):  # series ends when we get win #wins_needed in game k
        total += math.comb(k - 1, wins_needed - 1) * (p ** wins_needed) * ((1 - p) ** (k - wins_needed))
    return total


def expected_series_games(p: float, wins_needed: int) -> float:
    """E[series length] — either side finishing counts."""
    exp = 0.0
    for k in range(wins_needed, 2 * wins_needed):
        end_k = math.comb(k - 1, wins_needed - 1) * (
            p ** wins_needed * (1 - p) ** (k - wins_needed)
            + (1 - p) ** wins_needed * p ** (k - wins_needed))
        exp += k * end_k
    return exp


def _pwin(strengths: dict, a: str, b: str) -> float:
    return 1.0 / (1.0 + math.exp(-(strengths[a] - strengths[b])))


def _league_solve(seeds: list[str], strengths: dict) -> tuple[dict, dict, dict]:
    """Solve one league's bracket exactly. seeds = [s1..s6] abbrevs.
    Returns (pennant_prob, exp_games, reach) — reach[t] = {'LDS':p,'LCS':p}."""
    s = seeds
    exp = {t: 0.0 for t in s}
    reach = {t: {"LDS": 0.0, "LCS": 0.0} for t in s}

    def duel(a_dist: dict, b_dist: dict, wins_needed: int) -> dict:
        """a_dist/b_dist: {team: prob of being here}. Returns winner dist and
        accrues expected games + reach-this-round bookkeeping via closures."""
        out: dict[str, float] = {}
        for a, pa in a_dist.items():
            for b, pb in b_dist.items():
                if pa <= 0 or pb <= 0:
                    continue
                w = _pwin(strengths, a, b)
                meet = pa * pb
                glen = expected_series_games(w, wins_needed)
                exp[a] += meet * glen
                exp[b] += meet * glen
                out[a] = out.get(a, 0.0) + meet * series_win_prob(w, wins_needed)
                out[b] = out.get(b, 0.0) + meet * series_win_prob(1 - w, wins_needed)
        return out

    wc1 = duel({s[3]: 1.0}, {s[4]: 1.0}, 2)   # 4 vs 5, best-of-3
    wc2 = duel({s[2]: 1.0}, {s[5]: 1.0}, 2)   # 3 vs 6
    for t, pr in {**{s[0]: 1.0, s[1]: 1.0}, **wc1, **wc2}.items():
        reach[t]["LDS"] = max(reach[t]["LDS"], pr) if t in (s[0], s[1]) else pr
    lds1 = duel({s[0]: 1.0}, wc1, 3)          # 1 vs winner(4/5), best-of-5
    lds2 = duel({s[1]: 1.0}, wc2, 3)          # 2 vs winner(3/6)
    for t, pr in {**lds1, **lds2}.items():
        reach[t]["LCS"] = pr
    pennant = duel(lds1, lds2, 4)             # LCS best-of-7
    return pennant, exp, reach


def solve_bracket(field: dict, strengths: dict) -> dict:
    """Full-bracket exact solve. Returns per-team model dict keyed by abbrev."""
    out = {}
    pennants, exps, reaches = {}, {}, {}
    for lg in ("AL", "NL"):
        seeds = [t["abbrev"] for t in field[lg]]
        pen, exp, reach = _league_solve(seeds, strengths)
        pennants[lg], reaches[lg] = pen, reach
        exps.update(exp)
    for lg, other in (("AL", "NL"), ("NL", "AL")):
        for t in [x["abbrev"] for x in field[lg]]:
            ws_p, ws_games = 0.0, 0.0
            pt = pennants[lg].get(t, 0.0)
            for o, po in pennants[other].items():
                if pt <= 0 or po <= 0:
                    continue
                w = _pwin(strengths, t, o)
                ws_p += pt * po * series_win_prob(w, 4)
                ws_games += pt * po * expected_series_games(w, 4)
            exps[t] = exps.get(t, 0.0) + ws_games
            seed = next(x["seed"] for x in field[lg] if x["abbrev"] == t)
            out[t] = {
                "league": lg, "seed": seed,
                "strength": round(strengths[t], 4),
                "p_reach_lds": round(reaches[lg][t]["LDS"], 4),
                "p_reach_lcs": round(reaches[lg][t]["LCS"], 4),
                "p_pennant": round(pt, 4),
                "p_ws": round(ws_p, 4),
                "exp_games": round(exps[t], 2),
            }
    return out


def calibrate_strengths(field: dict, target_ws: dict[str, float]) -> dict:
    """Fit Bradley-Terry strengths so the exact bracket solve reproduces the
    market's devigged WS probabilities."""
    teams = [t["abbrev"] for lg in ("AL", "NL") for t in field[lg]]
    tgt = {t: min(max(target_ws.get(t, 1.0 / len(teams)), 1e-3), 0.9) for t in teams}
    z = sum(tgt.values())
    tgt = {t: p / z for t, p in tgt.items()}
    r = {t: 0.0 for t in teams}
    for _ in range(400):
        model = solve_bracket(field, r)
        err = 0.0
        for t in teams:
            m = max(model[t]["p_ws"], 1e-6)
            step = 0.15 * (math.log(tgt[t]) - math.log(m))
            step = max(-0.25, min(0.25, step))  # clamp: raw log steps explode as m -> 0
            r[t] += step
            err = max(err, abs(step))
        mean = sum(r.values()) / len(r)
        r = {t: v - mean for t, v in r.items()}
        if err < 1e-3:
            break
    return r


def pythag_strengths(field: dict) -> dict:
    """Default team strengths when no market odds are saved: Pythagorean
    expectation from run differential, compressed toward the mean (raw season
    records overstate true-talent gaps in a short series)."""
    r = {}
    for lg in ("AL", "NL"):
        for t in field[lg]:
            rs, ra = max(t.get("runs_scored") or 1, 1), max(t.get("runs_allowed") or 1, 1)
            pyth = rs ** 1.83 / (rs ** 1.83 + ra ** 1.83)
            pyth = min(max(pyth, 0.35), 0.72)
            r[t["abbrev"]] = 0.7 * math.log(pyth / (1 - pyth))
    mean = sum(r.values()) / len(r)
    return {t: v - mean for t, v in r.items()}


# FanGraphs team abbreviations that differ from MLB statsapi's.
_FG_ABBREV = {"TBR": "TB", "SDP": "SD", "SFG": "SF", "KCR": "KC",
              "WSN": "WSH", "CHW": "CWS", "ARI": "AZ", "ANA": "LAA"}


def parse_fangraphs(data) -> dict[str, dict]:
    """Parse a pasted FanGraphs playoff-odds JSON payload into a per-team
    advancement ladder: {abbrev: {playoffs, reach_lds, reach_lcs, pennant,
    ws_win}} (whatever subset their schema exposes; ws_win is required).

    FanGraphs sits behind Cloudflare so the server can't fetch it — the user
    opens fangraphs.com/api/playoff-odds/odds?... in their own browser and
    pastes the JSON. Key names drift, so detect them by fragment: the team key
    is whichever field holds a known abbreviation; probability keys are
    numeric fields whose names mention the round. Values may be 0-1 or pct."""
    if isinstance(data, dict):
        data = data.get("data") or data.get("odds") or list(data.values())
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("expected the FanGraphs JSON array of team rows")
    known = {t.get("abbreviation") for t in mlb_api.teams()} | set(_FG_ABBREV)

    def find_team(row: dict) -> str | None:
        for v in row.values():
            if isinstance(v, str) and v.upper() in known:
                return _FG_ABBREV.get(v.upper(), v.upper())
            if isinstance(v, dict):  # nested team object
                t = find_team(v)
                if t:
                    return t
        return None

    def classify(key: str) -> str | None:
        lk = key.lower().replace("_", "").replace(" ", "")
        wins = "win" in lk or "won" in lk
        if "ws" in lk or "worldseries" in lk:
            return "ws_win" if wins else "pennant"      # make/appear in WS = pennant
        if "lcs" in lk:
            return "pennant" if wins else "reach_lcs"   # win LCS = pennant
        if "lds" in lk or "divseries" in lk or "ds" == lk:
            return "reach_lcs" if wins else "reach_lds"
        if "poff" in lk or "playoff" in lk:
            return "playoffs"
        return None

    def walk(row: dict, out: dict) -> None:
        for k, v in row.items():
            if isinstance(v, dict):
                walk(v, out)
            elif isinstance(v, (int, float)):
                cat = classify(k)
                if cat:
                    p = float(v) / 100.0 if float(v) > 1.0 else float(v)
                    out[cat] = max(out.get(cat, 0.0), p) if cat != "ws_win" else p

    ladder: dict[str, dict] = {}
    for row in data:
        team = find_team(row)
        if not team:
            continue
        entry: dict = {}
        walk(row, entry)
        if "ws_win" not in entry and "pennant" in entry:
            # a lone "worldSeries" field is almost always P(win it), not P(appear)
            entry["ws_win"] = entry.pop("pennant")
        if "ws_win" in entry and entry.get("pennant", 1.0) < entry["ws_win"]:
            entry["pennant"], entry["ws_win"] = entry["ws_win"], entry["pennant"]
        # enforce the monotone ladder: each round's reach prob caps the next
        hi = entry.get("playoffs", 1.0)
        for k in ("reach_lds", "reach_lcs", "pennant", "ws_win"):
            if k in entry:
                entry[k] = min(entry[k], hi)
                hi = entry[k]
        if "ws_win" in entry:
            ladder[team] = entry
    if len(ladder) < 8:
        raise ValueError(f"could only parse {len(ladder)} teams — paste the raw JSON from the playoff-odds API")
    return ladder


# Expected series lengths — near-flat across realistic per-game edges
_E_LEN = {"wc": 2.55, "lds": 4.15, "lcs": 5.8, "ws": 5.8}


def _apply_fg_ladder(teams: dict, field: dict, ladder: dict) -> int:
    """Refine per-team reach probs + expected games with FanGraphs' full
    advancement ladder (make LDS / make LCS / make WS / win WS), conditioned
    on making the playoffs so August qualification odds don't dilute the
    bracket. Returns how many teams were refined."""
    n = 0
    for lg_ in ("AL", "NL"):
        for t in field[lg_]:
            ab = t["abbrev"]
            L = ladder.get(ab)
            if not L or len({"reach_lds", "reach_lcs", "pennant"} & set(L)) < 2:
                continue
            pp = L.get("playoffs", 0.0)
            cond = (lambda v: min(1.0, v / pp)) if pp > 0.02 else (lambda v: v)
            tm = teams[ab]
            bye = tm["seed"] <= 2
            rl = cond(L["reach_lds"]) if "reach_lds" in L else (1.0 if bye else tm["p_reach_lds"])
            rc = cond(L["reach_lcs"]) if "reach_lcs" in L else tm["p_reach_lcs"]
            pn = cond(L["pennant"]) if "pennant" in L else tm["p_pennant"]
            ww = cond(L["ws_win"])
            exp = (0.0 if bye else _E_LEN["wc"]) + rl * _E_LEN["lds"] + rc * _E_LEN["lcs"] + pn * _E_LEN["ws"]
            tm.update({"p_reach_lds": round(rl, 4), "p_reach_lcs": round(rc, 4),
                       "p_pennant": round(pn, 4), "p_ws": round(ww, 4),
                       "exp_games": round(exp, 2)})
            n += 1
    return n


def advancement_model(season: int, odds: dict, ws_probs: dict | None = None,
                      fg_ladder: dict | None = None) -> dict:
    """Field + calibrated per-team advancement probs + expected games.
    Calibration target priority: explicit model probs (FanGraphs import) >
    devigged market odds > Pythagorean defaults. A full FanGraphs ladder
    additionally refines round-reach probs and expected games directly."""
    key = ("model", season, tuple(sorted(odds.items())),
           tuple(sorted((ws_probs or {}).items())),
           tuple(sorted((k, tuple(sorted(v.items()))) for k, v in (fg_ladder or {}).items())))

    def build():
        field = playoff_field(season)
        abbrevs = [t["abbrev"] for lg in ("AL", "NL") for t in field[lg]]
        if ws_probs:
            in_field = {ab: ws_probs[ab] for ab in abbrevs if ab in ws_probs}
            med = sorted(in_field.values())[len(in_field) // 2] if in_field else 1.0
            full = {ab: ws_probs.get(ab, med) for ab in abbrevs}
            z = sum(full.values()) or 1.0
            target = {ab: p / z for ab, p in full.items()}
            strengths = calibrate_strengths(field, target)
            mode = "fangraphs"
        elif odds:
            target = implied_ws_probs(odds, abbrevs)
            strengths = calibrate_strengths(field, target)
            mode = "market"
        else:
            target = {}
            strengths = pythag_strengths(field)
            mode = "pythag"
        teams = solve_bracket(field, strengths)
        if fg_ladder:
            refined = _apply_fg_ladder(teams, field, fg_ladder)
            if refined >= 8:
                mode = "fangraphs-ladder"
        for lg in ("AL", "NL"):
            for t in field[lg]:
                teams[t["abbrev"]].update({
                    "team_id": t["team_id"], "name": t["name"],
                    "record": f"{t['wins']}-{t['losses']}", "reg_games": t["games"],
                    "market_ws": round(target.get(t["abbrev"], 0.0), 4),
                    "odds": odds.get(t["abbrev"]),
                    # expected games CONDITIONAL on winning it all — the ceiling
                    # a drafter should also see (bye skips the WC round)
                    "ws_run_games": 15.8 if teams[t["abbrev"]]["seed"] <= 2 else 18.4,
                })
        return {"season": season, "teams": teams, "mode": mode}
    return _cached(key, 600, build)


def fetch_ws_futures() -> dict[str, float]:
    """World Series winner futures from The Odds API -> {abbrev: american}."""
    from .odds_api import BASE  # reuse existing config
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        raise RuntimeError("ODDS_API_KEY not configured")
    import requests
    r = requests.get(
        f"{BASE}/sports/baseball_mlb_world_series_winner/odds",
        params={"apiKey": key, "regions": "us", "oddsFormat": "american"},
        timeout=20)
    r.raise_for_status()
    events = r.json()
    by_name: dict[str, list[float]] = {}
    for ev in events:
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for oc in mk.get("outcomes", []):
                    by_name.setdefault(oc["name"], []).append(float(oc["price"]))
    name_to_ab = {t["name"]: t.get("abbreviation") for t in mlb_api.teams()}
    out = {}
    for name, prices in by_name.items():
        ab = name_to_ab.get(name)
        if ab:
            prices.sort()
            out[ab] = prices[len(prices) // 2]  # median book
    return out


# ----- player pool & projections ---------------------------------------------


def _team_pool(team_id: int, season: int) -> list[dict]:
    """Team roster hydrated with season + last-30-days hitting/pitching stats
    (one call). The recent window prices CURRENT role — September rotations
    and call-ups, not April's."""
    def build():
        from datetime import timedelta
        end = Date.today()
        start = end - timedelta(days=30)
        data = mlb_api._get(
            f"/teams/{team_id}/roster",
            params={"rosterType": "active", "season": season,
                    "hydrate": (f"person(stats(group=[hitting,pitching],type=[season,byDateRange],"
                                f"season={season},startDate={start.isoformat()},endDate={end.isoformat()}))")})
        return data.get("roster", [])
    return _cached(("pool", team_id, season), 21600, build)


_RECENT_TEAM_G = 27.0   # ~team games in a 30-day window
_RECENT_W = 0.65        # weight on last-30-days usage vs full season


def _split(person: dict, group: str, stat_type: str = "season") -> dict:
    for s in person.get("stats", []):
        if ((s.get("group") or {}).get("displayName") == group
                and (s.get("type") or {}).get("displayName") == stat_type
                and s.get("splits")):
            return s["splits"][0].get("stat", {})
    return {}


def _ip_to_outs(ip) -> int:
    if not ip:
        return 0
    s = str(ip)
    whole, _, frac = s.partition(".")
    return int(whole or 0) * 3 + int(frac or 0)


def player_board(season: int, odds: dict, ws_probs: dict | None = None,
                 fg_ladder: dict | None = None) -> list[dict]:
    """The draft board: every player on a playoff-field team, priced by his
    team's expected postseason games. exp_pa / exp_ip are the headline numbers."""
    model = advancement_model(season, odds, ws_probs, fg_ladder)
    rows = []
    for ab, tm in model["teams"].items():
        reg_g = max(tm["reg_games"], 1)
        eg = tm["exp_games"]
        ceil_g = tm.get("ws_run_games", 18.0)
        for entry in _team_pool(tm["team_id"], season):
            person = entry.get("person", {})
            pos = (entry.get("position") or {}).get("abbreviation", "")
            hit, hit30 = _split(person, "hitting"), _split(person, "hitting", "byDateRange")
            pit, pit30 = _split(person, "pitching"), _split(person, "pitching", "byDateRange")
            base = {
                "player_id": person.get("id"), "name": person.get("fullName"),
                "team": ab, "team_id": tm["team_id"], "position": pos,
                "exp_games": eg, "p_ws": tm["p_ws"],
            }

            def blend_pg(season_total, recent_total, has_recent):
                s = (season_total or 0) / reg_g
                if not has_recent:
                    return s
                r = (recent_total or 0) / _RECENT_TEAM_G
                return _RECENT_W * r + (1 - _RECENT_W) * s

            if pos != "P" and hit.get("plateAppearances"):
                pa = hit["plateAppearances"]
                # usage from recent role, production rates from full season
                pa_pg = blend_pg(pa, hit30.get("plateAppearances"), bool(hit30.get("gamesPlayed")))
                exp_pa = pa_pg * eg
                avg = float(hit.get("avg") or 0)
                ab_share = (hit.get("atBats") or 0) / pa
                per_pa = {k: (hit.get(s_) or 0) / pa for k, s_ in
                          (("R", "runs"), ("HR", "homeRuns"), ("RBI", "rbi"), ("SB", "stolenBases"))}
                proj_ab = exp_pa * ab_share
                rows.append({**base, "role": "hitter",
                    "exp_pa": round(exp_pa, 1),
                    "ceil_pa": round(pa_pg * ceil_g, 0),
                    "proj": {
                        "AVG": round(avg, 3), "AB": round(proj_ab, 1),
                        "H": round(proj_ab * avg, 1),
                        "R": round(per_pa["R"] * exp_pa, 1),
                        "HR": round(per_pa["HR"] * exp_pa, 1),
                        "RBI": round(per_pa["RBI"] * exp_pa, 1),
                        "SB": round(per_pa["SB"] * exp_pa, 1),
                    },
                    "season": {"PA": pa, "AVG": hit.get("avg"),
                               "HR": hit.get("homeRuns"), "OPS": hit.get("ops")}})
            if pit.get("inningsPitched"):
                outs = _ip_to_outs(pit["inningsPitched"])
                ip = outs / 3.0
                gs = pit.get("gamesStarted") or 0
                gp = pit.get("gamesPlayed") or 1
                gs30 = pit30.get("gamesStarted") or 0
                is_starter = (gs / gp >= 0.5 and gs >= 3) or gs30 >= 3
                era = float(pit.get("era") or 0)
                whip = float(pit.get("whip") or 0)
                k_per_ip = (pit.get("strikeOuts") or 0) / max(ip, 1.0)
                if is_starter:
                    # Playoff rotations contract to ~4 arms: a healthy starter's
                    # share of team games RISES vs a 5-man regular season.
                    starts_pg = blend_pg(gs, gs30, bool(pit30.get("gamesPlayed")))
                    starts_pg = min(1 / 4.0, starts_pg * 1.25)
                    exp_starts = starts_pg * eg
                    ip_ps = ip / gs if gs else 5.0
                    exp_ip = exp_starts * ip_ps
                    ceil_ip = starts_pg * ceil_g * ip_ps
                    p_qs = max(0.0, min(0.85, (ip_ps - 4.4) / 2.4)) * max(0.3, min(1.1, 1.55 - era / 4.0))
                    qs = p_qs * exp_starts
                    svh = 0.0
                else:
                    ip_pg = blend_pg(ip, _ip_to_outs(pit30.get("inningsPitched")) / 3.0,
                                     bool(pit30.get("gamesPlayed")))
                    exp_starts = 0.0
                    exp_ip = ip_pg * eg
                    ceil_ip = ip_pg * ceil_g
                    qs = 0.0
                    svh_pg = blend_pg((pit.get("saves") or 0) + (pit.get("holds") or 0),
                                      (pit30.get("saves") or 0) + (pit30.get("holds") or 0),
                                      bool(pit30.get("gamesPlayed")))
                    svh = svh_pg * eg
                rows.append({**base, "role": "pitcher",
                    "exp_ip": round(exp_ip, 1),
                    "exp_starts": round(exp_starts, 1) if is_starter else None,
                    "ceil_ip": round(ceil_ip, 0),
                    "proj": {
                        "IP": round(exp_ip, 1), "ERA": round(era, 2), "WHIP": round(whip, 2),
                        "K": round(k_per_ip * exp_ip, 1),
                        "QS": round(qs, 2),
                        "SVH": round(svh, 2),
                    },
                    "season": {"IP": pit.get("inningsPitched"), "ERA": pit.get("era"),
                               "WHIP": pit.get("whip"), "K": pit.get("strikeOuts"),
                               "SV": pit.get("saves"), "HLD": pit.get("holds"), "GS": gs, "G": gp}})
    _attach_value(rows)
    rows.sort(key=lambda r: -r["value"])
    return rows


def _attach_value(rows: list[dict]) -> None:
    """Rough roto value: sum of pool z-scores (AVG/ERA/WHIP impact-weighted)."""
    def zfn(vals):
        vals = [v for v in vals if v is not None]
        if len(vals) < 2:
            return lambda x: 0.0
        mu = sum(vals) / len(vals)
        sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
        return lambda x: (x - mu) / sd
    hitters = [r for r in rows if r["role"] == "hitter"]
    pitchers = [r for r in rows if r["role"] == "pitcher"]
    if hitters:
        zs = {c: zfn([h["proj"][c] for h in hitters]) for c in ("R", "HR", "RBI", "SB")}
        zab = zfn([h["proj"]["AB"] for h in hitters])
        mu_avg = sum(h["proj"]["AVG"] for h in hitters) / len(hitters)
        for h in hitters:
            v = sum(zs[c](h["proj"][c]) for c in ("R", "HR", "RBI", "SB"))
            v += (h["proj"]["AVG"] - mu_avg) * 25 * max(0.2, min(2.0, 0.5 + zab(h["proj"]["AB"])))
            h["value"] = round(v, 2)
    if pitchers:
        zk = zfn([p["proj"]["K"] for p in pitchers])
        zqs = zfn([p["proj"]["QS"] for p in pitchers])
        zsv = zfn([p["proj"]["SVH"] for p in pitchers])
        zip_ = zfn([p["proj"]["IP"] for p in pitchers])
        mu_era = sum(p["proj"]["ERA"] for p in pitchers) / len(pitchers)
        mu_whip = sum(p["proj"]["WHIP"] for p in pitchers) / len(pitchers)
        for p in pitchers:
            ipw = max(0.2, min(2.0, 0.5 + zip_(p["proj"]["IP"])))
            v = zk(p["proj"]["K"]) + zqs(p["proj"]["QS"]) + zsv(p["proj"]["SVH"])
            v += ((mu_era - p["proj"]["ERA"]) / 1.5 + (mu_whip - p["proj"]["WHIP"]) / 0.35) * ipw
            p["value"] = round(v, 2)


# ----- live postseason scoring ------------------------------------------------


def _post_window(season: int) -> tuple[str, str]:
    return f"{season}-09-20", f"{season}-12-01"


def _post_stats(pid: int, season: int, group: str) -> dict:
    def build():
        start, end = _post_window(season)
        try:
            data = mlb_api._get(f"/people/{pid}/stats", params={
                "stats": "byDateRange", "group": group, "season": season,
                "startDate": start, "endDate": end, "gameType": "P"})
        except mlb_api.MlbApiError:
            return {}
        for s in data.get("stats", []):
            if s.get("splits"):
                return s["splits"][0].get("stat", {})
        return {}
    return _cached(("post", pid, season, group), 300, build)


def _post_qs(pid: int, season: int) -> int:
    def build():
        try:
            data = mlb_api._get(f"/people/{pid}/stats", params={
                "stats": "gameLog", "group": "pitching", "season": season, "gameType": "P"})
        except mlb_api.MlbApiError:
            return 0
        n = 0
        for s in data.get("stats", []):
            for sp in s.get("splits", []):
                st = sp.get("stat", {})
                if _ip_to_outs(st.get("inningsPitched")) >= 18 and (st.get("earnedRuns") or 0) <= 3:
                    n += 1
        return n
    return _cached(("qs", pid, season), 300, build)


def live_lines(lg: dict) -> list[dict]:
    """One stat line per pick from real postseason games."""
    season = int(lg["season"])
    out = []
    for p in lg["picks"]:
        line = dict(p)
        if p["role"] == "hitter":
            st = _post_stats(p["player_id"], season, "hitting")
            line["stats"] = {
                "AB": st.get("atBats", 0) or 0, "H": st.get("hits", 0) or 0,
                "R": st.get("runs", 0) or 0, "HR": st.get("homeRuns", 0) or 0,
                "RBI": st.get("rbi", 0) or 0, "SB": st.get("stolenBases", 0) or 0,
                "PA": st.get("plateAppearances", 0) or 0,
            }
        else:
            st = _post_stats(p["player_id"], season, "pitching")
            outs = _ip_to_outs(st.get("inningsPitched"))
            line["stats"] = {
                "OUTS": outs, "IP": round(outs / 3.0, 1),
                "ER": st.get("earnedRuns", 0) or 0,
                "BB": st.get("baseOnBalls", 0) or 0, "HA": st.get("hits", 0) or 0,
                "K": st.get("strikeOuts", 0) or 0,
                "QS": _post_qs(p["player_id"], season) if (st.get("gamesStarted") or 0) > 0 else 0,
                "SVH": (st.get("saves", 0) or 0) + (st.get("holds", 0) or 0),
            }
        out.append(line)
    return out


def _rank_points(values: dict[str, float | None], low_good: bool) -> dict[str, float]:
    """Roto points for one category. None (no data) ranks worst. Ties split."""
    n = len(values)
    worst = -math.inf if not low_good else math.inf
    keyed = [(v if v is not None else worst, m) for m, v in values.items()]
    keyed.sort(key=lambda t: t[0], reverse=not low_good)
    pts: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and keyed[j + 1][0] == keyed[i][0]:
            j += 1
        share = sum(n - k for k in range(i, j + 1)) / (j - i + 1)
        for k in range(i, j + 1):
            pts[keyed[k][1]] = round(share, 2)
        i = j + 1
    return pts


def roto_standings(lg: dict, lines: list[dict]) -> dict:
    """Category totals + roto points + MVP bonuses -> standings table."""
    mgrs = lg["managers"]
    tot = {m: {"AB": 0, "H": 0, "R": 0, "HR": 0, "RBI": 0, "SB": 0,
               "OUTS": 0, "ER": 0, "BB": 0, "HA": 0, "K": 0, "QS": 0, "SVH": 0}
           for m in mgrs}
    for line in lines:
        t = tot[line["manager"]]
        for k, v in line["stats"].items():
            if k in t:
                t[k] += v
    cats: dict[str, dict[str, float | None]] = {}
    for m in mgrs:
        t = tot[m]
        ip = t["OUTS"] / 3.0
        cats.setdefault("AVG", {})[m] = t["H"] / t["AB"] if t["AB"] else None
        for c in ("R", "HR", "RBI", "SB", "K", "QS", "SVH"):
            cats.setdefault(c, {})[m] = t[c]
        cats.setdefault("ERA", {})[m] = t["ER"] * 9.0 / ip if ip else None
        cats.setdefault("WHIP", {})[m] = (t["BB"] + t["HA"]) / ip if ip else None
    points = {c: _rank_points(vals, c in LOW_IS_GOOD) for c, vals in cats.items()}
    mvp_pts = {m: {a: 0.0 for a in MVP_POINTS} for m in mgrs}
    for award, player in (lg.get("mvp_awards") or {}).items():
        for p in lg["picks"]:
            if norm(p["name"]) == norm(player or ""):
                mvp_pts[p["manager"]][award] = MVP_POINTS[award]
    table = []
    for m in mgrs:
        cat_pts = {c: points[c][m] for c in HIT_CATS + PIT_CATS}
        total = sum(cat_pts.values()) + sum(mvp_pts[m].values())
        ip = tot[m]["OUTS"] / 3.0
        table.append({
            "manager": m, "total": round(total, 2), "cat_points": cat_pts,
            "mvp_points": mvp_pts[m],
            "cat_values": {
                "AVG": round(cats["AVG"][m], 4) if cats["AVG"][m] is not None else None,
                "R": tot[m]["R"], "HR": tot[m]["HR"], "RBI": tot[m]["RBI"], "SB": tot[m]["SB"],
                "ERA": round(cats["ERA"][m], 3) if cats["ERA"][m] is not None else None,
                "WHIP": round(cats["WHIP"][m], 3) if cats["WHIP"][m] is not None else None,
                "K": tot[m]["K"], "QS": tot[m]["QS"], "SVH": tot[m]["SVH"], "IP": round(ip, 1),
            },
        })
    table.sort(key=lambda r: -r["total"])
    for i, r in enumerate(table):
        r["place"] = i + 1
    return {"standings": table, "generated": time.time()}


def projected_standings(lg: dict) -> dict | None:
    """Pre/mid-draft what-if: run the roto engine on PROJECTED stats."""
    if not lg["picks"]:
        return None
    by_role: dict[tuple, dict] = {}  # (player_id, role) — TWP appears as both roles
    for r in player_board(int(lg["season"]), lg.get("odds") or {}, lg.get("ws_probs"),
                          lg.get("fg_ladder")):
        by_role[(r["player_id"], r["role"])] = r
    lines = []
    for p in lg["picks"]:
        r = by_role.get((p["player_id"], p["role"]))
        line = dict(p)
        if not r:
            line["stats"] = {k: 0 for k in (("AB", "H", "R", "HR", "RBI", "SB") if p["role"] == "hitter"
                                            else ("OUTS", "ER", "BB", "HA", "K", "QS", "SVH"))}
        elif p["role"] == "hitter":
            pr = r["proj"]
            line["stats"] = {"AB": pr["AB"], "H": pr["H"], "R": pr["R"], "HR": pr["HR"],
                             "RBI": pr["RBI"], "SB": pr["SB"]}
        else:
            pr = r["proj"]
            outs = pr["IP"] * 3
            line["stats"] = {"OUTS": outs, "ER": pr["ERA"] * pr["IP"] / 9.0,
                             "BB": 0.0, "HA": 0.0, "K": pr["K"], "QS": pr["QS"], "SVH": pr["SVH"]}
            # reconstruct BB+H from WHIP so the WHIP category works
            line["stats"]["HA"] = pr["WHIP"] * pr["IP"]
        lines.append(line)
    return roto_standings(lg, lines)


def team_status(season: int) -> dict:
    """{abbrev: 'alive'|'eliminated'|'champion'} from actual postseason series."""
    def build():
        try:
            data = mlb_api._get("/schedule/postseason/series",
                                params={"season": season, "sportId": 1})
        except mlb_api.MlbApiError:
            return {}
        abbrevs = {t["id"]: t.get("abbreviation") for t in mlb_api.teams()}
        status: dict[str, str] = {}
        for ser in data.get("series", []):
            games = ser.get("games", [])
            wins: dict[int, int] = {}
            teams_in = set()
            for g in games:
                for side in ("home", "away"):
                    t = (g.get("teams", {}).get(side, {}).get("team") or {})
                    if t.get("id"):
                        teams_in.add(t["id"])
                st = (g.get("status") or {}).get("abstractGameState")
                if st == "Final":
                    for side in ("home", "away"):
                        td = g.get("teams", {}).get(side, {})
                        if td.get("isWinner"):
                            wins[td.get("team", {}).get("id")] = wins.get(td.get("team", {}).get("id"), 0) + 1
            need = {"F": 2, "D": 3, "L": 4, "W": 4}.get(ser.get("series", {}).get("gameType"), 4)
            for tid in teams_in:
                ab = abbrevs.get(tid)
                if not ab:
                    continue
                status.setdefault(ab, "alive")
            for tid, w in wins.items():
                if w >= need:
                    winner_ab = abbrevs.get(tid)
                    for tid2 in teams_in:
                        if tid2 != tid:
                            status[abbrevs.get(tid2, "?")] = "eliminated"
                    if ser.get("series", {}).get("gameType") == "W":
                        status[winner_ab] = "champion"
        return status
    return _cached(("status", season), 300, build)
