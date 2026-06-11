"""Local dataset builder — computes leak-free projections on THIS machine
(project_slate(d) passes as_of=d internally) instead of hammering the 1GB
prod box. Designed for parallel chunked runs:

    python scripts/build_rows_local.py 2026-05-17 2026-05-24 /tmp/rows_a.json
    python scripts/build_rows_local.py 2026-05-25 2026-06-02 /tmp/rows_b.json
    python scripts/build_rows_local.py 2026-06-03 2026-06-10 /tmp/rows_c.json

then merge the chunk files. NB vs the server build: no ODDS_API_KEY locally,
so vegas/tb_prop factors are neutral and the chain leans on sp_factor — the
arsenal/platoon counterfactual A/B stays internally consistent (same chain
for both arms); TB-prop is forward-validated separately via the archive.
"""
import json, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date as D, timedelta
from mlb_dfs import projections as P, live
from mlb_dfs.draft import Pick

NUM_FEATS = ["base_pg","pg_l3","pg_l7","pg_l14","games_l3","games_l7","games_l14",
    "sample_games_14d","sp_factor","sp_factor_raw","qoc_factor","park_factor",
    "order_factor","batting_order","vegas_factor","implied_team_total","bullpen_factor",
    "platoon_factor","rolling_factor","iso_factor","sb_factor","hot_cold_factor",
    "barrel_pct","hardhit_pct","sweet_spot_pct","chain_product","base_per_start",
    "k9_season","ip_per_start","xera","xwoba_against","barrel_pct_allowed",
    "opp_implied_total","k_prop_adj","tto_factor",
    "tb_prop_z","tb_prop_factor","arsenal_factor","vegas_outs_line","outs_prop_adj","p_dud"]
CAT_FEATS = ["form_tag", "qoc_tier"]


def build_date(d_iso):
    d = D.fromisoformat(d_iso)
    try:
        projs = P.project_slate(d)
        box = live._index_boxscores(d)
    except Exception as e:
        print("  %s: FAILED %s" % (d_iso, str(e)[:80]), flush=True)
        return []
    out = []
    for p in projs:
        lines = box.get(p.player_id) or []
        if not lines:
            continue
        fake = Pick(drafter="-", slot=("SP" if p.role == "pitcher" else "UTIL"),
                    player_id=p.player_id, name=p.name, position=p.position or "-",
                    role=p.role, projected_points=p.projected_points, pick_number=0, game_pk=None)
        ps = live._score_player(fake, lines)
        if ps.game_state in ("Pre-Game", "Warmup", "Scheduled", ""):
            continue
        c = p.components or {}
        r = {f: c.get(f) for f in NUM_FEATS}
        for cf in CAT_FEATS:
            r["cat_" + cf] = c.get(cf) or ""
        r["chain_proj"] = p.projected_points
        r["actual"] = ps.points
        r["role"] = p.role
        r["date"] = d_iso
        r["player_id"] = p.player_id
        r["name"] = p.name
        r["bats"] = c.get("bats")
        r["vs_throws"] = c.get("vs_throws")
        out.append(r)
    print("  %s: %d player-games" % (d_iso, len(out)), flush=True)
    return out


if __name__ == "__main__":
    start, end, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    d = D.fromisoformat(start)
    rows = []
    while d <= D.fromisoformat(end):
        rows.extend(build_date(d.isoformat()))
        d += timedelta(days=1)
    json.dump(rows, open(out_path, "w"))
    print("DONE %s..%s -> %d rows -> %s" % (start, end, len(rows), out_path), flush=True)
