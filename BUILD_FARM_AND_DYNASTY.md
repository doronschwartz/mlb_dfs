# Build Guide: Farm Report + Dynasty Valuation

*A complete technical reference for rebuilding these two features — every API endpoint, cache TTL, magic constant, baseline data file, and code quirk. Written so a developer friend can reconstruct them exactly. All data sources are free (no API keys).*

---

## 0. Stack, data sources, and the two universal quirks

**Stack.** Python + FastAPI here, but it's all HTTP + arithmetic — port freely. Concurrency is `ThreadPoolExecutor(max_workers=8)`. Everything is disk- and memory-cached.

**The data sources and their exact base URLs / cache policy:**

| Source | Base | Used for | Cache |
|---|---|---|---|
| MLB Stats API | `https://statsapi.mlb.com/api/v1` | schedules, player IDs, MLB + MiLB stats, awards, transactions | 6h in-memory (farm), keyed by full URL |
| Baseball Savant | `https://baseballsavant.mlb.com/leaderboard/...?csv=true` | xwOBA/xERA/barrel/percentiles | **6h in-memory + 24h on disk** — bulk season CSVs only |
| ESPN injuries | `https://site.web.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries` | current IL status (dynasty) | 30 min disk |
| Fantrax | `https://www.fantrax.com/fxpa/req` (POST, `?leagueId=`) | roster + league team list | per-request (auth) |
| Stuff+ | static CSV `data/stuff_leaderboard.csv` | pitcher process quality (dynasty) | static file |

**Quirk #1 — Savant blocks automation.** Never pull per-player. Pull the whole-season **leaderboard CSV** once (`?type=batter&year=2026&min=10&csv=true`), index it by player id, cache 24h on disk. Per-player live requests get you rate-limited/blocked.

**Quirk #2 — normalize every name before joining.** Sources disagree on accents and suffixes. This exact function is used at every join:
```
norm(name):
  Unicode NFD decompose, drop combining marks   # Acuña -> acuna
  lowercase
  remove characters . ' ,
  split, drop tokens {jr, sr, ii, iii, iv}, rejoin on single space
```

---

# PART 1 — FARM REPORT

**Goal:** given a Fantrax league+team, list every minor-leaguer on the roster with live MiLB stats and a keep/cut verdict; plus a second list of good unowned prospects.

Module-level constants: `_TTL = 6*3600` (in-memory cache), all HTTP via a `_get(url)` helper with `timeout=20`.

## 1.1 — Roster (Fantrax)

The user pastes their browser cookie. **Only three cookies authenticate** — everything else in a DevTools dump is ad/tracking noise:
```
FX_RM   JSESSIONID   gamera_user_id
```
`FX_RM` is the durable "remember me" token (often HttpOnly, so `document.cookie` in the console can't read it — must be copied from DevTools → Application → Cookies). Validate that a paste contains at least one of the three; reject with a clear message otherwise (a paste of only ad cookies means they weren't logged in).

Requests POST to `https://www.fantrax.com/fxpa/req?leagueId={id}` with a `requests.Session` carrying the cookies. `get_roster(league_id, team_id)` → player names. `list_teams(league_id)` → all teams (needed for Add Targets). If the roster comes back empty, the cookie expired — surface "re-auth," don't show blank.

## 1.2 — Resolve name → MLB player id

```
GET /people/search?names={urlencoded name}
```
**Use `names=`, NOT `q=`.** `q=` does fuzzy matching and returns the wrong human (it hands back Freddie Freeman for "Mason Miller"). Pick the result whose `norm(fullName) == norm(query)`, else the first hit.

## 1.3 — MiLB stats, per level

Each minor-league level is a separate `sportId`. Loop them:

| sportId | Level |  | sportId | Level |
|---|---|---|---|---|
| 11 | AAA |  | 14 | A |
| 12 | AA |  | 16 | Rookie |
| 13 | A+ |  | 1 | MLB (rehab check) |

