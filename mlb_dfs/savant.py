"""Baseball Savant 'expected stats' (Statcast) — pulls CSV leaderboards.

Returns per-player {xwoba, xera, etc.} indexed by MLB player_id. Cached 6h.
We avoid pybaseball (heavy dep + slow scrape); the CSV endpoints are free
and stable.
"""
from __future__ import annotations

import csv
import io
import time

import requests

UA = {"User-Agent": "mlb_dfs/0.1"}
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 21600  # 6h in-memory

from . import disk_cache


@disk_cache.cached_disk(86400, namespace="savant_csv")  # 24h on disk
def _csv_disk(url: str) -> list[dict]:
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    text = r.text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def _csv(url: str) -> list[dict]:
    now = time.time()
    cached = _CACHE.get(url)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    try:
        rows = _csv_disk(url)
        _CACHE[url] = (now, rows)
        return rows
    except Exception:
        return []


def _idx(rows: list[dict], id_field: str = "player_id") -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in rows:
        try:
            pid = int(r.get(id_field, ""))
        except (TypeError, ValueError):
            continue
        out[pid] = r
    return out


def pitcher_expected(season: int) -> dict[int, dict]:
    """{pitcher_id: {xera, est_woba, est_ba, ...}}"""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=pitcher&year={season}&min=10&csv=true"
    )
    return _idx(rows)


def batter_expected(season: int) -> dict[int, dict]:
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter&year={season}&min=10&csv=true"
    )
    return _idx(rows)


def lookup_pitcher(pid: int, season: int) -> dict | None:
    return pitcher_expected(season).get(int(pid))


def lookup_batter(pid: int, season: int) -> dict | None:
    return batter_expected(season).get(int(pid))


def batter_statcast(season: int) -> dict[int, dict]:
    """Quality-of-contact metrics: barrel %, hard-hit %, sweet-spot %, EV."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/statcast"
        f"?type=batter&year={season}&min=q&csv=true"
    )
    return _idx(rows)


def pitcher_statcast(season: int) -> dict[int, dict]:
    """Pitcher quality-of-contact ALLOWED."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/statcast"
        f"?type=pitcher&year={season}&min=q&csv=true"
    )
    return _idx(rows)


def lookup_batter_qoc(pid: int, season: int) -> dict | None:
    return batter_statcast(season).get(int(pid))


def lookup_pitcher_qoc(pid: int, season: int) -> dict | None:
    return pitcher_statcast(season).get(int(pid))


def batter_expected_range(season: int, start_date: str, end_date: str) -> dict[int, dict]:
    """Rolling expected stats over a date window. Returns {pid: {est_woba, ...}}.
    Cached on disk for 24h via the same _csv pipeline."""
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter&year={season}&min=5&csv=true"
        f"&start_date={start_date}&end_date={end_date}"
    )
    return _idx(rows)


def pitcher_expected_range(season: int, start_date: str, end_date: str) -> dict[int, dict]:
    rows = _csv(
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=pitcher&year={season}&min=5&csv=true"
        f"&start_date={start_date}&end_date={end_date}"
    )
    return _idx(rows)
