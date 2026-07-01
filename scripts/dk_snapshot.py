"""Snapshot today's DraftKings MLB salaries → data/dk_salaries/YYYY-MM-DD.json.

DK salaries are the market's implied projection — the external benchmark this
model never had (critique #22). Run daily (wired into daily_health launchd);
scripts/dk_benchmark.py grades us against them once actuals land.
Idempotent: first snapshot of the day wins.
"""
import json, os, sys, urllib.request
from datetime import date as D

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "dk_salaries")


def fetch():
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
           "Accept": "application/json"}
    req = urllib.request.Request("https://www.draftkings.com/lobby/getcontests?sport=MLB", headers=hdr)
    d = json.load(urllib.request.urlopen(req, timeout=30))
    groups = {}
    for c in d.get("Contests", []):
        dg = c.get("dg")
        if dg:
            groups[dg] = groups.get(dg, 0) + 1
    if not groups:
        return None
    dg = max(groups, key=groups.get)  # main slate = most contests
    req2 = urllib.request.Request(
        f"https://api.draftkings.com/draftgroups/v1/draftgroups/{dg}/draftables", headers=hdr)
    dd = json.load(urllib.request.urlopen(req2, timeout=30))
    out = {}
    for p in dd.get("draftables", []):
        nm, sal = p.get("displayName"), p.get("salary")
        if nm and sal:
            out.setdefault(nm, {"salary": sal, "position": p.get("position")})
    return {"draft_group": dg, "players": out}


if __name__ == "__main__":
    today = D.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{today}.json")
    if os.path.exists(path):
        print(f"already snapshotted {today}")
        sys.exit(0)
    snap = fetch()
    if not snap or len(snap["players"]) < 100:
        print("DK fetch thin/failed — not writing")
        sys.exit(1)
    json.dump(snap, open(path, "w"))
    print(f"{today}: {len(snap['players'])} salaries (group {snap['draft_group']})")
