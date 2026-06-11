"""Merge local chunk files (+ any per-date retry files) into data/backtest_rows.json."""
import glob, json, os, sys

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "backtest_rows.json")
rows = []
seen_dates = set()
for path in sorted(glob.glob("/tmp/rows_chunk_*.json")) + sorted(glob.glob("/tmp/rows_retry_*.json")):
    try:
        chunk = json.load(open(path))
    except Exception as e:
        print("skip %s: %s" % (path, e)); continue
    dates = sorted({r["date"] for r in chunk})
    fresh = [r for r in chunk if r["date"] not in seen_dates]
    seen_dates.update(r["date"] for r in fresh)
    rows.extend(fresh)
    print("%s: %d rows (%s..%s)%s" % (os.path.basename(path), len(fresh),
          dates[0] if dates else "-", dates[-1] if dates else "-",
          " [dup dates dropped]" if len(fresh) < len(chunk) else ""))
rows.sort(key=lambda r: r["date"])
json.dump(rows, open(OUT, "w"))
print("\nTOTAL %d rows, %d dates -> %s" % (len(rows), len(seen_dates), OUT))
missing = []
from datetime import date as D, timedelta
d = D(2026, 5, 17)
while d <= D(2026, 6, 10):
    if d.isoformat() not in seen_dates:
        missing.append(d.isoformat())
    d += timedelta(days=1)
print("missing dates:", missing or "none")
