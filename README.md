# mlb_dfs

A 3-player snake-draft DFS baseball game, wired to the live MLB Stats API.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

Ports the rules from the spreadsheet game (3 IF / 3 OF / 1 UTIL / 1 BN / 2 SP, 10 picks per drafter, snake order) and adds:

- live slate pulled from `statsapi.mlb.com`
- live box-score scoring using the spreadsheet's exact point values
- "smart" projections: rolling 14-day production + opposing-pitcher adjustment for hitters, recent-form + opponent wOBA-proxy for pitchers
- a draft assistant that ranks the next-best pick for each drafter given roster needs

No API key — the MLB Stats API is open.

## Install

```bash
cd mlb_dfs
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Use

```bash
# today's slate (games + probable pitchers + lineups when posted)
mlb-dfs slate

# smart projections for a given date
mlb-dfs project --date 2026-04-30

# start a draft (interactive, snake order)
mlb-dfs draft start --drafters Stock,Meech,JL --date 2026-04-30

# score a saved lineup against the live box scores
mlb-dfs score --draft-id 2026-04-30
```

## Deploy

### Render (free, no card)

1. Click the **Deploy to Render** button above (or go to <https://dashboard.render.com/blueprints>).
2. Connect this GitHub repo. Render auto-detects [render.yaml](./render.yaml) and creates a Docker web service on the free plan.
3. First build takes ~3 min. Service URL is printed when done.

Free-tier caveats: service sleeps after 15 min idle (~30s cold start) and drafts live in ephemeral storage (reset on redeploy). To persist drafts, upgrade to the Starter plan and uncomment the `disk:` block in `render.yaml`.

### Fly.io

```bash
fly auth login
fly launch --copy-config --no-deploy
fly volumes create drafts --size 1 --region iad
fly deploy
```

### Any Docker host

```bash
docker build -t mlb-dfs .
docker run -p 8000:8000 -v "$PWD/data:/data" -e MLB_DFS_DRAFT_DIR=/data/drafts mlb-dfs
```

## Scoring (from the sheet)

| Hitting |  | Pitching |  |
|---|---|---|---|
| Single | +3 | Out recorded | +0.75 |
| Double | +5 | Strikeout | +1.5 |
| Triple | +8 | Quality Start | +4 |
| HR | +10 | Complete Game | +2.5 |
| Run | +2 | Shutout | +2.5 |
| RBI | +2 | No-hitter | +5 |
| BB | +2 | Earned run | -2 |
| HBP | +2 | Hit allowed | -0.6 |
| SB | +3 | Hit batter | -0.6 |
| GIDP | -1.5 | Walk issued | -0.6 |
| Strikeout (batter) | -1.0 | | |
