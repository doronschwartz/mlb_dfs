"""All-time records & league history from imported season data.

Reads from mlb_dfs/data/historic/{picks,standings}.json (per-season tagged
records) and computes a comprehensive record book:

  Single-game records
    - Top N hitter games (player, score, drafter, date)
    - Top N pitcher games
    - Worst single picks
    - Biggest blowouts (largest 1-day winning margin)
    - Highest single-day team total
    - Highest combined slate score

  Most-picked
    - Top hitters by pick count (with avg score)
    - Top pitchers by pick count
    - Top teams by appearance count across picks

  Drafter all-time stats
    - Daily wins (count of #1 daily finishes)
    - Win % (daily wins / days played)
    - Total points scored
    - Avg points per day
    - Best day / worst day
    - Longest winning streak
    - Season titles (#1 by season-total points)
    - Head-to-head records vs each other drafter

  League records
    - Most picks in a single season by a drafter
    - Most wins in a single season
    - Highest single-season point total
    - Most-picked player across all time
    - Longest "streak of perfection" (consecutive days won by same drafter)
    - Days played per season

All records are returned as plain dicts so they JSON-serialize cleanly.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import groupby

from . import historic


# ---------- helpers ----------

def _picks() -> list[dict]:
    return historic.picks()


def _standings() -> list[dict]:
    return historic.standings()


def seasons() -> list[int]:
    """Sorted list of seasons present in the data."""
    s: set[int] = set()
    for p in _picks():
        if p.get("season"):
            s.add(int(p["season"]))
    for s_rec in _standings():
        if s_rec.get("season"):
            s.add(int(s_rec["season"]))
    return sorted(s)


def _filter_season(records: list[dict], season: int | None) -> list[dict]:
    if not season:
        return records
    return [r for r in records if r.get("season") == season]


# ---------- single-game records ----------

def top_hitter_games(top_n: int = 10, season: int | None = None) -> list[dict]:
    """Highest single-game hitter scores."""
    rows = [p for p in _filter_season(_picks(), season) if p.get("role") == "hitter"]
    rows.sort(key=lambda x: -x["score"])
    return [
        {
            "rank": i + 1,
            "player": p["player_name"],
            "score": p["score"],
            "drafter": p["drafter"],
            "date": p["date"],
            "season": p.get("season"),
        }
        for i, p in enumerate(rows[:top_n])
    ]


def top_pitcher_games(top_n: int = 10, season: int | None = None) -> list[dict]:
    """Highest single-game pitcher scores."""
    rows = [p for p in _filter_season(_picks(), season) if p.get("role") == "pitcher"]
    rows.sort(key=lambda x: -x["score"])
    return [
        {
            "rank": i + 1,
            "player": p["player_name"],
            "score": p["score"],
            "drafter": p["drafter"],
            "date": p["date"],
            "season": p.get("season"),
        }
        for i, p in enumerate(rows[:top_n])
    ]


def worst_picks(top_n: int = 10, season: int | None = None) -> list[dict]:
    """Lowest single-game scores — the most painful picks ever."""
    rows = list(_filter_season(_picks(), season))
    rows.sort(key=lambda x: x["score"])
    return [
        {
            "rank": i + 1,
            "player": p["player_name"],
            "score": p["score"],
            "drafter": p["drafter"],
            "date": p["date"],
            "season": p.get("season"),
            "role": p["role"],
        }
        for i, p in enumerate(rows[:top_n])
    ]


def highest_team_totals(top_n: int = 10, season: int | None = None) -> list[dict]:
    """Highest single-day team totals (any drafter, any day)."""
    rows = []
    for s_rec in _filter_season(_standings(), season):
        for drafter_rec in s_rec.get("standings", []):
            rows.append({
                "date": s_rec["date"],
                "season": s_rec.get("season"),
                "drafter": drafter_rec["drafter"],
                "total": drafter_rec["total"],
            })
    rows.sort(key=lambda x: -x["total"])
    return [{"rank": i + 1, **r} for i, r in enumerate(rows[:top_n])]


def highest_slate_totals(top_n: int = 10, season: int | None = None) -> list[dict]:
    """Highest combined slate totals (all drafters that day summed)."""
    rows = []
    for s_rec in _filter_season(_standings(), season):
        combined = sum(d["total"] for d in s_rec.get("standings", []))
        rows.append({
            "date": s_rec["date"],
            "season": s_rec.get("season"),
            "combined_total": round(combined, 2),
            "winner": _winner_of(s_rec),
        })
    rows.sort(key=lambda x: -x["combined_total"])
    return [{"rank": i + 1, **r} for i, r in enumerate(rows[:top_n])]


def biggest_blowouts(top_n: int = 10, season: int | None = None) -> list[dict]:
    """Largest 1-day margin between #1 and #2 finisher."""
    rows = []
    for s_rec in _filter_season(_standings(), season):
        sd = sorted(s_rec.get("standings", []), key=lambda x: -x["total"])
        if len(sd) < 2:
            continue
        margin = sd[0]["total"] - sd[1]["total"]
        rows.append({
            "date": s_rec["date"],
            "season": s_rec.get("season"),
            "winner": sd[0]["drafter"],
            "winner_total": sd[0]["total"],
            "runnerup": sd[1]["drafter"],
            "runnerup_total": sd[1]["total"],
            "margin": round(margin, 2),
        })
    rows.sort(key=lambda x: -x["margin"])
    return [{"rank": i + 1, **r} for i, r in enumerate(rows[:top_n])]


