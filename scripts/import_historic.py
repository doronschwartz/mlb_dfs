"""Parse the spreadsheet CSV exports and emit data/historic/*.json.

Run once when new CSVs are exported from the MLB DFS 2026 sheet:
    python scripts/import_historic.py /path/to/Downloads
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import date as Date

YEAR = 2026
MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


def parse_date_label(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"([A-Za-z]+)\s+(\d+)", s)
    if not m:
        return None
    mon = MONTH_MAP.get(m.group(1))
    if not mon:
        return None
    return Date(YEAR, mon, int(m.group(2))).isoformat()


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_standings(path: str) -> list[dict]:
    with open(path) as f:
        rows = list(csv.reader(f))
    out = []
    for row in rows[1:]:
        # Defensive: pad row to 25 cols
        row = row + [""] * max(0, 25 - len(row))
        date_iso = parse_date_label(row[7])
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
            "drafters": list(names),
            "is_complete": True,
            "standings": standings,
        })
    out.sort(key=lambda x: x["date"])
    return out


def parse_picks(path: str, role: str) -> list[dict]:
    with open(path) as f:
        rows = list(csv.reader(f))
    out = []
    for row in rows[1:]:
        row = row + [""] * max(0, 15 - len(row))
        for offset, drafter in [(0, "Stock"), (5, "Meech"), (10, "JL")]:
            date_iso = parse_date_label(row[offset])
            player = (row[offset + 1] or "").strip()
            score = _to_float(row[offset + 2])
            if not date_iso or not player or score is None:
                continue
            clean = re.sub(r"\s*\(P\)\s*", "", player).strip()
            out.append({
                "date": date_iso,
                "drafter": drafter,
                "player_name": clean,
                "score": round(score, 2),
                "role": role,
            })
    out.sort(key=lambda x: (x["date"], x["drafter"]))
    return out


def parse_team_appearances(path: str) -> dict[str, int]:
    """Read 'How Often' column (col 1) per team; that's the season-to-date count."""
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


def main():
    downloads = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Downloads")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(repo, "data", "historic")
    os.makedirs(out_dir, exist_ok=True)

    standings_path = os.path.join(downloads, "MLB DFS 2026 - Standings_Points.csv")
    hitter_path = os.path.join(downloads, "MLB DFS 2026 - Hitter Stat Sheets.csv")
    pitcher_path = os.path.join(downloads, "MLB DFS 2026 - Pitcher Stat Sheets.csv")
    teams_path = os.path.join(downloads, "MLB DFS 2026 - Team How Often (1).csv")
    if not os.path.exists(teams_path):
        teams_path = os.path.join(downloads, "MLB DFS 2026 - Team How Often.csv")

    standings = parse_standings(standings_path)
    hitters = parse_picks(hitter_path, "hitter")
    pitchers = parse_picks(pitcher_path, "pitcher")
    picks = hitters + pitchers
    team_counts = parse_team_appearances(teams_path)

    with open(os.path.join(out_dir, "standings.json"), "w") as f:
        json.dump(standings, f, indent=2)
    with open(os.path.join(out_dir, "picks.json"), "w") as f:
        json.dump(picks, f, indent=2)
    with open(os.path.join(out_dir, "team_counts.json"), "w") as f:
        json.dump(team_counts, f, indent=2)

    print(f"standings: {len(standings)} days")
    print(f"picks:     {len(picks)} ({len(hitters)} hitters + {len(pitchers)} pitchers)")
    print(f"teams:     {len(team_counts)} teams (max count: {max(team_counts.values()) if team_counts else 0})")
    print(f"-> {out_dir}")


if __name__ == "__main__":
    main()
