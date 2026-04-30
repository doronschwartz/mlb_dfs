"""Create drafts on the live system for slates already planned in the
spreadsheet but not yet drafted (e.g. May 3-7).

Reads matchups from the 'Team How Often (1).csv', resolves them to live
gamePks via the deployed app's /api/slate, then bulk-creates one draft per
day via /api/schedule_builder/apply (which randomizes drafter order).

Run once, locally, when the spreadsheet adds new planned dates.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import urllib.request
from datetime import date as Date

YEAR = 2026
MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
TEAM_ALIASES = {"ARI": "AZ", "SFG": "SF", "WAS": "WSH"}


def parse_date_label(s: str) -> Date | None:
    s = (s or "").strip()
    m = re.match(r"([A-Za-z]+)\s+(\d+)", s)
    if not m:
        return None
    mon = MONTH_MAP.get(m.group(1))
    if not mon:
        return None
    return Date(YEAR, mon, int(m.group(2)))


def canon(abbr: str) -> str:
    return TEAM_ALIASES.get(abbr, abbr)


def parse_planned_slates(csv_path: str, only_dates_after: Date) -> dict[str, list[tuple[str, str]]]:
    """Returns {iso_date: [(home_abbr, away_abbr), ...]} for date columns
    that have data and are after `only_dates_after`. Each matchup appears
    once even though both teams' rows reference it."""
    with open(csv_path) as f:
        rows = list(csv.reader(f))
    header = rows[0]
    by_date: dict[str, set[tuple[str, str]]] = {}
    for col_i in range(2, len(header)):
        d = parse_date_label(header[col_i])
        if not d or d <= only_dates_after:
            continue
        date_iso = d.isoformat()
        for r in rows[1:]:
            if not r or not r[0] or len(r) <= col_i:
                continue
            team = canon(r[0].strip())
            opp = canon((r[col_i] or "").strip())
            if not opp:
                continue
            # Each matchup ends up listed twice (once per team) — sort to dedupe.
            pair = tuple(sorted([team, opp]))
            by_date.setdefault(date_iso, set()).add(pair)
    return {d: sorted(s) for d, s in by_date.items()}


def fetch_slate(base_url: str, date_iso: str) -> list[dict]:
    url = f"{base_url}/api/slate?date={date_iso}"
    with urllib.request.urlopen(url) as r:
        return json.load(r).get("games", [])


def resolve_gamepks(slate: list[dict], matchups: list[tuple[str, str]]) -> tuple[list[int], list[tuple[str, str]]]:
    """Find gamePks for each matchup. Returns (resolved_pks, unresolved_pairs)."""
    found: list[int] = []
    missing: list[tuple[str, str]] = []
    used: set[int] = set()
    for pair in matchups:
        a, b = pair
        candidates = [
            g for g in slate
            if g.get("gamePk") not in used
            and tuple(sorted([(g["away"]["abbr"] or ""), (g["home"]["abbr"] or "")])) == pair
        ]
        if candidates:
            found.append(candidates[0]["gamePk"])
            used.add(candidates[0]["gamePk"])
        else:
            missing.append(pair)
    return found, missing


def main():
    base_url = sys.argv[1] if len(sys.argv) > 1 else "https://mlb-dfs-doron.fly.dev"
    last_played = Date(2026, 4, 30)  # spreadsheet historic data ends here
    csv_path = "/Users/doronschwartz/Downloads/MLB DFS 2026 - Team How Often (1).csv"

    planned = parse_planned_slates(csv_path, only_dates_after=last_played)
    print(f"Planned dates after {last_played}: {sorted(planned.keys())}")

    days_payload = []
    total_unresolved = 0
    for date_iso in sorted(planned.keys()):
        matchups = planned[date_iso]
        slate = fetch_slate(base_url, date_iso)
        pks, missing = resolve_gamepks(slate, matchups)
        print(f"  {date_iso}: planned={len(matchups)} resolved={len(pks)} unresolved={len(missing)}")
        if missing:
            print(f"    UNRESOLVED: {missing}")
            total_unresolved += len(missing)
        days_payload.append({"date": date_iso, "game_pks": pks})

    if total_unresolved:
        print(f"\n!! {total_unresolved} matchups did not resolve to gamePks. Aborting.")
        sys.exit(1)

    body = {
        "drafters": ["Stock", "Meech", "JL"],
        "days": days_payload,
        "randomize_order": True,
    }
    req = urllib.request.Request(
        f"{base_url}/api/schedule_builder/apply",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        resp = json.load(r)
    print()
    print(f"Created drafts: {len(resp.get('created', []))}")
    for c in resp.get("created", []):
        print(f"  {c['date']}: order = {c['drafters']}")
    if resp.get("skipped"):
        print(f"Skipped: {resp['skipped']}")


if __name__ == "__main__":
    main()
