# How the Dynasty Side of the Site Was Built

*A plain-language tour of the three dynasty tools — the Farm Report, the Trade Deadline Draft, and Dynasty Valuation — and the data and logic behind each. Last updated 2026-08-11.*

Everything here runs on free public data (MLB's Stats API, Baseball Savant/Statcast, and your Fantrax league via a cookie you paste in), plus a couple of research-compiled lists we keep current by hand. Nothing needs a redeploy to update — the lists live on a data volume and update through upload buttons.

---

## 1. Farm Report — "who's cuttable, who's a keeper, who should I add"

**What it does.** Point it at any Fantrax league + team and it pulls every minor-leaguer on that roster, fetches their live 2026 minor-league stats, and stamps each one with a **green / yellow / red** verdict. A second view — *Add Targets* — surfaces ranked prospects nobody in the league owns who are actually performing, so you're not just staring at names.

**Where the data comes from.**
- **Fantrax** (via the cookie you paste) for the roster.
- **MLB Stats API** for per-level minor-league stats, using the sport-level IDs: `11`=AAA, `12`=AA, `13`=A+, `14`=A, `16`=Rookie. A player's line at each level is pulled and combined.
- **Our prospect rankings list** (see §4) to attach each guy's overall prospect rank and FV grade next to his stats.

**The verdict rules.** The key idea is that the **highest level a player has real playing time at governs the verdict** — mashing in Rookie ball while getting shelled at AAA doesn't earn a green.

*Hitters* (needs ≥50 PA at a level to count):
- **Green (keep):** OPS ≥ .850 overall **and** holding his own at his top level (≥ .750 there).
- **Red (cuttable):** OPS < .700, **or** strikeout rate > 32%, **or** he's drowning at his top level (< .650 OPS or > 35% K there).
- **Yellow:** everything in between, or too small a sample to judge.

*Pitchers* (needs ≥15 IP at a level):
- **Green:** K-BB% ≥ 15% **and** FIP-lite under ~4.2 **and** not falling apart at his top level.
- **Red:** K-BB% under 8%, **or** FIP-lite over 5.0.
- **Yellow:** in between or thin sample.

(FIP-lite is a quick fielding-independent ERA estimate we compute ourselves, since Savant blocks automated minor-league pulls.)

**The two sort modes.** "Cuttable first" (red on top — your drop list) and "best performers first," which ranks by a blended score where the verdict tier dominates and raw production breaks ties (weighted OPS for bats, inverse FIP for arms). A green always outranks a red no matter the counting stats.

**Add Targets logic.** It collects every player owned by *any* team in the league, subtracts them from the ranking list, takes the top ~120 unowned by rank, pulls their live stats, and keeps only the ones actually producing (green, or a strong yellow bat with ≥100 PA and ≥ .800 OPS). Red and thin names are dropped — the point is *good and available*, not just available.

**Engineering notes.**
- **Rehabber filter:** a big-leaguer on a minor-league rehab stint (>60 MLB PA or >20 MLB IP this year) is excluded — he's not a farm asset.
- **Name normalization:** accents and Jr./Sr./III suffixes are stripped so "Ronald Acuña Jr." matches "ronald acuna" across every data source.
- **Speed:** stat fetches run 8-at-a-time in parallel; results cache for 6 hours so the first load (~30s) is the only slow one.

---

## 2. Trade Deadline Draft — a self-scoring prediction game

**What it does.** Before the Aug 3 deadline, the league snake-drafts players you think will get traded, each with a guess at *where* they'll land. The site then watches MLB's official transaction feed and **scores every pick automatically** as real trades happen — no manual bookkeeping.

**The scoring rules.**

| You earn | For |
|---|---|
| **+1.0** | the player actually gets traded |
| **+1.0** | you correctly called his destination team |
| **+0.5** | he was ever an All-Star |
| **+0.5** | he ever finished top-3 in MVP or Cy Young voting |

The two half-point pedigree bonuses only pay **if the player is actually traded** — a decorated guy who stays put is worth nothing. Max on a single pick is **3.0** (traded + right team + ⭐ + 🏅), which is exactly what JL got calling Skubal → Dodgers.

**Where the data comes from.**
- **MLB Stats API transactions feed**, filtered to actual trades (`typeCode = TR`) between two MLB clubs. This is the single source of truth — announcements and rumors don't score, only the official paperwork does.
- **MLB Awards API** for the ⭐/🏅 flags (All-Star = ALAS/NLAS award codes; top-3 = MVP/Cy winners), re-verifiable live so newly-named 2026 All-Stars auto-attach.
- **The full active-roster list** (~1,300 players, cached 6h) so you can search-and-draft *anyone*, not just names on the pre-built list.
- **Our candidate pool** (see §5) — a research-compiled shortlist with rumor context, tiers, and ESPN trade-probability percentages.

**How scoring stays honest.** A few guards were added as real exploits showed up during the draft:
- **Retroactive-scoring guard:** each pick stamps the date it was made; a trade dated *before* your pick doesn't count. You can't draft a guy who was already dealt last week.
- **Same-day exploit guard (the "Halvorsen" fix):** trade dates are day-granular, so someone could pick a player *hours after* he was traded that morning and still score. Now any player whose trade is already in the feed is rejected outright at pick time and greyed out in search.
- **The feed-freshness fixes** (the important, invisible ones): MLB's API serves long date-range queries from a **stale server-side cache** — on deadline day it was literally missing the Skubal trade the single-day query already had. The fix always re-queries the **last 3 days fresh** and merges them in. A second bug: MLB uses **one transaction ID per trade but one row per player**, so a naive de-dup was eating players out of multi-player deals — fixed by keying on *(trade ID, player ID)*.
- **15-minute cache + a "🔄 Check trades now" button** that busts it and rescores on demand, for when you don't want to wait during deadline chaos.

**The board.** Pool is filterable by position, team, and award pedigree, and sortable by trade probability. Live scores, whose turn it is, ⭐/🏅 badges, and already-traded greying all update as the feed does.

---

## 3. Dynasty Valuation — a re-ranked top 500

**What it does.** Takes the FantraxHQ consensus dynasty rankings as a starting point and **re-sorts them** using Statcast skill signals, an explicit age curve, and position scarcity — the goal being to surface buy-low and sell-high gaps the consensus is slow to price.

**The starting point.** Each player's consensus rank is turned into a value via exponential decay (rank #1 ≈ 1000 points, value roughly halving every ~64 spots). Where both a Roto and a Points rank exist, they're blended 50/50.

**The skill read.** For each player we pull **3 years of Statcast** (current + two prior), recency-weighted, and build z-scores from the metrics that actually predict forward:
- *Hitters:* xwOBA (the anchor), xSLG, barrel%, hard-hit%, sweet-spot%, and sprint speed. Expected stats are blended with 40% actual production so genuine breakouts move the board while mirages get pulled back toward their xwOBA.
- *Pitchers:* xERA, xwOBA-against, barrel- and hard-hit-allowed, strikeout rate, and JL's Stuff+ (process quality, which stabilizes fastest). Process is weighted about as heavily as outcomes.

**How much the skill read is allowed to matter** is governed by sample size — proper Bayesian shrinkage. A player's total multi-year sample sets the confidence; a 40-inning injured half-season can't override two elite full years. (This is exactly the fix that keeps an injured ace from cratering in the rankings.) A separate **riser boost** lets young players climbing hard get an extra lift, but it only ever *lifts* — consensus-strong veterans are never faded by it.

**The adjustment multipliers**, applied on top:
- **Age curve** — peak 27, gentle decline after, steeper for pitchers.
- **Statcast luck** — up to ±5% buy-low / sell-high tilt from xwOBA-vs-wOBA (or ERA-vs-xERA) gaps, but elite underlying talent is never penalized for lagging luck.
- **Position scarcity** — catchers (×1.14), shortstops (×1.10), up-the-middle guys premium; 1B/DH/RP discounted.
- **Durability** — multi-year games/starts played.
- **Prospects** — minor-league lines are converted to MLB-equivalent talent with a level-based haircut plus the single most important prospect signal: **young-for-level** (a 18-year-old holding his own in a level gets credited over a 24-year-old doing the same).

**The final number** projects a 6-year value curve with age decay and level-appropriate discounting (proven MLB bats discounted gently at ~0.90/year, far-away prospects harder at ~0.70/year), then sums it. The whole consensus pool is re-ranked by that score.

---

## 4. The prospect rankings list (`prospect_rankings.json`)

A ranked list of prospects — `{rank, name, team, position, level, grade}` — that the Farm Report joins against. It's **volume-persisted and updated by upload**, not code, so refreshing it needs no deploy. The current build (as of 2026-08-09) is a fresh post-deadline top-212 from MLB Pipeline / ESPN / FanGraphs consensus with corrected trade-deadline orgs, merged with a deeper legacy tail (renumbered 213+) so roster and waiver guys still get a rank even when they're outside the top tier.

## 5. The deadline candidate pool (`deadline_candidates.json`)

The shortlist the Deadline Draft is built around — `{name, position, team, tier, rumored_teams, context, has_allstar, has_top3_voting}` plus ESPN trade percentages. Tiers run high / medium / long-shot / deep. Also volume-persisted and updatable through an upload endpoint, so during deadline week we can fold in fresh rumors (and re-verify award flags live) without touching code.

---

## The shared design principles

1. **Official feeds are the source of truth.** Trades score off MLB's transaction API, not headlines — which is why "Skubal to LA" didn't score until the paperwork actually posted.
2. **Data that changes fast lives on a volume, not in the code.** Prospect lists, candidate pools, and award flags all update through upload endpoints — no redeploy, no downtime, editable mid-deadline.
3. **Sample size gates trust everywhere.** The farm verdict needs minimum PA/IP, dynasty valuation shrinks skill by confidence, and small samples fall back to consensus rather than overreacting.
4. **Guard against the exploit you didn't see coming.** Most of the deadline-draft fixes — retroactive scoring, same-day picks, multi-player-deal de-dup, stale-cache freshness — were added *after* a real edge case surfaced mid-game.
