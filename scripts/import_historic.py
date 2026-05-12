"""Parse the spreadsheet CSV exports and emit data/historic/*.json.

Multi-season aware. Each season's CSVs should be named like:
    MLB DFS 2023 - Standings_Points.csv
    MLB DFS 2023 - Hitter Stat Sheets.csv
    MLB DFS 2023 - Pitcher Stat Sheets.csv
    MLB DFS 2023 - Team How Often.csv

Usage:
    # Re-import a single season (looks in ~/Downloads by default)
    python scripts/import_historic.py --year 2026

    # Import every season we have CSVs for
    python scripts/import_historic.py --all

    # Point at a different directory
    python scripts/import_historic.py --year 2024 --src ~/Downloads/mlb_history

Outputs to mlb_dfs/data/historic/{picks,standings,team_counts}.json — a single
aggregated file per kind, with `season` (int) attached to every record so the
records module can filter by season.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from datetime import date as Date

MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "Jun": 6, "Jul": 7, "Aug": 8,
    "Sep": 9, "Sept": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_date_label(s: str, year: int) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"([A-Za-z]+)\s+(\d+)", s)
    if not m:
        return None
    mon = MONTH_MAP.get(m.group(1))
    if not mon:
        return None
    try:
        return Date(year, mon, int(m.group(2))).isoformat()
    except ValueError:
        return None


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_standings(path: str, year: int) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.reader(f))
    out = []
    for row in rows[1:]:
        row = row + [""] * max(0, 25 - len(row))
        date_iso = parse_date_label(row[7], year)
        if not date_iso:
            continue
        ranks = (_to_float(row[8]), _to_float(row[9]), _to_float(row[10]))
        totals = (_to_float(row[13]), _to_float(row[14]), _to_float(row[15]))
        fulls = (_to_float(row[19]), _to_float(row[20]), _to_float(row[21]))
        if any(v is None for v in totals):
            continue
        names = ("Stock", "Meech", "JL")
        standings = [
            {
                "drafter": names[i],
                "rank": int(ranks[i]) if ranks[i] is not None else 0,
                "total": round(totals[i], 2),
                "full_total": round(fulls[i] or totals[i], 2),
            }
            for i in range(3)
        ]
        out.append({
            "date": date_iso,
            "season": year,
            "drafters": list(names),
            "is_complete": True,
            "standings": standings,
        })
    out.sort(key=lambda x: x["date"])
    return out


def parse_picks(path: str, role: str, year: int) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.reader(f))
    out = []
    for row in rows[1:]:
        row = row + [""] * max(0, 15 - len(row))
        for offset, drafter in [(0, "Stock"), (5, "Meech"), (10, "JL")]:
            date_iso = parse_date_label(row[offset], year)
            player = (row[offset + 1] or "").strip()
            score = _to_float(row[offset + 2])
            if not date_iso or not player or score is None:
                continue
            clean = re.sub(r"\s*\(P\)\s*", "", player).strip()
            out.append({
                "date": date_iso,
                "season": year,
                "drafter": drafter,
                "player_name": clean,
                "score": round(score, 2),
                "role": role,
            })
    out.sort(key=lambda x: (x["date"], x["drafter"]))
    return out


def parse_team_appearances(path: str, year: int) -> dict[str, int]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        rows = list(csv.reader(f))
    out: dict[str, int] = {}
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        n = _to_float(row[1])
        if n is None:
            continue
        out[row[0].strip()] = int(n)
    return out


def _candidate_paths(src_dir: str, year: int, name: str) -> list[str]:
    patterns = [
        f"MLB DFS {year} - {name}.csv",
        f"MLB DFS {year} - {name} (1).csv",
    ]
    return [os.path.join(src_dir, p) for p in patterns]


def _find(src_dir: str, year: int, name: str) -> str | None:
    for p in _candidate_paths(src_dir, year, name):
        if os.path.exists(p):
            return p
    return None


# ---- 2023/2024 format adapters (different schema from 2026) ----
# 2023 splits standings across two files:
#   "Daily Standings" — Date,JL,Meech,Stock (rank 1/2/3 per drafter)
#   "Total Points Sheet" — Date,JL,Meech,Stock totals, then totals (no min) on cols 5-7
# Hitter/Pitcher sheets use stride 4 between drafter blocks (vs 5 in 2026).


def parse_picks_2023(path: str, role: str, year: int) -> list[dict]:
    """Stride-4 layout: Stock at col 0/1/2, Meech at 4/5/6, JL at 8/9/10."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.reader(f))
    out = []
    for row in rows[1:]:
        row = row + [""] * max(0, 12 - len(row))
        for offset, drafter in [(0, "Stock"), (4, "Meech"), (8, "JL")]:
            date_iso = parse_date_label(row[offset], year)
            player = (row[offset + 1] or "").strip()
            score = _to_float(row[offset + 2])
            if not date_iso or not player or score is None:
                continue
            clean = re.sub(r"\s*\(P\)\s*", "", player).strip()
            out.append({
                "date": date_iso, "season": year, "drafter": drafter,
                "player_name": clean, "score": round(score, 2), "role": role,
            })
    out.sort(key=lambda x: (x["date"], x["drafter"]))
    return out


