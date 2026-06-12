# Launch Kit — mlb-dfs-public.fly.dev (free MLB DFS projections)

Researched 2026-06-12. Subscriber counts and rules pulled live from Reddit's `about.json` / `about/rules.json` endpoints on this date. All numbers approximate.

---

## 1. SUBREDDIT MAP

**Headline finding: the DFS subreddit landscape has collapsed since 2024.** r/dfsports is **BANNED** (`{"reason": "banned"}` from reddit API), r/draftkings is **BANNED**, r/fantasysports is **private**, and r/DKFantasy returns an empty shell (effectively dead). The DFS audience now lives in operator-specific subs (r/PrizePicks, r/UnderdogFantasy, r/DraftKingsDiscussion) and the big season-long/betting subs. Plan accordingly — the two "obvious" DFS targets in the original plan don't exist anymore.

| Subreddit | Subs | Self-promo rules (from rules.json) | Tools/site posts allowed? | Best time | Removal risk |
|---|---|---|---|---|---|
| **r/fantasybaseball** | ~398k | Rule "Self-Posts": *"Writers and website owners, etc are required to answer questions and participate in discussions within their own posts at a bare minimum... Do not go overboard with posting links from your site, r/fantasybaseball is not a farm for clicks."* Also: no memes; team/league questions go in stickied threads; no podcast links. | **Yes — explicitly.** Dedicated text post is fine if you participate. Tool posts with `Sabermetrics`/`Strategy` flair historically score 60–115+ (e.g., "New Streaming Planner Tool" 61pts, "I made a buy-low/sell-high algorithm" 86pts, "Mr. Cheatsheet" 115pts). A user already posts a *daily* "Whiffs Leaders" data thread under Sabermetrics flair — precedent for recurring data posts. | Weekday **8–11am ET** (lineup-setting window; the Daily Anything Goes thread posts ~7am ET and had 444 comments by mid-day). Avoid Fri night/Sat. | **Low** if you write a real text post, stay in comments all day, and don't link-spam. This is your primary target. |
| **r/dfsports** | — | **BANNED subreddit.** Does not exist anymore. | n/a | n/a | n/a — remove from plan |
| **r/DKFantasy** | — | Returns empty/defunct via API. Effectively dead. Use **r/DraftKingsDiscussion** instead (below). | n/a | n/a | n/a — remove from plan |
| **r/sportsbook** | ~602k | Rule 1: no touting/selling picks. Rule 2: *"No excessive self promotion. All content (picks/analysis/stats/trends/insights/etc.) must be included on Reddit."* — i.e., paste the analysis into the post; a link alone gets nuked. | **Comment-first.** Post your actual data (e.g., today's top Stuff+ arms, P(dud) flags) as native content; the site link rides along. No "check out my site" posts. | Morning ET before slates lock | **Medium.** Native-content rule is enforced. Works only as a data-included post. |
| **r/baseball** | ~3.15M | Rule: *"Self-promotion on r/baseball is allowed only with the explicit, prior approval of the moderators."* Strict quality bar; X/Twitter links banned; in-season restrictions on off-topic content. | **Only with modmail approval first.** The Stuff+ leaderboard is the one asset nerdy enough to qualify (frame as a sabermetrics resource, not a DFS tool). | n/a until approved | **High** without approval — auto-removal likely. Modmail first or skip. |
| **r/Sabermetrics** | ~16k | **No listed rules** (empty rules array). Analytics-focused, small but exactly the audience for Stuff+ methodology. | **Yes.** A methodology write-up ("How I built a self-grading projection model / Stuff+ with Bayesian shrinkage") fits perfectly. | Any weekday | **Low.** Best-fit small community. |
| **r/dynastybaseball** | ~11k | One rule: team-specific/trade questions go in stickied thread; *posts* are for general topics. | **Yes** for the **dynasty trade analyzer** as a general-topic tool post; don't frame as "rate my trade." | Weekday | **Low.** Perfect niche fit for the trade analyzer. |
| **r/DraftKingsDiscussion** | ~34k | *"No promotion of any kind without mod approval."* | Only after modmail. Free + no-paywall is a decent pitch to mods. | n/a until approved | **High** without approval. |
| **r/PrizePicks** | ~123k | *"No promotion of any kind without moderator approval"* — explicitly bans anything "remotely resembling promotion," instant ban. | **No.** Do not post. | n/a | **Ban-level.** Skip. |
| **r/UnderdogFantasy** | ~47k | "No Self-Promotion Spam" (repeat promotion banned), no promo codes, weekly discussion thread preferred. One-off genuine share *might* survive; repeat posting won't. | Marginal — one value-first comment in the weekly thread at most. | Weekly thread | **Medium-high.** Low priority. |
| **r/sportsbetting** | ~548k | Rule 3: *"No Advertising or Self-Promotion... This includes promoting... betting blogs."* | Effectively no. | n/a | **High.** Skip. |

**Net targeting order:** r/fantasybaseball (main launch) → r/Sabermetrics (methodology) → r/dynastybaseball (trade analyzer) → r/sportsbook (data-native comment/post) → mod-approval attempts at r/baseball and r/DraftKingsDiscussion.

Sources: live `reddit.com/r/<sub>/about/rules.json` fetches (2026-06-12); [bettingusa.com subreddit guide](https://www.bettingusa.com/best-subreddits-discussion-forums/); [gummysearch r/fantasybaseball stats](https://gummysearch.com/r/fantasybaseball/).

---

## 2. READY-TO-PASTE POSTS

### 2a. r/fantasybaseball — "I built a thing" (text post, flair: Sabermetrics or Strategy)

**Title:** I built a free MLB projections site that grades itself publicly every day — including when it's wrong

**Body:**

> Solo dev here, been lurking this sub for years. I got tired of projection sites that never show their work, so I built my own and I'm just giving it away: https://mlb-dfs-public.fly.dev
>
> The thing I actually care about and haven't seen anywhere else: there's an **accuracy page that re-grades the model every single day** against what actually happened. Bias and MAE, published, no cherry-picking. If the model has a cold week, that's on the page too. I figured if I'm not willing to show the receipts, why would anyone trust the numbers.
>
> Other stuff on there:
> - **Every projection has a factor-by-factor breakdown** — hover any number and see exactly why it's high or low (matchup, park, recent form, etc). No black box.
> - **Floor/ceiling bands + P(dud) per player**, built from empirical error distributions, not vibes. There's a Ceiling view if you're hunting tournament upside.
> - **A Stuff+ leaderboard** that updates daily, with a date-range picker so you can see whose stuff has actually ticked up over the last two weeks (uses Bayesian shrinkage so 1-inning wonders don't top the board).
> - **A dynasty trade analyzer** — paste both sides, get a verdict.
>
> It's free, no paywall, no signup. Updates every morning before lock.
>
> Honest caveats: it's one guy's model, the UI is functional rather than pretty, and the accuracy page will show you the days it whiffed. Would genuinely love feedback from this sub — especially if you find a projection that looks insane, tell me, the breakdown tooltip usually shows me where the model went wrong.
>
> Happy to answer anything about the methodology in the comments.

*(Then actually live in the comments for 24h — that's literally the sub's Rule 4.)*

### 2b. Daily-discussion-thread comment blurb (r/fantasybaseball Daily Anything Goes, r/UnderdogFantasy weekly thread, etc.)

> For anyone streaming SP tonight: my (free) model has [Pitcher A] as the best value on the slate — Stuff+ has him at 112 over his last 5 starts and the matchup grades out soft. [Pitcher B] is the trap: decent surface line but a 31% P(dud) tonight per the empirical bands. Full slate + the why-behind-every-number is at mlb-dfs-public.fly.dev (no paywall, and there's an accuracy page that grades the model daily so you can decide if it's worth listening to).

*(Template — swap in real players each day. The pick-with-reasoning has to come first; the link is a footnote.)*

### 2c. DFS-angled post — for r/sportsbook (native data included) or r/DraftKingsDiscussion (after mod approval). Originally specced for r/dfsports, which is banned.

**Title:** Free tool: empirical ceiling/floor bands and P(dud) for every MLB player tonight — built it because median projections kept losing me GPPs

**Body:**

> Cash-game projections are everywhere. What I couldn't find free anywhere was honest *distribution* data — so I built it.
>
> For tonight's slate, every player gets:
> - **Floor / median / ceiling bands** derived from the model's actual historical error, not a made-up ±20%
> - **P(dud)** — the empirical probability he posts a near-zero. Two players can project 9.0 FP and one of them busts twice as often; that's the number that decides GPP vs cash.
> - A **Ceiling view** that re-sorts the whole slate by tournament upside instead of median
>
> Sample from tonight: [Player X] and [Player Y] project within 0.5 FP of each other, but X's P(dud) is 14% vs Y's 29% — same salary tier. That's a free leverage decision.
>
> The model also grades itself publicly every day (bias/MAE on an accuracy page — including the bad days), and every projection has a tooltip showing the factor-by-factor math. Free, no signup: https://mlb-dfs-public.fly.dev
>
> Solo dev, happy to take feature requests or get told why I'm wrong.

### 2d. Three X/Twitter posts

**Launch tweet:**

> I built a free MLB DFS projections site that does something no paid site will: it grades itself publicly. Every day. Bias + MAE on a public accuracy page — bad days included.
>
> Also free: floor/ceiling bands, P(dud) per player, a live Stuff+ leaderboard, a dynasty trade analyzer.
>
> No paywall. No signup. https://mlb-dfs-public.fly.dev

**Daily-content template tweet:**

> ⚡ Today's top 5 ceiling plays (free model, receipts on the accuracy page):
>
> 1. [Player] — ceiling [X] FP, P(dud) [Y]%
> 2. [Player] — ...
> 3. ...
> 4. ...
> 5. ...
>
> Trap of the day: [Player] — projects fine, but [Z]% dud risk.
>
> Full slate + the math behind every number: mlb-dfs-public.fly.dev

**Transparency-angle tweet (with accuracy-page screenshot):**

> Every projection site says "trust our numbers." Here's what mine says instead: 👇
>
> [SCREENSHOT: accuracy page — last 30 days of bias/MAE, including the worst day, circled]
>
> Self-graded daily. No cherry-picking — the rough patches stay up. If a model won't show you its misses, ask why. https://mlb-dfs-public.fly.dev

---

## 3. CONTENT FLYWHEEL

**The single best format: a weekly "Model Report Card" post on r/fantasybaseball (Sabermetrics flair), every Monday morning, paired with a daily pick-with-reasoning comment in the Daily Anything Goes thread.**

Why this combo:
- **The daily thread is where the volume is** (444 comments by mid-day on a random Thursday) and comments carry zero removal risk. A daily "best SP value / trap of the day with the why" comment builds name recognition without ever tripping the self-promo rule. Precedent: a user already runs a daily "Whiffs Leaders" *post* — but standalone daily data posts score low (1–23 pts); the daily thread comment + weekly post split is higher leverage.
- **The weekly Report Card is the differentiated asset.** Nobody else can post "here's what my model said last week, here's what happened, here's the bias/MAE, here's the worst call I made and why the factor breakdown explains it" — because nobody else publishes their misses. Self-criticism is the most reddit-native content format that exists; it converts the accuracy page from a feature into a recurring story. Format: 1 table (predicted vs actual for the week's boldest calls), 1 number (weekly MAE vs a naive baseline), 1 confession (worst miss, what the tooltip revealed), 1 win.
- Cross-post the same Report Card to **r/Sabermetrics** (methodology-heavy version) and mirror it as the **transparency tweet** each Monday. One artifact, three surfaces.
- The Stuff+ leaderboard's date-range control feeds a secondary weekly format — "whose stuff changed the most in the last 14 days" — which doubles as injury/breakout detection content for r/fantasybaseball and is the asset to pitch r/baseball mods with.

---

## 4. MONETIZATION SCAN

### Affiliate / referral reality for a small DFS site

| Program | What's actually available | Terms found |
|---|---|---|
| **Underdog Fantasy** | Real CPA affiliate program; offers visible on aggregators. Most realistic first partner for a small site. | CPA listings of roughly **$4–6 per iOS install/acquisition** on networks ([Affplus listings](https://www.affplus.com/o/underdog-fantasy-sports)); direct partner deals are CPA per first-time depositor, negotiated. Consumer referral: $10/$10 credit. ([magentheme review](https://www.magentheme.com/underdog-fantasy-affiliate-program-review-pros-payouts/)) |
| **PrizePicks** | Partner program via [application](https://www.prizepicks.com/prizepicks-partner-application); promo-code based, manually approved, dedicated account manager, **negotiable CPA per depositing user**. Not on public networks (no Impact/CJ). | One-time CPA per new depositor; rates scale with audience. ([UpPromote](https://uppromote.com/affiliate-directory/prizepicks/)) Built for creators/sites with engaged audiences — apply once you can show traffic. |
| **Sleeper** | Consumer "Give/Get" referral only — **$25 per referred depositor, hard cap of 20 referrals** (waivable at Sleeper's discretion); requires a compensation disclosure on any page carrying the code. | [Sleeper Give/Get ToS](https://support.sleeper.com/en/articles/6150534-give-get-promotion-terms-of-use). Not a real affiliate program — pocket money, not a business model. |
| **DraftKings / FanDuel** | Consumer refer-a-friend is capped (DK: up to $100/referral, 5 per window — [terms](https://thegameday.com/draftkings/referral-bonus/); FD: $50/referral, 5 per 30 days — [SI](https://www.si.com/betting/sportsbook-promos/fanduel-promo-code/refer-a-friend)). Their *real* affiliate programs exist but are geared to licensed media partners with compliance review — realistic later, not at launch. | Start with Underdog + PrizePicks; revisit DK/FD once you have traffic numbers. |

**Practical take:** DFS affiliate is CPA-only (no rev-share), so value = volume of *new depositors*, not pageviews. A "Best DFS apps" / promo-code page plus contextual "play this slate on Underdog" links next to the Ceiling view is the standard pattern. Note many states (and **DFS ads are banned in California since July 2025** per Google policy — see below) complicate geo-targeting.

### AdSense viability

Risky and probably not worth it at launch. Google folded DFS explicitly into its **gambling policy** ([policy](https://support.google.com/adspolicy/answer/15132179?hl=en)); the policy changed ~18 times in 2025 ([Fortis Media](https://www.fortismedia.com/en/articles/google-ads-policy-on-gambling-is-changing/)), and **Daily Fantasy Sports advertising is disallowed in California** as of July 2025 ([Search Engine Roundtable](https://www.seroundtable.com/google-ads-disallows-daily-fantasy-sports-california-39792.html)). For a *publisher*, gambling-adjacent content typically gets restricted ad serving (fewer bidders, lower CPMs) and an account-review risk if affiliate links to real-money operators sit next to AdSense units. Expected revenue at small-site traffic: negligible. **Verdict: skip AdSense; affiliate CPA + an eventual premium tier dominate it.**

### What comparable sites charge (your free-tier anchor)

| Site | Price | Notes |
|---|---|---|
| **SaberSim** | **$97 / $197 / $297 per month** (Starter/Pro/Ultimate), $7 7-day trial | All sports incl. MLB; sims + optimizer. ([sabersim.com/pricing](https://www.sabersim.com/pricing), fetched live) |
| **Stokastic** | MLB **Core: $119.95/mo, $34.95/wk, or $599.95/season**; higher "Max" tier above that | Projections + ownership + sims. ([stokastic.com/pricing](https://www.stokastic.com/pricing)) |
| **Pitcher List PL Pro** | **$60/mo or $240/yr** | Season-long + DFS projections, tools, Discord. ([pitcherlist.com/premium](https://pitcherlist.com/premium/)) |
| **RotoWire** | **$22.99/mo** all-sports with DFS tools (~$11.99/mo annual) | The budget anchor. ([rotowire.com/subscribe](https://www.rotowire.com/subscribe/pricing/)) |

**Implication:** "free" against a $35–120/week market is a genuinely loud differentiator — lead with it everywhere. If a premium tier ever comes, the comp set says **$10–30/mo** is the credible indie band (undercut RotoWire, stay far under Stokastic); gate convenience (CSV export, API, alerts, lineup optimizer), never the accuracy page — transparency is the brand.

---

## 5. LAUNCH CHECKLIST — WEEK 1

**Pre-launch (Day 0, weekend):**
- [ ] Screenshot-polish the accuracy page (it's the hook in every post) and verify tooltips work on mobile — reddit traffic is ~70% mobile.
- [ ] Add a tiny "built by one person, free, feedback → [contact]" footer; set up a feedback channel (email or GitHub issues).
- [ ] Install privacy-friendly analytics with per-page tracking (Plausible/GoatCounter) so you can attribute each subreddit.
- [ ] Sanity-check your reddit account: posts from low-karma accounts in big subs get auto-filtered. Spend 2–3 days commenting normally in r/fantasybaseball first if the account is thin.
- [ ] Modmail r/baseball and r/DraftKingsDiscussion asking for self-promo approval (cite free/no-paywall; expect silence — costs nothing).

**Day 1 (Mon or Tue, 8–10am ET):**
- [ ] Post 2a to r/fantasybaseball. **Block the day to answer every comment** (their Rule 4 requires it; it's also what makes these posts work).
- [ ] Launch tweet (2d-1) same morning.
- Measure: upvote ratio, comments, referral visits, accuracy-page views vs projections views.

**Day 2:**
- [ ] Methodology post to r/Sabermetrics (rewrite 2a around the model design + Stuff+ shrinkage, not the product).
- [ ] Start the daily-thread comment habit (2b) — every morning from here on.

**Day 3:**
- [ ] Trade-analyzer post to r/dynastybaseball (general-topic framing per their one rule).
- [ ] First daily-content tweet (2d-2); commit to the daily template.

**Day 4–5:**
- [ ] r/sportsbook native-data post (2c with full data table pasted in — their rules demand content on reddit, link secondary).
- [ ] Transparency tweet with accuracy screenshot (2d-3).

**Day 7 (Mon):**
- [ ] First weekly **Model Report Card** on r/fantasybaseball — including how launch-week projections actually graded. This is the flywheel's first turn.
- [ ] Apply to Underdog affiliate (and PrizePicks partner if traffic justifies) with week-1 numbers in hand.

**What to measure all week:** visits per subreddit (which community converts), accuracy-page CTR from landing (validates the hook), return-visitor rate by day 7 (the only number that matters for a daily-updated tool), and every feature request verbatim (week-2 roadmap + future post material).

**Standing rules:** never post the same link in two subs on the same day; answer every comment for 24h after any post; if a mod removes something, modmail politely and ask the right format — mod relationships are the long game.
