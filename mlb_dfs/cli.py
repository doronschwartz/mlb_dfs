"""CLI: `mlb-dfs slate | project | draft | score | serve`."""

from __future__ import annotations

from datetime import date as Date

import click
from rich.console import Console
from rich.table import Table

from . import draft as draft_mod
from . import live as live_mod
from . import mlb_api, projections

console = Console()


@click.group()
def cli():
    """MLB DFS — 3-player snake draft wired to the live MLB Stats API."""


def _parse_date(s: str | None) -> Date:
    return Date.fromisoformat(s) if s else Date.today()


@cli.command()
@click.option("--date", "date_str", default=None, help="YYYY-MM-DD (default today)")
def slate(date_str):
    d = _parse_date(date_str)
    rows = mlb_api.slate(d)
    t = Table(title=f"MLB slate {d.isoformat()}", header_style="bold cyan")
    for col in ("Time/Status", "Away", "Away SP", "Home", "Home SP", "Venue"):
        t.add_column(col)
    for g in rows:
        away_sp = (g["away"]["probablePitcher"] or {}).get("name", "TBD")
        home_sp = (g["home"]["probablePitcher"] or {}).get("name", "TBD")
        t.add_row(
            g["detailedStatus"] or "",
            g["away"]["name"] or "", away_sp,
            g["home"]["name"] or "", home_sp,
            g["venue"] or "",
        )
    console.print(t)


@cli.command()
@click.option("--date", "date_str", default=None)
@click.option("--top", default=30, help="Show top N projections.")
def project(date_str, top):
    d = _parse_date(date_str)
    projs = projections.project_slate(d)
    t = Table(title=f"Top projections — {d.isoformat()}", header_style="bold green")
    for col in ("Pts", "Player", "Pos", "Role", "Notes"):
        t.add_column(col)
    for p in projs[:top]:
        t.add_row(
            f"{p.projected_points:.2f}", p.name, p.position or "-",
            p.role, " | ".join(p.notes),
        )
    console.print(t)


@cli.group()
def draft():
    """Draft commands."""


@draft.command("start")
@click.option("--date", "date_str", default=None)
@click.option("--drafters", required=True, help="Comma-separated, in snake order. Ex: Stock,JL,Meech")
def draft_start(date_str, drafters):
    d = _parse_date(date_str)
    dr = draft_mod.new_draft(d, [x.strip() for x in drafters.split(",") if x.strip()])
    path = draft_mod.save_draft(dr)
    console.print(f"[green]Created draft[/] {dr.draft_id}  -> {path}")


@draft.command("recommend")
@click.argument("draft_id")
@click.option("--top", default=8)
def draft_recommend(draft_id, top):
    dr = draft_mod.load_draft(draft_id)
    info = dr.on_the_clock()
    if info is None:
        console.print("[yellow]Draft is complete.[/]")
        return
    drafter, slot = info
    projs = projections.project_slate(Date.fromisoformat(dr.date))
    recs = dr.recommend(projs, top_n=top)
    t = Table(title=f"On the clock: {drafter} ({slot})", header_style="bold magenta")
    for col in ("Score", "Proj", "Player", "Pos", "Slot"):
        t.add_column(col)
    for r in recs:
        t.add_row(
            f"{r['score']:.2f}", f"{r['projected_points']:.2f}",
            r["name"], r["position"] or "-", r["recommend_slot"],
        )
    console.print(t)


@draft.command("pick")
@click.argument("draft_id")
@click.argument("player_id", type=int)
@click.option("--slot", required=True, type=click.Choice(["IF", "OF", "UTIL", "BN", "SP"]))
def draft_pick(draft_id, player_id, slot):
    dr = draft_mod.load_draft(draft_id)
    projs = projections.project_slate(Date.fromisoformat(dr.date))
    by_id = {p.player_id: p for p in projs}
    if player_id not in by_id:
        raise click.ClickException(f"player {player_id} not on slate")
    p = dr.make_pick(slot, by_id[player_id])
    draft_mod.save_draft(dr)
    console.print(f"[green]Picked[/] #{p.pick_number} {p.drafter} -> {slot}: {p.name}")


@cli.command()
@click.argument("draft_id")
def score(draft_id):
    dr = draft_mod.load_draft(draft_id)
    standings = live_mod.score_draft(dr)
    t = Table(title=f"Live scoring — {draft_id}", header_style="bold yellow")
    for col in ("Rank", "Drafter", "Total", "Full Total"):
        t.add_column(col)
    for s in standings:
        t.add_row(str(s.rank), s.drafter, f"{s.total:.2f}", f"{s.full_total:.2f}")
    console.print(t)
    for s in standings:
        sub = Table(title=f"{s.drafter} — {s.total:.2f}", header_style="bold")
        for col in ("Slot", "Player", "Proj", "Actual", "State"):
            sub.add_column(col)
        for pick, ps in s.picks:
            sub.add_row(
                pick.slot, pick.name, f"{pick.projected_points:.2f}",
                f"{ps.points:.2f}" if ps else "-",
                ps.game_state if ps else "-",
            )
        console.print(sub)


@cli.command()
@click.option("--port", default=8000)
def serve(port):
    """Run the FastAPI app + frontend."""
    import uvicorn
    uvicorn.run("mlb_dfs.web:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    cli()