# ---------- most-picked ----------

def most_picked_hitters(top_n: int = 20, season: int | None = None) -> list[dict]:
    return _most_picked("hitter", top_n, season)


def most_picked_pitchers(top_n: int = 20, season: int | None = None) -> list[dict]:
    return _most_picked("pitcher", top_n, season)


def _most_picked(role: str, top_n: int, season: int | None) -> list[dict]:
    rows = [p for p in _filter_season(_picks(), season) if p.get("role") == role]
    by_player: dict[str, list[dict]] = defaultdict(list)
    for p in rows:
        by_player[p["player_name"]].append(p)
    summary = []
    for player, picks_list in by_player.items():
        scores = [p["score"] for p in picks_list]
        summary.append({
            "player": player,
            "times_picked": len(picks_list),
            "avg_score": round(sum(scores) / len(scores), 2),
            "best_score": max(scores),
            "worst_score": min(scores),
            "total_score": round(sum(scores), 2),
            "drafters": list(sorted(set(p["drafter"] for p in picks_list))),
        })
    summary.sort(key=lambda x: (-x["times_picked"], -x["avg_score"]))
    return summary[:top_n]


# ---------- per-drafter all-time stats ----------

def _winner_of(s_rec: dict) -> str | None:
    sd = sorted(s_rec.get("standings", []), key=lambda x: -x["total"])
    if not sd:
        return None
    if len(sd) >= 2 and sd[0]["total"] == sd[1]["total"]:
        return None  # tie — no winner
    return sd[0]["drafter"]


def drafter_alltime(season: int | None = None) -> list[dict]:
    """Per-drafter aggregate stats."""
    s_recs = _filter_season(_standings(), season)
    p_recs = _filter_season(_picks(), season)
    drafters: set[str] = set()
    for s in s_recs:
        for d in s.get("standings", []):
            drafters.add(d["drafter"])
    out = []
    for drafter in sorted(drafters):
        days = []
        for s in s_recs:
            for d in s.get("standings", []):
                if d["drafter"] == drafter:
                    days.append({"date": s["date"], "total": d["total"], "winner": _winner_of(s)})
        wins = sum(1 for d in days if d["winner"] == drafter)
        totals = [d["total"] for d in days]
        days_played = len(days)
        best = max(days, key=lambda d: d["total"]) if days else None
        worst = min(days, key=lambda d: d["total"]) if days else None
        # Picks
        drafter_picks = [p for p in p_recs if p["drafter"] == drafter]
        out.append({
            "drafter": drafter,
            "days_played": days_played,
            "wins": wins,
            "win_pct": round(wins / days_played, 3) if days_played else 0,
            "total_points": round(sum(totals), 2),
            "avg_points": round(sum(totals) / days_played, 2) if days_played else 0,
            "best_day": {"date": best["date"], "total": best["total"]} if best else None,
            "worst_day": {"date": worst["date"], "total": worst["total"]} if worst else None,
            "picks_made": len(drafter_picks),
            "longest_win_streak": _longest_win_streak(s_recs, drafter),
        })
    out.sort(key=lambda x: -x["wins"])
    return out


