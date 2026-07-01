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
import urllib.request, unicodedata, math
def _rev():
    try:
        return json.load(urllib.request.urlopen("https://mlb-dfs-doron.fly.dev/api/changelog", timeout=20)).get("current","?")
    except Exception:
        return "?"
L = ["# Weekly calibration audit — %s..%s — MODEL_REV %s\n" % (start, end, _rev())]
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
    # --- rank quality + band coverage (tracked weekly since v9.48) ---
    P = np.array([p for p, a, r in sub]); A = np.array([a for p, a, r in sub])
    rp = np.argsort(np.argsort(P)); ra = np.argsort(np.argsort(A))
    rho = float(np.corrcoef(rp, ra)[0, 1]) if len(P) > 10 else 0
    if role == "hitter":
        flo = np.maximum(-3.0, P - (2.60 + 0.712 * np.maximum(P, 0))); cei = P + 4.88 + 0.459 * np.maximum(P, 0)
    else:
        flo = np.maximum(-8.0, P - (6.5 + 0.25 * np.maximum(P, 0))); cei = P + 9.1 + 0.046 * np.maximum(P, 0)
    inside = float(np.mean((A >= flo) & (A <= cei)))
    L.append("\n**Rank & bands:** Spearman %.3f · band coverage %.0f%% (target ~80%%)\n" % (rho, 100 * inside))
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
# --- market-factor grading from the permanent prop archive (v9.48) ---
def norm(n):
    d2 = unicodedata.normalize("NFD", n or "")
    a2 = "".join(c for c in d2 if not unicodedata.combining(c)).lower().replace(".", "").replace("'", "")
    return " ".join(t for t in a2.split() if t not in ("jr", "sr", "ii", "iii", "iv"))
def fetch(u):
    try:
        return json.load(urllib.request.urlopen(u, timeout=60))
    except Exception:
        return None
def amer(o):
    return 100.0 / (o + 100.0) if o > 0 else (-o) / ((-o) + 100.0)
tb_rows = []
dates_all = sorted({r["date"] for r in rows})
for dd in dates_all:
    arch = fetch("https://mlb-dfs-doron.fly.dev/api/prop_archive/%s?market=batters" % dd)
    lines = (arch or {}).get("lines") or {}
    if len(lines) < 20:
        continue
    scal = {}
    for nm, info in lines.items():
        if not isinstance(info, dict) or info.get("book_count", 0) < 2:
            continue
        ln, oo, uo = info.get("line"), info.get("over_odds"), info.get("under_odds")
        if ln is None or oo is None or uo is None:
            continue
        po, pu = amer(int(oo)), amer(int(uo))
        scal[norm(nm)] = float(ln) + (po / (po + pu) - 0.5) * 3.5
    if len(scal) < 30:
        continue
    vals = list(scal.values()); mu = sum(vals) / len(vals)
    sd = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5 or 1.0
    for r in rows:
        if r["date"] != dd or r["role"] != "hitter":
            continue
        z = scal.get(norm(r.get("name") or ""))
        if z is None:
            continue
        p, a = num(r["chain_proj"]), num(r["actual"])
        if p is None or a is None:
            continue
        tb_rows.append(((z - mu) / sd, a - p))
L.append("\n## TB-prop archive grading (n=%d prop-covered hitter-games)\n" % len(tb_rows))
if len(tb_rows) >= 60:
    L.append("| market-z bucket | n | resid bias |\n|---|---|---|")
    for lo, hi, lab in ((-9, -0.8, "z<-0.8"), (-0.8, 0.8, "mid"), (0.8, 9, "z>+0.8")):
        b = [r2 for z2, r2 in tb_rows if lo <= z2 < hi]
        if len(b) >= 15:
            L.append("| %s | %d | %+.2f |" % (lab, len(b), sum(b) / len(b)))
    L.append("\n→ positive gradient (high-z outperforming) = the TB factor is under-weighted; flat = market adds nothing beyond the chain.")
else:
    L.append("_Not enough archived prop days in this window yet — accrues daily (quota permitting)._")
open(report, "w").write("\n".join(L) + "\n")
print("report written:", report)
EOF
echo "done $(date)" >> "$REPORT.log"
