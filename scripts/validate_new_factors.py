"""Forward-validation for the v9.39/v9.40 factors (TB-prop, arsenal matchup,
personalized platoon). These can't be backtested (no historical prop archive;
season-cumulative leaderboards leak on past dates), so we validate FORWARD:
after ~a week of post-deploy dates, rebuild the row table over those dates
(scripts/build_rows.py with START >= deploy date) and run this.

For each new factor it decomposes held-out error by factor bucket. The factor
EARNS ITS KEEP if high buckets out-perform low buckets in actual-vs-projection
terms (positive bias gradient means the factor is under-damped → ratchet up;
flat means no signal → consider removing; negative means harmful → remove).

    python scripts/build_rows.py   # with June post-deploy window
    python scripts/validate_new_factors.py
"""
import json, os, sys
import numpy as np

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "backtest_rows.json")
DEPLOY_DATE = "2026-06-11"   # v9.39/v9.40 went live — only later dates count


def num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except Exception:
        return None


def decomp(rows, label, bucket_fn):
    b = {}
    for r in rows:
        a, p = num(r.get("actual")), num(r.get("chain_proj"))
        if a is None or p is None:
            continue
        b.setdefault(bucket_fn(r), []).append(a - p)
    print("\n=== %s ===" % label)
    print("  %-16s %6s %8s %8s %6s" % ("bucket", "n", "bias", "mae", "σ"))
    for k in sorted(b, key=str):
        d = np.array(b[k])
        if len(d) < 15:
            continue
        se = d.std() / np.sqrt(len(d)) if len(d) > 1 else 0
        print("  %-16s %6d %+8.2f %8.2f %6.1f" % (
            k, len(d), d.mean(), np.abs(d).mean(), abs(d.mean() / se) if se else 0))


def tb_bucket(r):
    z = num(r.get("tb_prop_z"))
    if z is None: return "no prop"
    if z < -0.8: return "z < -0.8"
    if z > 0.8: return "z > +0.8"
    return "z mid"


def arsenal_bucket(r):
    f = num(r.get("arsenal_factor"))
    if f is None or f == 1.0: return "neutral/none"
    if f <= 0.985: return "bad matchup"
    if f >= 1.015: return "good matchup"
    return "mild"


def platoon_bucket(r):
    f = num(r.get("platoon_factor"))
    if f is None: return "none"
    if f <= 0.94: return "f <= 0.94"
    if f < 0.99: return "0.94-0.99"
    if f <= 1.01: return "~1.0"
    if f < 1.06: return "1.01-1.06"
    return "f >= 1.06"


if __name__ == "__main__":
    rows = json.load(open(PATH))
    rows = [r for r in rows if r.get("date", "") >= DEPLOY_DATE and r["role"] == "hitter"]
    if len(rows) < 300:
        print("Only %d post-%s hitter rows — wait for more slates before judging." % (len(rows), DEPLOY_DATE))
        sys.exit(0)
    print("post-deploy hitter rows: %d (dates %s..%s)" % (
        len(rows), min(r["date"] for r in rows), max(r["date"] for r in rows)))
    decomp(rows, "TB-prop (positive bias gradient with z → under-damped)", tb_bucket)
    decomp(rows, "Arsenal matchup (good should out-bias bad)", arsenal_bucket)
    decomp(rows, "Personalized platoon", platoon_bucket)