def _longest_win_streak(s_recs: list[dict], drafter: str) -> int:
    s_sorted = sorted(s_recs, key=lambda x: x["date"])
    best = cur = 0
    for s in s_sorted:
        if _winner_of(s) == drafter:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def season_titles() -> list[dict]:
    """Season champion (highest total points) per season."""
    by_season: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s_rec in _standings():
        ssn = s_rec.get("season")
        if not ssn:
            continue
        for d in s_rec.get("standings", []):
            by_season[ssn][d["drafter"]] += d["total"]
    out = []
    for ssn, totals in sorted(by_season.items()):
        ranked = sorted(totals.items(), key=lambda x: -x[1])
        if not ranked:
            continue
        out.append({
            "season": ssn,
            "champion": ranked[0][0],
            "winner_total": round(ranked[0][1], 2),
            "standings": [{"drafter": d, "total": round(t, 2)} for d, t in ranked],
        })
    return out


def head_to_head() -> dict[str, dict[str, dict]]:
    """For each drafter pair: wins/losses/ties when they shared a day."""
    s_recs = _standings()
    drafters: set[str] = set()
    for s in s_recs:
        for d in s.get("standings", []):
            drafters.add(d["drafter"])
    drafters_list = sorted(drafters)
    h2h: dict[str, dict[str, dict]] = {a: {b: {"wins": 0, "losses": 0, "ties": 0} for b in drafters_list if b != a} for a in drafters_list}
    for s in s_recs:
        by_name = {d["drafter"]: d["total"] for d in s.get("standings", [])}
        for a in drafters_list:
            for b in drafters_list:
                if a >= b or a not in by_name or b not in by_name:
                    continue
                if by_name[a] > by_name[b]:
                    h2h[a][b]["wins"] += 1
                    h2h[b][a]["losses"] += 1
                elif by_name[a] < by_name[b]:
                    h2h[a][b]["losses"] += 1
                    h2h[b][a]["wins"] += 1
                else:
                    h2h[a][b]["ties"] += 1
                    h2h[b][a]["ties"] += 1
    return h2h


# ---------- league records ----------

def league_records(season: int | None = None) -> dict:
    """Headline single-stat records, MLB-record-board style.
    Pass `season` to limit records to that season; otherwise spans all data."""
    picks = _filter_season(_picks(), season)
    standings_list = _filter_season(_standings(), season)
    hitter_picks = [p for p in picks if p.get("role") == "hitter"]
    pitcher_picks = [p for p in picks if p.get("role") == "pitcher"]

    def _topline(rows, scorer, label_cb):
        if not rows:
            return None
        winner = max(rows, key=scorer)
        return label_cb(winner)

    # Per-drafter season-aggregated stats
    season_totals: dict[tuple[int, str], float] = defaultdict(float)
    season_wins: dict[tuple[int, str], int] = defaultdict(int)
    for s in standings_list:
        ssn = s.get("season")
        winner = _winner_of(s)
        for d in s.get("standings", []):
            season_totals[(ssn, d["drafter"])] += d["total"]
            if d["drafter"] == winner:
                season_wins[(ssn, d["drafter"])] += 1

    rec: list[dict] = []

    # Best hitter game ever
    if hitter_picks:
        h = max(hitter_picks, key=lambda x: x["score"])
        rec.append({
            "stat": "Best Hitter Game",
            "name": h["player_name"],
            "value": h["score"],
            "date": h["date"],
            "extra": f"drafted by {h['drafter']} · {h.get('season')}",
        })
    # Best pitcher game
    if pitcher_picks:
        h = max(pitcher_picks, key=lambda x: x["score"])
        rec.append({
            "stat": "Best Pitcher Game",
            "name": h["player_name"],
            "value": h["score"],
            "date": h["date"],
            "extra": f"drafted by {h['drafter']} · {h.get('season')}",
        })
    # Worst single pick
    if picks:
        h = min(picks, key=lambda x: x["score"])
        rec.append({
            "stat": "Worst Single Pick",
            "name": h["player_name"],
            "value": h["score"],
            "date": h["date"],
            "extra": f"{h['drafter']} · {h['role']} · {h.get('season')}",
        })
    # Highest single-day team total
    team_totals = []
    for s in standings_list:
        for d in s.get("standings", []):
            team_totals.append({"drafter": d["drafter"], "total": d["total"], "date": s["date"], "season": s.get("season")})
    if team_totals:
        top = max(team_totals, key=lambda x: x["total"])
        rec.append({
            "stat": "Highest Daily Team Total",
            "name": top["drafter"],
            "value": top["total"],
            "date": top["date"],
            "extra": f"{top['season']}",
        })
    # Biggest blowout
    bb = biggest_blowouts(top_n=1, season=season)
    if bb:
        b = bb[0]
        rec.append({
            "stat": "Biggest Blowout",
            "name": f"{b['winner']} over {b['runnerup']}",
            "value": b["margin"],
            "date": b["date"],
            "extra": f"{b['winner_total']:.1f} vs {b['runnerup_total']:.1f}",
        })
    # Most wins in a single season
    if season_wins:
        (ssn, drafter), wins = max(season_wins.items(), key=lambda x: x[1])
        rec.append({
            "stat": "Most Wins in a Season",
            "name": drafter,
            "value": wins,
            "date": f"{ssn}",
            "extra": f"days won in {ssn}",
        })
    # Highest single-season total
    if season_totals:
        (ssn, drafter), total = max(season_totals.items(), key=lambda x: x[1])
        rec.append({
            "stat": "Highest Season Total",
            "name": drafter,
            "value": round(total, 2),
            "date": f"{ssn}",
            "extra": f"total points scored in {ssn}",
        })
    # Most-picked player (any role)
    if picks:
        pick_counter = Counter(p["player_name"] for p in picks)
        name, count = pick_counter.most_common(1)[0]
        rec.append({
            "stat": "Most-Picked Player (All-Time)",
            "name": name,
            "value": count,
            "date": "—",
            "extra": f"times drafted across all seasons",
        })
    # Most all-time wins
    drafter_all = drafter_alltime(season=season)
    if drafter_all:
        top_d = max(drafter_all, key=lambda x: x["wins"])
        rec.append({
            "stat": "Most Daily Wins (All-Time)",
            "name": top_d["drafter"],
            "value": top_d["wins"],
            "date": "—",
            "extra": f"{top_d['days_played']} days · {top_d['win_pct']*100:.1f}% win rate",
        })
    # Longest winning streak
    if drafter_all:
        top_streak = max(drafter_all, key=lambda x: x["longest_win_streak"])
        rec.append({
            "stat": "Longest Win Streak",
            "name": top_streak["drafter"],
            "value": top_streak["longest_win_streak"],
            "date": "—",
            "extra": f"consecutive days won",
        })

    return {"records": rec}


