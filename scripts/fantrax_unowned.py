"""Given a list of player names (one per line, or the rich prospect TSV) and a
Fantrax league_id, print which of those players are NOT rostered by anyone in
the league (i.e. available free agents). Pulls every team's roster and diffs
with accent-insensitive name matching.

    FANTRAX_COOKIE=... python scripts/fantrax_unowned.py <league_id> /tmp/prospect_names.json
"""
import json, sys, unicodedata
sys.path.insert(0, ".")
from mlb_dfs import fantrax


def norm(n: str) -> str:
    d = unicodedata.normalize("NFD", n or "")
    a = "".join(c for c in d if not unicodedata.combining(c))
    # strip Jr/Sr/III suffixes + punctuation so "George Lombard Jr." matches
    a = a.lower().replace(".", "").replace("'", "").replace(",", "")
    toks = [t for t in a.split() if t not in ("jr", "sr", "ii", "iii", "iv")]
    return " ".join(toks)


def main():
    league_id = sys.argv[1]
    names_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/prospect_names.json"
    want = json.load(open(names_path))
    if not fantrax.is_authenticated():
        print("NOT AUTHENTICATED — set FANTRAX_COOKIE or save a cookie first.")
        sys.exit(2)

    teams = fantrax.list_teams(league_id)
    print(f"League has {len(teams)} teams. Pulling rosters…", file=sys.stderr)
    owned = {}  # norm_name -> team short
    for t in teams:
        try:
            r = fantrax.get_roster(league_id, t["team_id"])
        except Exception as e:
            print(f"  ! {t['name']}: {e}", file=sys.stderr)
            continue
        for p in r.get("players", []):
            nm = p.get("name")
            if nm:
                owned[norm(nm)] = t.get("short") or t.get("name")
        print(f"  ✓ {t['name']}: {len(r.get('players', []))} rostered", file=sys.stderr)

    unowned, rostered = [], []
    for nm in want:
        if norm(nm) in owned:
            rostered.append((nm, owned[norm(nm)]))
        else:
            unowned.append(nm)

    print(f"\n=== {len(unowned)} of {len(want)} are AVAILABLE (not owned) ===\n")
    for nm in unowned:
        print(f"  • {nm}")
    print(f"\n--- {len(rostered)} already owned ---")
    for nm, who in rostered:
        print(f"  {nm}  ({who})")


if __name__ == "__main__":
    main()