def parse_standings_2023(daily_path: str, totals_path: str, year: int) -> list[dict]:
    """Merge two files: Daily Standings (ranks) + Total Points Sheet (scores)."""
    ranks_by_date: dict[str, dict] = {}
    totals_by_date: dict[str, dict] = {}
    full_totals_by_date: dict[str, dict] = {}

    if os.path.exists(daily_path):
        with open(daily_path) as f:
            rows = list(csv.reader(f))
        # Header tells us drafter column order. Defensive against reorderings.
        header = rows[0] if rows else []
        drafter_cols = []
        for i, col in enumerate(header):
            name = col.strip()
            if name in ("JL", "Meech", "Stock"):
                drafter_cols.append((i, name))
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            d_iso = parse_date_label(row[0], year)
            if not d_iso:
                continue
            ranks_by_date[d_iso] = {}
            for idx, name in drafter_cols:
                v = _to_float(row[idx] if idx < len(row) else "")
                if v is not None:
                    ranks_by_date[d_iso][name] = int(v)

    if os.path.exists(totals_path):
        with open(totals_path) as f:
            rows = list(csv.reader(f))
        header = rows[0] if rows else []
        # First block of (JL,Meech,Stock) is "Total (with min)" — the scoring total
        # used to determine rank. Second block is "Total (No Min)" — full total.
        # Find both blocks by scanning the header.
        first_block: list[tuple[int, str]] = []
        second_block: list[tuple[int, str]] = []
        seen_names: set[str] = set()
        for i, col in enumerate(header):
            name = col.strip()
            if name in ("JL", "Meech", "Stock"):
                if name not in seen_names:
                    first_block.append((i, name))
                    seen_names.add(name)
                elif len(second_block) < 3:
                    second_block.append((i, name))
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            d_iso = parse_date_label(row[0], year)
            if not d_iso:
                continue
            tot = {}
            for idx, name in first_block:
                v = _to_float(row[idx] if idx < len(row) else "")
                if v is not None:
                    tot[name] = v
            if tot:
                totals_by_date[d_iso] = tot
            full = {}
            for idx, name in second_block:
                v = _to_float(row[idx] if idx < len(row) else "")
                if v is not None:
                    full[name] = v
            if full:
                full_totals_by_date[d_iso] = full

    out = []
    names_order = ("Stock", "Meech", "JL")
    for date_iso in sorted(set(ranks_by_date) | set(totals_by_date)):
        ranks = ranks_by_date.get(date_iso, {})
        totals = totals_by_date.get(date_iso, {})
        fulls = full_totals_by_date.get(date_iso, {})
        # Need at least totals to be meaningful
        if not totals:
            continue
        standings = []
        for n in names_order:
            standings.append({
                "drafter": n,
                "rank": int(ranks.get(n, 0)),
                "total": round(totals.get(n, 0.0), 2),
                "full_total": round(fulls.get(n, totals.get(n, 0.0)), 2),
            })
        out.append({
            "date": date_iso, "season": year,
            "drafters": list(names_order),
            "is_complete": True, "standings": standings,
        })
    out.sort(key=lambda x: x["date"])
    return out


