"""Replay the 2025 Fantasy Postseason spreadsheet through the postseason
engine: resolve the 144 drafted names to MLB ids, pull real 2025 postseason
stats, run the roto scoring, and diff against the sheet's final standings.

Usage: python3 scripts/validate_postseason_2025.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mlb_dfs import mlb_api, postseason as ps  # noqa: E402

LEAGUE_JSON = os.path.join(os.path.dirname(__file__), "..", "mlb_dfs", "data", "postseason_2025.json")
ID_CACHE = "/tmp/postseason_2025_ids.json"


def resolve(name: str, role: str) -> dict | None:
    clean = name.replace("(P)", "").strip()
    data = mlb_api._get("/people/search", params={"names": clean, "sportIds": 1, "active": "true"})
    cands = data.get("people", [])
    if not cands:  # initials like "JT"/"JP" only match with periods, or player inactive
        for q in (clean, ". ".join(clean.split()[0]) + ". " + " ".join(clean.split()[1:])):
            data = mlb_api._get("/people/search", params={"names": q, "sportIds": 1})
            cands = data.get("people", [])
            if cands:
                break
    exact = [c for c in cands if ps.norm(c.get("fullName", "")) == ps.norm(clean)]
    pool = exact or cands
    if not pool:
        return None

    def is_pitcher(c):
        return (c.get("primaryPosition") or {}).get("type") == "Pitcher"
    pref = [c for c in pool if is_pitcher(c) == (role == "pitcher")]
    # Two-way players (Ohtani) count for either role
    pref = pref or [c for c in pool if (c.get("primaryPosition") or {}).get("code") == "Y"] or pool
    c = pref[0]
    return {"id": c["id"], "name": c["fullName"],
            "position": (c.get("primaryPosition") or {}).get("abbreviation", "")}


def main():
    raw = json.load(open(LEAGUE_JSON))
    cache = json.load(open(ID_CACHE)) if os.path.exists(ID_CACHE) else {}
    picks = []
    misses = []
    for p in raw["picks"]:
        role = "pitcher" if p["slot"] in ps.PIT_SLOTS else "hitter"
        key = f"{p['name']}|{role}"
        if cache.get(key) is None:
            cache[key] = resolve(p["name"], role)
        r = cache[key]
        if not r:
            misses.append(p["name"])
            continue
        picks.append({"manager": p["manager"], "slot": p["slot"], "player_id": r["id"],
                      "name": p["name"], "team_id": 0, "team": "", "position": r["position"],
                      "role": role, "pick_number": len(picks) + 1})
    json.dump(cache, open(ID_CACHE, "w"))
    print(f"resolved {len(picks)}/{len(raw['picks'])} picks; misses: {misses}")

    lg = {"season": 2025, "managers": raw["managers"], "slots": ps.DEFAULT_SLOTS,
          "picks": picks, "odds": {}, "mvp_awards": raw["mvp_awards"]}

    # Warm the postseason stat cache in parallel (live_lines itself is sequential)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for p in picks:
            grp = "pitching" if p["role"] == "pitcher" else "hitting"
            ex.submit(ps._post_stats, p["player_id"], 2025, grp)
    lines = ps.live_lines(lg)
    result = ps.roto_standings(lg, lines)

    sheet = raw["sheet_standings"]
    print(f"\n{'manager':10} {'engine':>7} {'sheet':>6} {'diff':>6}")
    for row in result["standings"]:
        m = row["manager"]
        diff = row["total"] - sheet.get(m, 0)
        flag = "" if abs(diff) < 0.01 else "  <-- MISMATCH"
        print(f"{m:10} {row['total']:7.1f} {sheet.get(m, 0):6.1f} {diff:+6.1f}{flag}")
        print(f"           cats={row['cat_values']}")


if __name__ == "__main__":
    main()
