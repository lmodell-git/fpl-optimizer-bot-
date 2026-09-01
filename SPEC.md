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
  then **shrunk toward a positional prior** by sample size
  (`_shrink`, half-weight at `shrink_minutes` = 300 min). This is what stops a
  1-minute cameo with `xG90 = 15` blowing up the optimiser.
* **Set-piece duty** added after shrinkage (penalty taker +0.9 xP/90, corners/FK
  +0.15) — it's a role, not a sample.
* **Fixtures, not raw FDR.** `fixture_mult` = (my attack strength ÷ opponent
  defence strength, home/away split) ^ `fixture_swing`, × home advantage.
  Clean-sheet probability from opponent attack strength.
* **API anchor.** Every GW is blended toward FPL's own `ep_next`, weighted
  harder when our own sample is thin and fading across the horizon.
* Per-fixture xP hard-clamped at `max_fixture_xp` (13) as a blow-up guard.

**Known gap:** with 2–3 games played the anchor + shrinkage dominate, so the
early-season squad is close to "trust FPL's form column". Importing last
season's per-90s (via `fpl_api.element_summary` or a static CSV) is the intended
fix and the cleanest next task.

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

## Deferred: auto-execution

The build plan locks in "fully auto-execute", but this is **not built**. When it
is, it goes in an isolated `fplbot/execute.py` and needs:

* login to `users.premierleague.com/accounts/login/` with email + password
  (a new `FPL_EMAIL` / `FPL_PASSWORD` secret), capture the session cookie, POST
  to the undocumented `/api/transfers/` and `/api/my-team/{id}/` endpoints —
  the same reverse-engineered flow the community `fpl` library uses;
* a hard **manual-override switch** (`state.json.auto_execute`, default false)
  and a **failure alert on every path** — never fail silently;
* acceptance that the FPL password lives in a secret and FPL can change these
  endpoints mid-season without notice.

Build the read + recommend pipeline's track record first, then add this behind
the switch.

## Config surface (`config.yaml`)

`notify.window_hours` / `tighten_hours`; `predict.*` (horizon, decay, anchor
weight, shrinkage, fixture swing, set-piece bonuses, clamp); `optimizer.budget`;
`transfers.*` (decay, bench weight, time limit, churn penalty, chip scan GWs,
per-position pool sizes); `claude.web_search`. Rank curves are code
(`strategy.py`) so they can carry comments.
