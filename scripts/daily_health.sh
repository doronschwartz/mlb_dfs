#!/bin/zsh
# Daily system health check (v9.48). Would have caught the June 13-30 odds
# quota blackout in ONE day instead of 17. Checks, via the live app:
#   1. odds-api credits remaining (alert < 60 — ~1.5 days of full-market burn)
#   2. market data present on today's slate (Vegas totals fetched)
#   3. yesterday's prop archive actually wrote to the volume
#   4. app health endpoints answering
# On any failure: writes HEALTH-ALERT.txt + fires a macOS notification.
# Runs from launchd (com.diamondmodel.daily-health, 10:00 daily).
set -u
REPO=/Users/doronschwartz/mlb_dfs
OUT="$REPO/data/audit_reports"
mkdir -p "$OUT"
LOG="$OUT/health.log"
TODAY=$(date +%Y-%m-%d)
YDAY=$(date -v-1d +%Y-%m-%d)
FAILS=()

note() { echo "[$(date '+%F %T')] $1" >> "$LOG"; }

DIAG=$(curl -s -m 30 "https://mlb-dfs-doron.fly.dev/api/diag/odds" 2>/dev/null)
CREDITS=$(echo "$DIAG" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('credits',{}).get('remaining','none'))" 2>/dev/null || echo "parse-fail")
TOTALS=$(echo "$DIAG" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('team_totals_count',0))" 2>/dev/null || echo 0)

if [[ "$CREDITS" =~ ^[0-9.]+$ ]]; then
  if (( ${CREDITS%.*} < 60 )); then
    FAILS+=("odds-api credits low: $CREDITS remaining — market data dies soon (free tier: upgrade or lean mode)")
  fi
else
  # 'None' (no fetch yet today), 'parse-fail', or anything non-numeric —
  # never let this reach arithmetic under set -u (crashed 2026-07-03).
  note "credits unknown ($CREDITS)"
fi
if (( TOTALS < 5 )); then
  FAILS+=("only $TOTALS Vegas team totals for $TODAY — market data likely absent (quota/key?)")
fi

ARCH=$(curl -s -m 30 "https://mlb-dfs-doron.fly.dev/api/prop_archive/$YDAY?market=batters" 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d.get('lines',{})))" 2>/dev/null || echo 0)
if (( ARCH < 10 )); then
  FAILS+=("prop archive for $YDAY has $ARCH batter lines — archival not accruing")
fi

for APP in mlb-dfs-doron mlb-dfs-public; do
  OK=$(curl -s -m 20 "https://$APP.fly.dev/api/health" -o /dev/null -w "%{http_code}" 2>/dev/null)
  [[ "$OK" == "200" ]] || FAILS+=("$APP /api/health returned $OK")
done

# Pre-warm today's slate (cold compute pegs the box and fails Fly health
# checks right when the league wakes — seen 2026-07-03 07:51/08:32). This
# runs at 10:00 London = 05:00 ET, well before anyone drafts.
curl -s -m 400 "https://mlb-dfs-doron.fly.dev/api/projections?date=$TODAY" -o /dev/null 2>&1
note "slate pre-warmed for $TODAY"

# Warm the server's rolling-xwOBA dailies (yesterday + stragglers). ~30s.
curl -s -m 120 "https://mlb-dfs-doron.fly.dev/api/admin/xwoba_warm?days=3" >> "$LOG" 2>&1; echo >> "$LOG"

# DK salary snapshot (external benchmark — critique #22). Quiet, idempotent.
cd "$REPO" && source .venv/bin/activate 2>/dev/null && python scripts/dk_snapshot.py >> "$LOG" 2>&1

if (( ${#FAILS[@]} > 0 )); then
  {
    echo "DIAMOND MODEL HEALTH ALERT — $TODAY"
    for f in "${FAILS[@]}"; do echo "  ✗ $f"; done
  } > "$OUT/HEALTH-ALERT.txt"
  note "ALERT (${#FAILS[@]} failures): ${FAILS[*]}"
  osascript -e "display notification \"${FAILS[1]}\" with title \"⚠️ Diamond Model health\"" 2>/dev/null || true
else
  rm -f "$OUT/HEALTH-ALERT.txt"
  note "OK — credits=$CREDITS totals=$TOTALS archive[$YDAY]=$ARCH"
fi
