"""Counterfactual A/B for the v9.39/v9.40 factors — TODAY, not in a week.

The chain stores every factor separately in components, and the post-chain
transforms (v9.35 compression, v9.36-38 shrinks) are exactly invertible. So
from a dataset built under live v9.40 we can reconstruct, for every row, what
the projection WOULD have been without the new factors:

    pre  = base_pg × chain_product            (components, includes hot/cold)
    comp = 5.6 + (pre − 5.6) × 0.85           (v9.35)
    post = comp × 0.81 if COLD                 (v9.36/38)
           comp × 0.88 if pg_l3<4 & games_l3≥2 (v9.37)
    → must equal chain_proj (identity check, tolerance 0.02)

Counterfactual: divide arsenal/tb_prop out of chain_product, swap the
personalized platoon back to the old static (±5% / 1.03 switch), re-apply
the transform. Compare on real actuals.

LEAK CAVEAT (stated, not hidden): arsenal + platoon inputs are season-
cumulative-to-today, so past-date factors see ~10-25% future data. The test
is therefore asymmetric: factors that FAIL even with this tailwind are
decisively bad; factors that pass get kept at current damping and confirmed
by the (clean) forward validation as slates accrue.
"""
import json, os
import numpy as np

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "backtest_rows.json")
PIV, K = 5.6, 0.85


def num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except Exception:
        return None


def transform(pre, form_tag, pg3, g3):
    p = PIV + (pre - PIV) * K
    if form_tag == "COLD":
        p *= 0.81
    elif pg3 is not None and pg3 < 4 and g3 is not None and g3 >= 2:
        p *= 0.88
    return p


def static_platoon(bats, throws):
    if not bats or not throws or bats not in ("L", "R", "S") or throws not in ("L", "R"):
        return 1.0
    if bats == "S":
        return 1.03
    return 1.05 if bats != throws else 0.95


def main():
    rows = json.load(open(PATH))
    H = [r for r in rows if r["role"] == "hitter"]
    ok, mismatch = [], 0
    for r in H:
        base = num(r.get("base_pg")); cpc = num(r.get("chain_product"))
        proj = num(r.get("chain_proj")); act = num(r.get("actual"))
        if base is None or cpc is None or proj is None or act is None:
            continue
        pg3, g3 = num(r.get("pg_l3")), num(r.get("games_l3"))
        tag = r.get("cat_form_tag") or ""
        rebuilt = transform(base * cpc, tag, pg3, g3)
        if abs(rebuilt - proj) > 0.02 * max(1.0, abs(proj)):
            mismatch += 1
            continue  # lineup-out rows, openers, anything non-invertible
        ok.append(r)
    print("hitter rows: %d usable / %d mismatched-skipped" % (len(ok), mismatch))
    dates = sorted({r["date"] for r in ok})
    print("dates: %s..%s" % (dates[0], dates[-1]))

    def project(r, *, arsenal=True, tb=True, platoon_personal=True):
        base = num(r["base_pg"]); cpc = num(r["chain_product"])
        af = num(r.get("arsenal_factor")) or 1.0
        tf = num(r.get("tb_prop_factor")) or 1.0
        pf = num(r.get("platoon_factor")) or 1.0
        sf = static_platoon(r.get("bats"), r.get("vs_throws"))
        if not arsenal and af:
            cpc = cpc / af
        if not tb and tf:
            cpc = cpc / tf
        if not platoon_personal and pf:
            cpc = cpc / pf * sf
        return transform(base * cpc, r.get("cat_form_tag") or "", num(r.get("pg_l3")), num(r.get("games_l3")))

    variants = [
        ("v9.40 (all factors)", dict()),
        ("baseline (none new)", dict(arsenal=False, tb=False, platoon_personal=False)),
        ("only arsenal", dict(arsenal=True, tb=False, platoon_personal=False)),
        ("only TB-prop", dict(arsenal=False, tb=True, platoon_personal=False)),
        ("only personal platoon", dict(arsenal=False, tb=False, platoon_personal=True)),
    ]
    print("\n%-24s %8s %9s" % ("variant", "bias", "mae"))
    for label, kw in variants:
        d = np.array([num(r["actual"]) - project(r, **kw) for r in ok])
        print("%-24s %+8.3f %9.4f" % (label, d.mean(), np.abs(d).mean()))

    # gradient check per factor: does the factor's direction predict residual
    # of the BASELINE (no-new-factors) projection?
    print("\nGradient vs baseline residual (positive bias in 'high' bucket = factor points the right way):")
    base_resid = {id(r): num(r["actual"]) - project(r, arsenal=False, tb=False, platoon_personal=False) for r in ok}
    for fname, getter in (("arsenal_factor", lambda r: num(r.get("arsenal_factor"))),
                          ("tb_prop_z", lambda r: num(r.get("tb_prop_z"))),
                          ("platoon shift", lambda r: (num(r.get("platoon_factor")) or 1.0) - static_platoon(r.get("bats"), r.get("vs_throws")))):
        lo = [base_resid[id(r)] for r in ok if (getter(r) is not None) and getter(r) < (-0.012 if fname == "platoon shift" else (0.99 if fname == "arsenal_factor" else -0.8))]
        hi = [base_resid[id(r)] for r in ok if (getter(r) is not None) and getter(r) > (0.012 if fname == "platoon shift" else (1.01 if fname == "arsenal_factor" else 0.8))]
        if len(lo) > 30 and len(hi) > 30:
            lo, hi = np.array(lo), np.array(hi)
            se = (lo.std()**2/len(lo) + hi.std()**2/len(hi)) ** 0.5
            print("  %-16s low n=%4d bias=%+.3f | high n=%4d bias=%+.3f | gap=%+.3f (%.1fσ)" % (
                fname, len(lo), lo.mean(), len(hi), hi.mean(), hi.mean()-lo.mean(), abs(hi.mean()-lo.mean())/se if se else 0))
        else:
            print("  %-16s insufficient bucket sizes (lo=%d hi=%d)" % (fname, len(lo), len(hi)))


if __name__ == "__main__":
    main()