```
GET /people/{pid}/stats?stats=season&season=2026&group=hitting,pitching&sportId={11..16}
```
For each level with real activity build a line:
- **Hitter** (keep if `plateAppearances > 0`): `pa`, `ops`, `k_pct`.
- **Pitcher** (keep if `inningsPitched > 0`): `ip`, `k_pct`, `bb_pct`, `kbb_pct`, and **`fip_lite`**.

**FIP-lite** (Savant won't serve MiLB xERA, so compute it):
```
fip_lite = round( (13*HR + 3*BB - 2*K) / IP + 3.10 , 2)   # None if IP == 0
```

## 1.4 — The verdict function (exact thresholds)

Three tiers: **green / yellow / red**. Governing principle: **the highest level with real playing time caps the verdict** — you can't green up on Rookie-ball numbers while getting shelled at AAA.

**Hitters** — a level "counts" at `pa >= 50`:
```
if total PA < 50:                                   return yellow   # small sample
top = the highest-level line with pa >= 50
if top and (top.ops < 0.650 or top.k_pct > 35):     return red      # drowning up top
# aggregate ops (PA-weighted) and k_pct across levels:
if ops >= 0.850 and (not top or top.ops >= 0.750):  return green
if ops < 0.700 or k_pct > 32:                        return red
return yellow
```

**Pitchers** — a level counts at `ip >= 15`:
```
if total IP < 15:                                    return yellow   # injured/rehab
top = highest-level line with ip >= 15
if top and (top.kbb_pct < 8 or (top.fip_lite is not None and top.fip_lite > 5.5)):
                                                     return red
# aggregate kbb_pct (IP-weighted), fip_lite (avg across levels):
if kbb_pct >= 15 and (fip_lite is None or fip_lite < 4.2) and (not top or top.kbb_pct >= 10):
                                                     return green
if kbb_pct < 8 or (fip_lite is not None and fip_lite > 5.0):
                                                     return red
return yellow
```
No 2026 MiLB stats at all → `red, "inactive or long-term injured"`. Always return **(verdict, reason-string)** — the reason ("struggling — 15% K-BB, 5.38 FIP-lite over 36 IP") is what makes the table credible.

## 1.5 — `my_farm(league_id, team_id, sort="cut")`

```
names = roster names from Fantrax
rows  = ThreadPool(8).map(player_row, names)     # player_row = resolve + milb_lines + verdict
for each row:
  keep if it has MiLB stats (bat or arm) OR has no MLB activity at all (a "stash")
  REHABBER FILTER: query sportId=1 season stats; if plateAppearances > 60 or inningsPitched > 20,
                   DROP it — a big-leaguer on a rehab assignment isn't a farm asset
attach prospect rank + FV grade via norm-name join against the rankings file
sort:
  "cut"  -> order red(0), yellow(1), green(2)      # drop list first
  "perf" -> perf_score descending                  # best assets first
```

**perf_score** — verdict tier dominates, raw production breaks ties:
```
base = {green:2000, yellow:1000, red:0}[verdict]     # default 500 if unknown
bats: base + weighted_OPS * 100
arms: base + max(0, 60 - fip_lite*6)                 # inverse FIP; lower ERA-est = higher
```

## 1.6 — `add_targets(league_id, limit=25, scan=120)`

```
ranks = rankings file
owned = union of every player on every team (loop list_teams -> get_roster)
free  = [p in ranks if norm(name) not in owned][:scan]      # top 120 unowned by rank
enrich each with live MiLB stats (ThreadPool 8)
keep the performers only:
  green                                          -> keep
  yellow AND has bats AND sum(PA) >= 100 AND weighted OPS >= 0.800 -> keep
  else                                           -> drop
sort green-first then by rank; return top `limit`
```

## 1.7 — Farm data + endpoints

- **`prospect_rankings.json`** — `{as_of, source, prospects:[{rank,name,team,position,level,grade}]}`. Source of current build: MLB Pipeline/ESPN/FanGraphs consensus top ~212 (post-deadline orgs) merged with a legacy deep tail renumbered 213+. Volume-persisted.
- `GET /api/farm/report?league_id&team_id&sort=cut|perf` → `{players:[...]}`
- `GET /api/farm/targets?league_id&limit=25` → `{as_of, targets:[...]}`
- `POST /api/farm/rankings` → replace the rankings JSON (no redeploy)

---

# PART 2 — DYNASTY VALUATION

**Goal:** re-rank a consensus dynasty list using multi-year Statcast skill, an age curve, luck, and scarcity. Constants below are the live values.

## 2.1 — Consensus baseline

**`dynasty_top500.csv`** (FantraxHQ export, gitignored/volume). Columns read: `Roto` (rank), `Points` (points_rank), `Age`, plus name/pos/team/level/eta. Blend `Roto` and `Points` 50/50 where both exist. Rank → value by exponential decay:
```
_DECAY_K = 0.0108
rank_value(r) = 1000 * exp(-0.0108 * (r-1))
# #1≈1000  #25≈765  #50≈589  #100≈343  #200≈117  (halves every ~64 spots)
```

## 2.2 — Multi-year skill read (`_skill_scores`)

Pull **current + 2 prior seasons** of Savant. Year weights (recency), then each year also weighted by its sample:
```
_YEAR_RECENCY = {0: 1.30, 1: 1.00, 2: 0.70}   # offset-from-current
```
Blend expected with actual so real breakouts move but mirages don't:
```
_ACTUAL_WEIGHT = 0.40
credit(expected, actual) = 0.60*expected + 0.40*actual
```

**Z-score baselines `(mean, sd)`:**
```
_HIT_BASE = { xwoba:(.315,.040)  xslg:(.410,.075)  xba:(.245,.025)
              barrel:(8.5,4.2)   hardhit:(40.0,6.5) sweetspot:(33.0,4.5) }
_PIT_BASE = { xera:(4.20,.85)  xwoba_against:(.315,.035)          # both negated (lower=better)
              barrel_allowed:(8.0,3.0)  hardhit_allowed:(39.0,5.0) }
```

**Composite weights:**
```
hitter:  xwoba .38  barrel .16  speed .14  xslg .12  hardhit .10  xba .06  sweetspot .04
pitcher: xera .28   stuff .24   k_rate .20  xwoba_against .18  barrel_allowed .12  hardhit_allowed .08
```
Notes: hitter `speed` = current-season sprint-speed percentile; pitcher `k_rate` = current K% percentile (stabilizes fast), `stuff` = z from the Stuff+ CSV. Process (k_rate+stuff = .44) ≈ outcome. Also compute **trajectory** = current-minus-prior xwOBA (hitter) / prior-minus-current xERA (pitcher). Final `skill_z` is `Σ(w*z)/Σw`, capped so a small-sample rookie can't outrank an MVP.

## 2.3 — Blend skill into baseline (Bayesian shrinkage — the key step)

```
_SKILL_BLEND = 0.35                          # skill's max share of the vote
k_prior:  hitters 220,  pitchers 90,  MiLB prospects 160
conf = total_sample_PA / (total_sample_PA + k_prior)
eff  = min(conf, 0.35)
base_value = eff * skill_value + (1-eff) * consensus_value
```
This is the **Skubal fix**: a 40-IP injured season has low `conf`, so consensus dominates and it can't tank a true ace. Optional **riser boost** (only ever lifts): if `rank_gap > 25 and conf >= 0.40`, add up to `+0.45 * (rank_gap/170) * conf * youth_amp`, where `youth_amp = 1 + min((27-age)/14, 0.5)`.

## 2.4 — Multiplier stack (exact values)

**Age curve** (`_age_factor`, peak 27):
```
hitter:  age<=27: max(0.93, 1 - 0.011*(27-age));  age>27: max(0.60, 1 - 0.022*(age-27))
pitcher: age<=27: max(0.92, 1 - 0.009*(27-age));  age>27: max(0.55, 1 - 0.024*(age-27))
```

**Statcast luck** (`_luck_multiplier`, ±5%, role-aware):
```
hitter delta = xwoba - woba;  mult = 1 + clamp(delta/0.030*0.05, ±0.05)
  delta >= +0.015 -> buy-low ;  delta <= -0.015 -> sell-high UNLESS xwoba >= .360 (elite, no ding)
pitcher delta = era - xera;   mult = 1 + clamp(delta/0.75*0.05, ±0.05)
  delta >= +0.40  -> buy-low ;  delta <= -0.40 -> sell-high UNLESS xera <= 3.20 (elite, no ding)
```

**Position scarcity** (`_POS_SCARCITY`):
```
C 1.14  SS 1.10  2B 1.06  CF 1.03  3B 1.02  (corner OF ~1.00)  TWP 1.12
1B 0.95  DH 0.90  RP 0.62
```

**ETA** (`_eta_factor`): `mult = max(0.82, 1 - 0.05*years_out)`; already-up = 1.0.
**Multi-position** (`_multipos_factor`): 2 real positions ×1.03, 3+ ×1.06 (DH/UT don't count).
**Young-ascending** (`_young_ascending_factor`): age ≤ 23 and skill_z ≥ 0.4 → up to +8% (`min(0.08, (24-age)*0.015 + (skill_z-0.4)*0.03)`).
**Trajectory**: ±4%, from the YoY xwOBA/xERA delta above.
**Durability** (`_durability`): avg games/starts over 2 prior seasons → ~0.91 to 1.0.
**Injury** (`_injury_factor`): ESPN current IL status; 60-day IL = real haircut, day-to-day ≈ none; small standing arm-risk on pitchers.
**Prospect bust-risk** (`_PROSPECT_RISK`, by consensus level): `AAA .90  AA .80  A+ .72  A .65  RK .58  CPX .55  DSL .50` (MLB players unaffected), blended toward 1.0 by pedigree.

**Prospects with no MLB Statcast** get skill from MiLB production instead (`_prospect_skill_z`): a production z-score off MiLB baselines, times a **level haircut** (`_LEVEL_FACTOR`: `AAA .80  AA .62  A+ .45  A .32  RK ~.20`), **plus the dominant signal** — age-vs-level: `+0.10z per year young for the level` (`_LEVEL_EXP_AGE`: MLB 27, AAA 24, AA 23, A+ 22, A 21…). Shrunk by `pa/(pa+130)` hitters, `/(bf+70)` pitchers.

## 2.5 — Project 6 years and re-rank

```
HORIZON = 6
_DISCOUNT_BY_LEVEL = { MLB/'' 0.90  AAA 0.85  AA 0.81  A+ 0.78  A 0.75  RK 0.73  CPX 0.72  DSL 0.70 }
peak_value = base_value / max(age_factor(now), 0.5)
dynasty_score = Σ_{k=0..5}  peak_value
                          * age_factor(age+k)
                          * pos * luck * inj * eta * multipos * young * traj * dur * prospect
                          * (DISCOUNT ** k)
```
Uncertainty-aware: farther-from-MLB prospects discount faster (A-ball 0.75/yr vs proven bat 0.90/yr), so by year 6 an A-ball guy is ~0.18× vs an MLB guy's ~0.59×. Re-rank the whole pool by `dynasty_score`; cache 6h (`_INPROC_TTL = 6*3600`). Endpoint: `GET /api/dynasty/rankings?season=2026&limit=500`.

---

## The quirks that will bite you if you skip them

1. **`names=` not `q=`** on `/people/search` — or you resolve the wrong players entirely.
2. **Savant is bulk-CSV-only, 24h disk cache** — per-player live requests get blocked.
3. **`norm()` at every join** — accents/suffixes silently break name matching across sources.
4. **Highest-level-governs** in the farm verdict — the one rule that makes it think like a scout.
5. **Shrink skill by sample size** in dynasty (`conf`, `k_prior`) — without it a 40-IP fluke tanks an ace; this is the Skubal fix.
6. **FIP-lite you compute yourself** (`(13·HR+3·BB−2·K)/IP+3.10`) — no MiLB xERA exists.
7. **Rehabber filter** (>60 MLB PA / >20 MLB IP) — keeps big-leaguers out of the farm view.
8. **Only 3 Fantrax cookies matter** (`FX_RM`/`JSESSIONID`/`gamera_user_id`); `FX_RM` is HttpOnly so it must come from the DevTools cookie panel, not the console.
9. **All tunables live at module top** — decay k, blend weights, k_priors, scarcity, haircuts, discount rates — so re-tuning never touches logic.
