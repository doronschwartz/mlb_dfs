"""Grade our projections against DraftKings salaries (the market benchmark).

For every archived DK-salary date with completed games:
  - join salaries ↔ our calibration rows (name-normalized)
  - Spearman(our proj, actual) vs Spearman(DK salary, actual)
  - MAE of ours vs a salary-implied projection (linear fit salary→pts,
    fit per-date so DK gets the fairest possible shake)

    python scripts/dk_benchmark.py
"""
import json, os, unicodedata, urllib.request
import numpy as np

DK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "dk_salaries")


def norm(n):
    d = unicodedata.normalize("NFD", n or "")
    a = "".join(c for c in d if not unicodedata.combining(c)).lower().replace(".", "").replace("'", "")
    return " ".join(t for t in a.split() if t not in ("jr", "sr", "ii", "iii", "iv"))


def spear(x, y):
    rx = np.argsort(np.argsort(x)); ry = np.argsort(np.argsort(y))
    return float(np.corrcoef(rx, ry)[0, 1])


def main():
    dates = sorted(f[:-5] for f in os.listdir(DK_DIR) if f.endswith(".json")) if os.path.isdir(DK_DIR) else []
    if not dates:
        print("no DK snapshots yet — they accrue daily via daily_health")
        return
    all_ours, all_dk = [], []
    for d in dates:
        snap = json.load(open(os.path.join(DK_DIR, f"{d}.json")))
        sal = {norm(k): v["salary"] for k, v in snap["players"].items()}
        try:
            cal = json.load(urllib.request.urlopen(
                f"https://mlb-dfs-doron.fly.dev/api/calibration?date={d}", timeout=300))
        except Exception as e:
            print(f"  {d}: calibration fetch failed ({str(e)[:60]})")
            continue
        rows = []
        for r in cal.get("rows", []):
            s = sal.get(norm(r.get("name")))
            if s and r.get("actual") is not None and r.get("projected") is not None:
                rows.append((float(r["projected"]), float(s), float(r["actual"])))
        if len(rows) < 80:
            print(f"  {d}: only {len(rows)} joined — skip")
            continue
        P = np.array([x[0] for x in rows]); S = np.array([x[1] for x in rows]); A = np.array([x[2] for x in rows])
        # salary-implied projection: per-date linear fit (generous to DK)
        b, a0 = np.polyfit(S, A, 1)
        dk_proj = a0 + b * S
        print(f"  {d}: n={len(rows)} | Spearman ours={spear(P, A):.3f} DK={spear(S, A):.3f} | "
              f"MAE ours={np.abs(A - P).mean():.3f} DK-implied={np.abs(A - dk_proj).mean():.3f}")
        all_ours.append((P, A)); all_dk.append((dk_proj, S, A))
    if all_ours:
        P = np.concatenate([x[0] for x in all_ours]); A = np.concatenate([x[1] for x in all_ours])
        D_ = np.concatenate([x[0] for x in all_dk]); S = np.concatenate([x[1] for x in all_dk])
        print(f"\nOVERALL n={len(P)}: Spearman ours={spear(P, A):.3f} DK={spear(S, A):.3f} | "
              f"MAE ours={np.abs(A - P).mean():.3f} DK-implied={np.abs(A - D_).mean():.3f}")


if __name__ == "__main__":
    main()
