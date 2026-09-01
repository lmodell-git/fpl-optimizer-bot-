# fpl-optimizer-bot

Weekly Fantasy Premier League optimiser. Every run it pulls the official FPL
API, projects expected points over a rolling horizon, solves a multi-period
mixed-integer program for transfers + captain + chip timing, has Claude
sanity-check the plan against fresh news, and — inside the pre-deadline window —
emails the recommendation to `louismodell1001@gmail.com`.

**New here?** → [SETUP.md](SETUP.md): connect the repo to GitHub.

**Recommend-only.** It emails a plan; you make the moves in the FPL app.
Auto-execution was built and then shelved — the Premier League retired the
scriptable login for a Cloudflare-walled account system
(see [SPEC.md](SPEC.md) §"Auto-execution (shelved)").

---

## How it fits together

```
run.py  ── orchestrates ──────────────────────────────────────────────┐
                                                                      │
 fplbot/fpl_api.py     read-only official API client (cached)         │
 fplbot/deadlines.py   dynamic next-deadline + notification window     │
 fplbot/state.py       state.json  (squad, bank, FT, chips, rank)      │
 fplbot/predict.py     xP model: P(minutes) × per-90 underlying rates  │
 fplbot/optimizer.py   MIP — initial 15 + XI + captain (PuLP/CBC)      │
 fplbot/transfers.py   MIP — multi-week transfers, FT banking, chips   │
 fplbot/strategy.py    overall rank → differential/template risk knobs │
 fplbot/captain.py     template-vs-differential captain gate           │
 fplbot/claude_review.py  Claude sanity-check pass + web search        │
 fplbot/report.py      builds the email + the Claude context          │
 emailer.py            Gmail SMTP + app password (shared helper)       ┘
```

`config.yaml` holds every tunable. `.github/workflows/fpl_bot.yml` runs it.

---

## Deadline handling (why it's not a Friday cron)

FPL deadlines are 90 min before the first kickoff and shift off Friday roughly
one week in six (Saturday 11:00, Sundays, midweek at Christmas). Nothing is
hardcoded. The workflow fires every 15 minutes; a dependency-free gate step
computes hours-to-deadline and only runs the full pipeline when:

* inside `notify.window_hours` (default 26h) of the next deadline, **or**
* the 07:00 UTC daily heartbeat, **or**
* a manual `workflow_dispatch`.

Inside the last `notify.tighten_hours` (default 3h) every 15-min tick runs, so a
slipped GitHub cron still lands the email before the deadline.

---

## One-time setup (all doable from a phone browser)

1. **Create the FPL team first.** The initial-squad optimiser (`mode: initial`)
   proposes a £100.0m squad; you enter that on the FPL site, then paste the
   numeric team ID (from the URL of *Points* → *Gameweek history*) into
   `state.json` as `entry_id`. Until then the bot just emails squad proposals.

2. **Create a public GitHub repo** (public = unlimited Action minutes) and push
   this folder, keeping `.github/workflows/` intact.

3. **Secrets** — repo → Settings → Secrets and variables → Actions:
   | Secret | Value |
   |---|---|
   | `GMAIL_APP_PASSWORD` | the 16-char Gmail app password (same one the momentum-check / risk-scan jobs use — [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), needs 2-Step Verification) |
   | `ANTHROPIC_API_KEY` | from [console.anthropic.com](https://console.anthropic.com) — *optional*; without it the pipeline still runs and emails the raw solver output |

4. **Enable Actions** (Actions tab → green button).

5. **Test:** Actions → `fpl-optimizer-bot` → Run workflow (leave *force notify*
   on). Check the log and your inbox.

---

## Local runs

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in GMAIL_APP_PASSWORD / ANTHROPIC_API_KEY

DRY_RUN=1 FORCE_NOTIFY=1 python run.py      # compute + print, no email, no state write
DRY_RUN=1 FORCE_NOTIFY=1 NO_CLAUDE=1 python run.py   # ... and skip the Claude call
python run.py                               # real run (respects the deadline window)
```

| Env var | Effect |
|---|---|
| `DRY_RUN=1` | print only — never send email, never write `state.json` |
| `FORCE_NOTIFY=1` | ignore the deadline window, act now |
| `NO_CLAUDE=1` | skip the Claude review pass |
| `FPL_STATE=path` | use a different state file |

Tests: `python tests/test_optimizer.py && python tests/test_deadlines.py`
(or `pytest tests/` if you have it).

Build a starting squad on demand:

```bash
python scripts/build_squad.py --template     # low-variance, template-owned
python scripts/build_squad.py                 # pure xP-optimal
python scripts/build_squad.py --template --lock 411 --exclude 279
```

---

## state.json

Committed back to the repo at the end of every real run — it's the only thing
that survives between stateless Action runs.

```jsonc
{
  "entry_id": null,          // FPL team id — paste in once the team exists
  "squad": [],               // 15 {element, purchase_price, selling_price}; synced from API when entry_id is set
  "bank": 1000,              // tenths of £m
  "free_transfers": 1,
  "chips_used": [],           // [{"chip": "wildcard", "event": 8}, ...]
  "overall_rank": null,
  "history": []               // run log, last 50
}
```

When `entry_id` is set the live API is the source of truth each run (squad, bank,
free transfers, chips, overall rank) and `state.json` just mirrors it plus the
run log. **No login needed** — `/api/entry/{id}/…` is public; the squad is read
from the last started gameweek (which is your team until you transfer).

---

## Known limitations (v0.1)

* **Early-season xP is heavily anchored to FPL's own `ep_next`** — with only 2–3
  matches played, per-90 rates are shrunk hard toward positional priors and the
  model leans on `ep_next`. It gets its own voice back as the season accrues
  minutes. Prior-season per-90 import is the obvious next improvement
  (`fpl_api.element_summary` per player, or a static file).
* **Price changes over the horizon are not modelled** — buys use `now_cost`.
* **Free Hit is approximated** as a one-week wildcard; the solver is told the
  squad reverts but doesn't re-optimise the revert.
* **Free-transfer recursion in the MIP is linearised** with big-M and is
  approximate when multiple hits stack in one GW.
* The multi-period solver runs against a pruned player universe (top-N per
  position by xP + everyone you own) to keep CBC solves in the low seconds.

See [SPEC.md](SPEC.md) for the full design.