def import_year(src_dir: str, year: int) -> dict:
    # Filename schema differs between seasons. Try both variants.
    standings_path = _find(src_dir, year, "Standings_Points")
    daily_standings_path = _find(src_dir, year, "Daily Standings")
    totals_path = _find(src_dir, year, "Total Points Sheet")
    hitter_path = _find(src_dir, year, "Hitter Stat Sheets")
    pitcher_path = _find(src_dir, year, "Pitcher Stat Sheets")
    teams_path = (_find(src_dir, year, "Team How Often")
                  or _find(src_dir, year, "How Often Each Team"))

    # Detect picks format from the header row stride. 2026 uses stride 5; 2023 stride 4.
    # Stride is the column offset between consecutive drafter blocks.
    def _detect_picks_stride(path: str) -> int:
        if not path or not os.path.exists(path):
            return 5
        try:
            with open(path) as f:
                header = next(csv.reader(f), [])
            # Find indices of "Player ... Picked" cells
            indices = [i for i, c in enumerate(header) if "Picked" in (c or "")]
            if len(indices) >= 2:
                return indices[1] - indices[0]
        except Exception:
            pass
        return 5
    stride = _detect_picks_stride(hitter_path or pitcher_path)

    if stride == 4:
        hitters = parse_picks_2023(hitter_path, "hitter", year) if hitter_path else []
        pitchers = parse_picks_2023(pitcher_path, "pitcher", year) if pitcher_path else []
    else:
        hitters = parse_picks(hitter_path, "hitter", year) if hitter_path else []
        pitchers = parse_picks(pitcher_path, "pitcher", year) if pitcher_path else []

    if standings_path:
        standings = parse_standings(standings_path, year)
    elif daily_standings_path or totals_path:
        standings = parse_standings_2023(daily_standings_path or "", totals_path or "", year)
    else:
        standings = []

    teams = parse_team_appearances(teams_path, year) if teams_path else {}
    return {
        "year": year,
        "standings": standings,
        "picks": hitters + pitchers,
        "team_counts": teams,
    }


def discover_years(src_dir: str) -> list[int]:
    years: set[int] = set()
    for path in glob.glob(os.path.join(src_dir, "MLB DFS *.csv")):
        m = re.search(r"MLB DFS (\d{4})", os.path.basename(path))
        if m:
            years.add(int(m.group(1)))
    return sorted(years)


def main():
    parser = argparse.ArgumentParser(description="Import historic season CSVs into data/historic/")
    parser.add_argument("--year", type=int, help="Single season to import (e.g. 2024)")
    parser.add_argument("--all", action="store_true", help="Import every season whose CSVs are found")
    parser.add_argument("--src", default=os.path.expanduser("~/Downloads"),
                        help="Directory containing the CSVs (default: ~/Downloads)")
    parser.add_argument("positional", nargs="?", help="(deprecated) positional source dir")
    args = parser.parse_args()
    src_dir = args.positional or args.src

    if args.all:
        years = discover_years(src_dir)
        if not years:
            print(f"No CSVs found in {src_dir}", file=sys.stderr)
            sys.exit(1)
    elif args.year:
        years = [args.year]
    else:
        years = [Date.today().year]

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(repo, "mlb_dfs", "data", "historic")
    os.makedirs(out_dir, exist_ok=True)

    def _load(name):
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    all_standings = _load("standings.json") or []
    all_picks = _load("picks.json") or []
    all_team_counts = _load("team_counts.json") or {}
    for rec in all_standings:
        rec.setdefault("season", int(rec["date"][:4]) if rec.get("date") else None)
    for rec in all_picks:
        rec.setdefault("season", int(rec["date"][:4]) if rec.get("date") else None)

    for year in years:
        result = import_year(src_dir, year)
        all_standings = [s for s in all_standings if s.get("season") != year] + result["standings"]
        all_picks = [p for p in all_picks if p.get("season") != year] + result["picks"]
        for team, n in result["team_counts"].items():
            all_team_counts[team] = n
        print(f"[{year}] standings={len(result['standings'])}  picks={len(result['picks'])}  teams={len(result['team_counts'])}")

    all_standings.sort(key=lambda x: x["date"])
    all_picks.sort(key=lambda x: (x["date"], x["drafter"]))

    with open(os.path.join(out_dir, "standings.json"), "w") as f:
        json.dump(all_standings, f, indent=2)
    with open(os.path.join(out_dir, "picks.json"), "w") as f:
        json.dump(all_picks, f, indent=2)
    with open(os.path.join(out_dir, "team_counts.json"), "w") as f:
        json.dump(all_team_counts, f, indent=2)

    seasons = sorted(set(p.get("season") for p in all_picks if p.get("season")))
    print(f"\nTotals across all seasons: standings={len(all_standings)}  picks={len(all_picks)}")
    print(f"Seasons loaded: {seasons}")
    print(f"-> {out_dir}")


if __name__ == "__main__":
    main()
