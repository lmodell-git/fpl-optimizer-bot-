# FPL Optimizer Bot — Spec

Distilled from the build plan. This is what the code implements; deviations are
called out.

## Goal

Climb toward the top ~1% (rank ~50–100k of ~11m) with a weekly automated
optimiser + occasional mid-week injury checks. One weekly decision point, a
GitHub Actions cron, a committed state file. Recommend-only for now.

## Architecture (implemented)

```
GitHub Actions  ── */15 cron, dependency-free gate step ──┐
  gate: hours-to-deadline < 26h  OR  07:00 heartbeat  OR  manual  → run full
                                                          │
run.py:                                                    │
  1. pull bootstrap-static + fixtures  (+ live squad if entry_id set)
  2. deadline window?  → notification_state()
  3. project xP over a 5-GW rolling horizon
  4. optimise:  initial-squad MIP   OR   multi-week transfer+chip MIP
     + rank-aware captain choice
  5. Claude sanity-check pass (news, judgement calls) — optional
  6. inside the window → email the recommendation
  7. write state.json, commit back to the repo
```

## Deadlines (implemented — `fplbot/deadlines.py`)

* `next_deadline()` — first event whose `deadline_time` is in the future. No
  weekday assumption.
* `notification_state(window_hours=26, tighten_hours=3)` — decides `should_notify`
  and returns `recheck_in_minutes` (180 normally, 15 in the final 3h). The
  workflow's gate step re-implements the 26h check in pure stdlib so it costs
  nothing when there's no deadline near.

## Data sources

| Source | Used for | Status |
|---|---|---|
| Official FPL API | prices, ownership, form, ICT, xG/xA per 90, defensive contribution, set-piece order, fixtures, deadlines, injury flags, live squad/rank | **implemented** (`fpl_api.py`, read-only, cached) |
| Understat / FBref | xG cross-check, deeper set-piece data | not wired — hook point noted in `predict.py` |
| Prior-season per-90s | early-season cold-start | **not done** — biggest known gap (see below) |
| OpenFPL / FPL Review xPts | second-opinion baseline | not wired |

## Prediction layer (implemented — `fplbot/predict.py`)

```
xP(player, gw) = P(appears) · appearance_pts
              + minutes_factor · (att_p90 · fixture_mult + def_p90 + bonus_p90)
              - minutes_factor · discipline_p90
```

* **Expected minutes is the primary driver.** `_start_probability()` combines
  `status` / `chance_of_playing_next_round` with season start-rate and
  minutes-per-game consistency. A rotation risk is dragged down hard.
* **Underlying over actual.** Attacking rate = 0.85·xG/xA-per-90 + 0.15·actual,
  then **shrunk toward that player's own last-season per-90** (`fplbot/priors.py`,
  built from `element-summary` `history_past`, half-weight at `shrink_minutes` =
  300 min). Falls back to a positional constant for promoted-team / new-signing
  players with no PL history. This is what stops a 1-minute cameo with
  `xG90 = 15` blowing up the optimiser *and* keeps a proven 20-goal forward from
  being flattened to average after two quiet games.
* **Set-piece duty** added after shrinkage (penalty taker +0.9 xP/90, corners/FK
  +0.15) — it's a role, not a sample.
* **Fixtures, not raw FDR.** `fixture_mult` = (my attack strength ÷ opponent
  defence strength, home/away split) ^ `fixture_swing`, × home advantage.
  Clean-sheet probability from opponent attack strength.
* **API anchor.** Every GW is blended toward FPL's own `ep_next`, weighted
  harder when our own sample is thin and fading across the horizon.
* Per-fixture xP hard-clamped at `max_fixture_xp` (13) as a blow-up guard.

**Backtest** (`fplbot/backtest.py`, `scripts/backtest.py`): rebuilds an
as-of projection for every finished GW from `element-summary` history and scores
it against actual points — Spearman rank correlation, MAE, per-position bias,
top-15 overlap. `data/backtest_latest.md` is refreshed monthly by the
`refresh-priors-and-backtest` workflow alongside `priors.json`.

**Honest state (GW1–2, n=2 so directional):** rank correlation ≈ 0.18, near-zero
bias, MAE ≈ 2.2 pts/player. Single-gameweek FPL scoring is mostly variance (one
goal + clean sheet + bonus is largely luck), so this is expected — the model
earns its keep over the 5-GW transfer horizon, not week to week. Correlation was
higher in GW2 than GW1 (more data), which is the signal to watch as the season
runs. Knob tuning (`api_anchor_weight`, `shrink_minutes`, `fixture_swing`)
waits for ~6+ GWs of backtest data.

## Optimisation engine (implemented — `fplbot/optimizer.py`, `fplbot/transfers.py`)