# ---------- aggregate API ----------

def all_records(top_n: int = 10, season: int | None = None) -> dict:
    """One-shot bundle for the UI to consume. Pass season to scope every
    record to a single year (head_to_head and season_titles still span all
    seasons since those are explicitly multi-season views)."""
    return {
        "seasons": seasons(),
        "season_filter": season,
        "league_records": league_records(season)["records"],
        "top_hitter_games": top_hitter_games(top_n, season),
        "top_pitcher_games": top_pitcher_games(top_n, season),
        "worst_picks": worst_picks(top_n, season),
        "highest_team_totals": highest_team_totals(top_n, season),
        "highest_slate_totals": highest_slate_totals(top_n, season),
        "biggest_blowouts": biggest_blowouts(top_n, season),
        "most_picked_hitters": most_picked_hitters(20, season),
        "most_picked_pitchers": most_picked_pitchers(20, season),
        "drafter_alltime": drafter_alltime(season),
        "season_titles": season_titles(),
        "head_to_head": head_to_head() if season is None else _head_to_head_season(season),
    }


def _head_to_head_season(season: int) -> dict:
    """Head-to-head limited to a single season."""
    s_recs = _filter_season(_standings(), season)
    drafters: set[str] = set()
    for s in s_recs:
        for d in s.get("standings", []):
            drafters.add(d["drafter"])
    drafters_list = sorted(drafters)
    h2h = {a: {b: {"wins": 0, "losses": 0, "ties": 0} for b in drafters_list if b != a} for a in drafters_list}
    for s in s_recs:
        by_name = {d["drafter"]: d["total"] for d in s.get("standings", [])}
        for a in drafters_list:
            for b in drafters_list:
                if a >= b or a not in by_name or b not in by_name:
                    continue
                if by_name[a] > by_name[b]:
                    h2h[a][b]["wins"] += 1; h2h[b][a]["losses"] += 1
                elif by_name[a] < by_name[b]:
                    h2h[a][b]["losses"] += 1; h2h[b][a]["wins"] += 1
                else:
                    h2h[a][b]["ties"] += 1; h2h[b][a]["ties"] += 1
    return h2h
