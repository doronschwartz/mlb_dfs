"""Pull MLB pitcher strikeout prop lines from the-odds-api.com.

Requires an API key (free tier covers ~500 requests/month):
    fly secrets set ODDS_API_KEY=...   (in production)
    export ODDS_API_KEY=...            (locally)

Each event-odds call costs 1 credit per market. We cache for 10 minutes
so that a tab refresh doesn't re-burn credits.
"""

from __future__ import annotations

import os
import time
from statistics import median

import requests

BASE = "https://api.the-odds-api.com/v4"

_CACHE: dict[str, tuple[float, object]] = {}
_TTL_SEC = 600  # 10 min


def is_configured() -> bool:
    return bool(os.environ.get("ODDS_API_KEY"))


def _key() -> str:
    return os.environ.get("ODDS_API_KEY", "")


def _get(path: str, params: dict | None = None):
    if not _key():
        raise RuntimeError("ODDS_API_KEY not configured")
    p = dict(params or {})
    p["apiKey"] = _key()
    cache_key = f"{path}?{sorted((k, v) for k, v in p.items() if k != 'apiKey')}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < _TTL_SEC:
        return cached[1]
    r = requests.get(f"{BASE}{path}", params=p, timeout=12)
    r.raise_for_status()
    data = r.json()
    _CACHE[cache_key] = (now, data)
    return data


def get_pitcher_strikeout_lines(date_iso: str) -> dict[str, dict]:
    """Returns {pitcher_name: {line, over_odds, under_odds, book_count}}.

    Strategy: list events for the date, then for each event pull
    pitcher_strikeouts player-prop market across US books. For each
    pitcher, pick the line offered by the most books and median the
    over/under odds across those books.
    """
    if not is_configured():
        return {}
    events = _get("/sports/baseball_mlb/events")
    today_events = [
        e for e in events
        if str(e.get("commence_time", "")).startswith(date_iso)
    ]

    out: dict[str, dict] = {}
    for event in today_events:
        eid = event.get("id")
        if not eid:
            continue
        try:
            data = _get(
                f"/sports/baseball_mlb/events/{eid}/odds",
                params={
                    "markets": "pitcher_strikeouts",
                    "regions": "us",
                    "oddsFormat": "american",
                },
            )
        except Exception:
            continue

        # per_pitcher[name][line_value] -> {over_odds: [...], under_odds: [...], books: set()}
        per_pitcher: dict[str, dict[float, dict]] = {}
        for bm in data.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "pitcher_strikeouts":
                    continue
                for outcome in mkt.get("outcomes", []):
                    name = outcome.get("description")
                    line = outcome.get("point")
                    side = outcome.get("name")
                    odds = outcome.get("price")
                    if not name or line is None or odds is None:
                        continue
                    pp = per_pitcher.setdefault(name, {})
                    entry = pp.setdefault(float(line), {
                        "over_odds": [], "under_odds": [], "books": set(),
                    })
                    entry["books"].add(bm.get("key"))
                    if side == "Over":
                        entry["over_odds"].append(int(odds))
                    elif side == "Under":
                        entry["under_odds"].append(int(odds))

        for name, lines in per_pitcher.items():
            if not lines:
                continue
            # Most-popular line wins.
            best_line = max(lines.keys(), key=lambda L: len(lines[L]["books"]))
            entry = lines[best_line]
            out[name] = {
                "line": best_line,
                "over_odds": int(median(entry["over_odds"])) if entry["over_odds"] else None,
                "under_odds": int(median(entry["under_odds"])) if entry["under_odds"] else None,
                "book_count": len(entry["books"]),
            }
    return out