* **Initial squad** — MIP: £100.0m budget, 2/5/5/3 squad, 3-per-club, valid
  formation, XI + captain. Objective = weighted-horizon xP with a bench weight.
* **Weekly transfers** — one MIP over the whole `horizon` (default 5 GW), not a
  greedy single week:
  * free transfers bank up to 5; an extra transfer costs 4 pts (`HIT_COST`);
  * per-GW squad transition `own[g] = own[g-1] + buy - sell`;
  * rolling bank with selling-price rule (`state.json` purchase/selling prices);
  * XI + captain chosen per GW; decayed objective;
  * `churn_penalty` stops the solver ping-ponging near-equal players.
* **Chip timing** — `evaluate_chip_options()` re-solves the whole horizon with
  each available chip slotted into each of the next `chip_scan_gws` GWs and
  ranks the objective delta vs the no-chip baseline. Bench Boost = bench weight
  → 1.0 for that GW; Triple Captain = captain multiplier → 3; Wildcard / Free
  Hit = one GW of free transfers. This is the "decide chip timing by simulating
  the whole window" requirement.
* Solver: CBC (ships with PuLP). Player universe pruned to top-N/position + owned
  to keep solves at ~1–8 s.

**Reference:** `sertalpbilal/FPL-Optimization-Tools` is the mature multi-period
solver; this is a lighter self-contained implementation of the same idea. Swap
it in later if you outgrow this.

## Rank-aware strategy (implemented — `fplbot/strategy.py`)

`profile_for_rank(rank, gameweek)` → `RiskProfile`:

| Rank band | Differential appetite | Captain differential threshold |
|---|---|---|
| top ~1% | low (0.30) — protect | ~1.5 xP |
| top 5% | 0.55 — measured differentials | ~1.8 xP |
| top 20% | 0.70 — need differential exposure to close the gap | ~2.0 xP |
| below | 0.50 — balanced rebuild | ~1.75 xP |

Damped in the first ~8 GWs (small sample). Feeds the captain gate and (hook)
the objective's ownership handling. The objective function taking current rank
as an input — rather than a static "safe/aggressive" switch — is the design.

## Captain (implemented — `fplbot/captain.py`)

Template (highest effective-ownership among the top-xP options) by default. A
differential captain is only recommended when **all** hold: within
`captain_diff_threshold` xP of the template, ≥ the template's start
probability, > 8 percentage points lower owned, and `differential_appetite ≥
0.5`. Otherwise revert to template.

## Where Claude fits (implemented — `fplbot/claude_review.py`)

The MIP is optimal for the numbers it was given. Claude's pass (model
`claude-opus-5`, adaptive thinking, optional web search) does the layer on top:
sanity-check transfers/captain against very recent news, flag close judgement
calls (presser hints, "carrying a knock"), question a −4 hit. Returns a JSON
verdict (`endorse` / `amend` / `hold`) + summary + concerns, which go into the
email. If `ANTHROPIC_API_KEY` is unset the pipeline still ships the raw plan.

## Email (implemented — `fplbot/report.py`)

Sent via the shared `emailer.py` (Gmail SMTP + `GMAIL_APP_PASSWORD`). Contains:
proposed transfer(s) in/out with xP, hit cost, captain + vice with rationale,
bench order, the multi-GW horizon plan, ranked chip-timing options, and a
league-wide flagged-player watch list. Subject line carries the headline
(transfers / captain / chip / hours to deadline).

## Auto-execution (shelved)

The build plan wanted "fully auto-execute". It was built (`fplbot/execute.py`,
guarded, dry-run-first) and then **removed** after the first login test: the
Premier League has retired `users.premierleague.com` — the endpoint every
community FPL tool logs in through. Auth now goes via `account.premierleague.com`,
behind Cloudflare + AWS API Gateway (a bot challenge, not a form POST).

Consequences:

* Fully scripted login is not feasible — it hits a Cloudflare "verify you're
  human" wall.
* The only remaining path is **session-cookie mode**: log in via a browser,
  copy the session cookie into a secret, reuse it for reads + writes until it
  expires (days–weeks), refresh manually. Rejected here as too manual/fragile.

**This bot is recommend-only.** The weekly pipeline reads the public API and
emails the plan; the manager makes the moves in the app. If cookie-mode is ever
wanted, it's a self-contained `execute.py` again — see git history for the
removed version (`fpl-optimizer-bot` commits around the initial scaffold).

## Config surface (`config.yaml`)

`notify.window_hours` / `tighten_hours`; `predict.*` (horizon, decay, anchor
weight, shrinkage, fixture swing, set-piece bonuses, clamp); `optimizer.budget`;
`transfers.*` (decay, bench weight, time limit, churn penalty, chip scan GWs,
per-position pool sizes); `claude.web_search`. Rank curves are code
(`strategy.py`) so they can carry comments.
