"""Pull MLB pitcher strikeout prop lines from the-odds-api.com.

Requires an API key (free tier covers ~500 requests/month):
    fly secrets set ODDS_API_KEY=...   (in production)
    export ODDS_API_KEY=...            (locally)

Each event-odds call costs 1 credit per market. We cache for 10 minutes
so that a tab refresh doesn't re-burn credits.
"""

from __future__ import annotations

import json
import os
import time
from statistics import median

import requests

BASE = "https://api.the-odds-api.com/v4"

_CACHE: dict[str, tuple[float, object]] = {}
_TTL_SEC = 600  # 10 min

# Saved-odds-per-day directory. Lives on the Fly volume next to drafts/.
ODDS_DIR = os.environ.get(
    "MLB_DFS_ODDS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "odds"),
)


def _ensure_odds_dir() -> None:
    os.makedirs(ODDS_DIR, exist_ok=True)


def _odds_path(date_iso: str) -> str:
    return os.path.join(ODDS_DIR, f"{date_iso}.json")


def saved_odds(date_iso: str) -> dict | None:
    """Returns {fetched_at, date, pitchers} from disk if previously saved."""
    path = _odds_path(date_iso)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def save_odds(date_iso: str, pitchers: dict) -> None:
    _ensure_odds_dir()
    payload = {
        "fetched_at": time.time(),
        "date": date_iso,
        "pitchers": pitchers,
    }
    path = _odds_path(date_iso)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def cleanup_old_odds(keep_from: str) -> int:
    """Delete saved-odds files for dates strictly before `keep_from`.
    Returns the count removed. Called automatically on every fetch so we
    never accumulate yesterday's lines."""
    _ensure_odds_dir()
    removed = 0
    for fn in os.listdir(ODDS_DIR):
        if not fn.endswith(".json"):
            continue
        date_part = fn[:-5]
        if date_part < keep_from:
            try:
                os.remove(os.path.join(ODDS_DIR, fn))
                removed += 1
            except OSError:
                pass
    return removed


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


def get_pitcher_strikeout_lines_cached(date_iso: str, *, force_refresh: bool = False) -> tuple[dict[str, dict], dict]:
    """Cache-first: if a saved file exists for this date and force_refresh
    is False, return it. Otherwise hit the API and persist the result.
    Always runs cleanup_old_odds(date_iso) so yesterday's file is cleared.

    Returns (pitchers, meta) where meta = {cached: bool, fetched_at: float|None}.
    """
    cleanup_old_odds(date_iso)
    if not force_refresh:
        saved = saved_odds(date_iso)
        if saved and saved.get("pitchers"):
            return saved["pitchers"], {"cached": True, "fetched_at": saved.get("fetched_at")}
    fresh = get_pitcher_strikeout_lines(date_iso)
    if fresh:
        save_odds(date_iso, fresh)
    return fresh, {"cached": False, "fetched_at": time.time() if fresh else None}


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
