#!/bin/zsh
# Weekly self-running calibration audit. Builds the trailing 10 days locally
# (leak-free, prod box untouched), then writes a markdown report with:
#   - overall + bucket bias/MAE (form, magnitude, L3-deviation)
#   - forward-validation verdicts for the new factors (TB-prop, platoon, outs-prop)
# Installed via cron (Mon 08:30). Reports land in data/audit_reports/.
set -e
REPO=/Users/doronschwartz/mlb_dfs
cd "$REPO"
source .venv/bin/activate 2>/dev/null || true

END=$(date -v-1d +%Y-%m-%d)            # through yesterday
START=$(date -v-10d +%Y-%m-%d)
STAMP=$(date +%Y-%m-%d)
OUT_DIR="$REPO/data/audit_reports"
mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/$STAMP.md"

echo "weekly audit $START..$END" > "$REPORT.log"
# Server build (NOT local): market factors (TB-prop, outs-prop) only exist in
# server-computed rows — the local box has no ODDS_API_KEY. Recent dates are
# warm-cached server-side from daily league usage, so this is cheap; a cold
# date just gets skipped by the builder's retry logic.
ROWS_START="$START" ROWS_END="$END" ROWS_OUT=/tmp/rows_weekly.json \
  python scripts/build_rows.py >> "$REPORT.log" 2>&1

python - "$START" "$END" "$REPORT" << 'EOF' >> "$REPORT.log" 2>&1
import json, sys, numpy as np
start, end, report = sys.argv[1], sys.argv[2], sys.argv[3]
rows = json.load(open("/tmp/rows_weekly.json"))
def num(x):
    try:
        v = float(x); return v if np.isfinite(v) else None
    except Exception: return None
L = ["# Weekly calibration audit — %s..%s\n" % (start, end)]
for role in ("hitter", "pitcher"):
    sub = [(num(r["chain_proj"]), num(r["actual"]), r) for r in rows if r["role"] == role]
    sub = [(p, a, r) for p, a, r in sub if p is not None and a is not None]
    d = np.array([a - p for p, a, r in sub])
    L.append("\n## %s (n=%d): bias %+.3f · MAE %.3f\n" % (role, len(d), d.mean(), np.abs(d).mean()))
    def bucket(title, key):
        b = {}
        for p, a, r in sub:
            b.setdefault(key(p, a, r), []).append(a - p)
        L.append("\n**%s**\n" % title)
        L.append("| bucket | n | bias | σ-from-0 |\n|---|---|---|---|")
        flagged = False
        for k in sorted(b, key=str):
            arr = np.array(b[k])
            if len(arr) < 25: continue
            se = arr.std() / np.sqrt(len(arr))
            sig = abs(arr.mean() / se) if se else 0
            flag = " ⚠️" if sig >= 3 else ""
            flagged = flagged or sig >= 3
            L.append("| %s | %d | %+.2f | %.1f%s |" % (k, len(arr), arr.mean(), sig, flag))
        if flagged:
            L.append("\n→ **bucket ≥3σ — investigate / consider a ratchet**")
    bucket("by form tag", lambda p, a, r: r.get("cat_form_tag") or "-")
    bucket("by magnitude", lambda p, a, r: "0-4" if p < 4 else "4-7" if p < 7 else "7-10" if p < 10 else "10+")
    if role == "hitter":
        def l3dev(p, a, r):
            l3, b3 = num(r.get("pg_l3")), num(r.get("base_pg"))
            if l3 is None or b3 is None: return "no L3"
            dd = l3 - b3
            return "L3<<base" if dd < -4 else "L3<base" if dd < -1 else "L3~base" if dd <= 1 else "L3>base" if dd <= 4 else "L3>>base"
        bucket("by L3 deviation (v9.42 watches this)", l3dev)
        def tbz(p, a, r):
            z = num(r.get("tb_prop_z"))
            return "no prop" if z is None else ("z<-0.8" if z < -0.8 else "z>+0.8" if z > 0.8 else "z mid")
        bucket("TB-prop forward validation", tbz)
        def plat(p, a, r):
            f = num(r.get("platoon_factor"))
            return "-" if f is None else ("≤0.95" if f <= 0.95 else "≥1.05" if f >= 1.05 else "~1")
        bucket("platoon forward validation", plat)
    else:
        def outsb(p, a, r):
            adj = num(r.get("outs_prop_adj"))
            return "no line" if adj is None else ("adj<0" if adj < -0.3 else "adj>0" if adj > 0.3 else "~0")
        bucket("outs-prop forward validation", outsb)
open(report, "w").write("\n".join(L) + "\n")
print("report written:", report)
EOF
echo "done $(date)" >> "$REPORT.log"
