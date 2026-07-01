# Diamond Model — Methodology (one page)

*Last updated: 2026-07-01 (v9.48). Update this when the pipeline shape changes, not for constant ratchets — those live in the changelog.*

## The projection pipeline (hitters)

1. **Base rate** — recency-weighted pts/G from disjoint L3/L7/L14/season buckets (weights ~2.5/2.2/1.5/0.7, sample-scaled) + league-average ghost prior for thin samples.
2. **Streak override** (HOT/COLD only) — base becomes `0.85·L3eff + 0.15·weighted`, where **L3eff regresses the spike toward season true talent** (v9.46: sample-trust × overshoot-damping; stops 2-game flukes from setting the baseline).
3. **Statcast prior blend** — barrel%/hard-hit% implied pts/G blended at weight 0.15 (HOT/COLD/ELITE/POOR) or 0.40 (others).
4. **Multiplicative chain** — opposing SP (or Vegas team total, which *supersedes* SP + bullpen and damps park to its HR/handedness residual), park, batting order (PA-change only), platoon (league prior ±5% blended 2× toward player's own splits, PA-shrunk), rolling K%, ISO form, SB threat, TB-prop market z (±5%), [arsenal — gated off].
5. **Post-chain corrections** (order matters): HOT ×1.13 / COLD ×0.80 / ELITE ×1.10 / STEADY ×1.12 → compression `5.6+(p−5.6)·0.85` → COLD ×0.81 / weak-L3 ×0.88 → **recency deviation** `+0.60·clamp(L3−base,±6)` → re-compression `·0.95` → lineup OUT ×0.05 / **pending × P(start)** (14-day appearance share).
6. **Uncertainty** — empirical p10/p90 bands (`−(2.60+0.712p)`, `+4.88+0.459p`), P(dud) logistic. Validated coverage ~80%.

**Pitchers**: same shape — weighted L7/L14/season per-start base, Statcast prior (w=0.40), chain (opp/Vegas, park, K%, ump, TTO, defense, framing, lineup), K-prop + outs-prop market deltas, HOT ×1.35 / COLD ×0.38, QoC tier lift/trim, de-compression ×1.25@9 then ×1.12@11.5, opener clamp.

## Validation rules (the discipline)

- **Never ship a knob without an A/B grid on held-out data** (time-split; both halves must improve or bias must be neutral).
- **One conservative notch inside the grid edge**; re-audit before the next notch.
- **A bias-fix that worsens MAE is not a fix** (high-variance buckets are traps — rejected twice on hot-recent).
- **Replicate before trusting**: a signal must appear in ≥2 independent windows (or ≥4σ in one) before acting.
- **Leak awareness**: Savant leaderboards + splits are season-cumulative → fine live, leak on backtests. Local rebuilds have no odds key → market-free; only compare like-for-like.
- Weekly self-audit (launchd, Mon 08:30) + daily health check (10:00) + this file + `/api/changelog`.

## Infrastructure map

| Thing | Where |
|---|---|
| Model | `mlb_dfs/projections.py` (MODEL_REV gates all caches) |
| Local leak-free rebuild | `scripts/build_rows_local.py` (3 parallel chunks OK) |
| Server rebuild | `scripts/build_rows.py` (env: ROWS_START/END/OUT) |
| Counterfactual A/B | `scripts/factor_ab_inversion.py` |
| Weekly audit | `scripts/weekly_audit.sh` → `data/audit_reports/` |
| Daily health + DK snapshot | `scripts/daily_health.sh`, `scripts/dk_snapshot.py` |
| DK benchmark | `scripts/dk_benchmark.py` |
| Prop archive | `/data/odds_archive` (volume) via `/api/prop_archive/{date}` |
| Draft-engine invariants | `tests/test_draft_simulation.py` (DRAFT_SIM_N for soak) |

## Known limitations (accepted, not forgotten)

- Extreme L3 tails (±4+ deviation, ~2% of hitters) stay ±3σ biased — corrections inflate MAE.
- Pitcher constants ride on small n; treat pitcher ratchets skeptically.
- Odds free tier (500/mo) < full-market burn (~45/day) → mid-month blackout unless upgraded. Health check alerts at <60 credits.
- No teammate correlation, no true rolling xwOBA (K%-shift proxy), no weather-temp factor. See the 40-angle critique (session notes 2026-06-30) for the full backlog.
