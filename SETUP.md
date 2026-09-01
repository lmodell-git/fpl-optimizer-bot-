# Setup

Two parts:

1. [Connect the repo to GitHub](#1-connect-to-github) — get the weekly
   recommendation email running. **Do this now.**
2. [Enable auto-execution](#2-auto-execution-not-built-yet) — have the bot
   actually make your transfers. **Not built yet**; this section is what it
   needs and what you'd have to provide.

---

## 1. Connect to GitHub

### What you need

| Thing | Where to get it | Required? |
|---|---|---|
| A GitHub account | [github.com](https://github.com) | yes |
| `git` on your machine | `git --version` — comes with macOS Xcode tools | yes (or use the web upload fallback) |
| Gmail **app password** (16 chars) | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) — needs 2‑Step Verification switched on first | yes — this is how the bot emails you |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) → API keys | optional — without it the pipeline still runs and emails the raw solver output |
| Your FPL **team ID** | log in at fantasy.premierleague.com → *Pick Team* / *Points*, the number in the URL `.../entry/<THIS>/event/...` | needed once your team exists; paste into `state.json` → `entry_id` |

You already have a Gmail app password from the momentum-check / risk-scan jobs —
**reuse the same one**, no need to make another.

### Steps

**1. Create the repository.** Public repos get unlimited Actions minutes, and
this repo holds no secrets in its files (they live in GitHub's encrypted secret
store), so public is fine and recommended.

With the [GitHub CLI](https://cli.github.com):

```bash
cd ~/files/Projects/fpl-optimizer-bot
gh repo create fpl-optimizer-bot --public --source=. --remote=origin --push
```

Or manually: create an empty repo named `fpl-optimizer-bot` on github.com (no
README/licence), then:

```bash
cd ~/files/Projects/fpl-optimizer-bot
git remote add origin https://github.com/<your-username>/fpl-optimizer-bot.git
git push -u origin main
```

**2. Add the secrets.** Repo → **Settings → Secrets and variables → Actions →
New repository secret**. Add:

| Name | Value |
|---|---|
| `GMAIL_APP_PASSWORD` | the 16 characters (spaces are fine, the code strips them) |
| `ANTHROPIC_API_KEY` | your key — skip if you don't want the Claude review pass |

**3. Enable Actions.** The **Actions** tab → green "I understand… enable" button
if prompted. The workflow (`.github/workflows/fpl_bot.yml`) is already in the repo.

**4. Add your FPL team ID.** Once you've entered a team on the FPL site, edit
`state.json`, set `"entry_id": <your number>`, commit and push:

```bash
git add state.json && git commit -m "set entry_id" && git push
```

Until then the bot runs in "initial squad" mode and emails a proposed £100.0m team.

**5. Test it.** Actions tab → **fpl-optimizer-bot** → **Run workflow** → leave
*force notify* ticked → **Run**. Watch the log; check your inbox (and spam,
once).

### How it runs after that

- Fires every 15 minutes. A dependency-free gate step exits in ~2 s unless
  there's a deadline within 26 h (or it's the 07:00 UTC daily heartbeat).
- Inside 26 h of a deadline: full pipeline + email.
- Inside the last 3 h: runs every 15 min so a slipped GitHub cron still gets the
  email out in time.
- Each real run commits `state.json` + `last_run.txt` back to the repo — that
  doubles as the keep‑alive that stops GitHub auto‑disabling the schedule after
  60 days.

### Costs / limits

- Public repo → Actions minutes are free and unmetered.
- Anthropic: one `claude-opus-5` call per notifying run (~once a week, plus
  mid-week if a deadline is near) with web search — cents per call.
- Gmail SMTP: well within the free sending limits at this volume.

---

## 2. Auto-execution (not built yet)

Right now the bot **recommends**; you make the moves in the FPL app. Full
auto-execution was in the brief but deferred deliberately — build a track record
on the recommendations first, then turn this on behind a switch.

### Why it's not just another API call

FPL has **no official write API**. Reading (prices, fixtures, your picks) is
open; making a transfer or setting a captain is not. Auto-execution means
scripting the same **undocumented, reverse-engineered** flow the community
`fpl` Python library uses:

1. `POST https://users.premierleague.com/accounts/login/`
   with `login`, `password`, `app=plfpl-web`, `redirect_uri=https://fantasy.premierleague.com/`
   → capture the `pl_profile` cookie + session.
2. `GET /api/my-team/{entry_id}/` (auth required) → current picks, bank, and the
   **selling prices** (needed to compute a legal transfer).
3. `POST /api/transfers/` with
   `{"entry": id, "event": gw, "chips": null, "transfers": [{"element_in", "element_out", "purchase_price", "selling_price"}]}`.
4. `POST /api/my-team/{entry_id}/` to set the XI, captain, vice and bench order.

### What you'd need to provide

| Thing | Notes |
|---|---|
| **FPL account email + password** | added by *you* as repo secrets `FPL_EMAIL` / `FPL_PASSWORD` — I never see them; the code reads them from the environment the same way `emailer.py` reads the Gmail password |
| **No 2‑Step Verification on the FPL account** | the scripted login can't pass a 2FA / email-code challenge. If your account has it, auto-execution isn't viable without a manual token step |
| **Guardrail decisions** | e.g. only auto-execute when the Claude verdict is `endorse` (not `amend`/`hold`); never auto-take more than N points of hits; hold off in the last 30 min before a deadline; a hard on/off switch |

### What I'd build

- `fplbot/execute.py` — login, session, `apply_transfers()`, `set_lineup()`,
  each returning a structured result.
- A `--execute` path in `run.py`, gated on **all** of:
  `state.json` → `"auto_execute": true`  **AND**  a config threshold
  (`execute.max_auto_hit`, `execute.require_verdict`)  **AND**  not within the
  final safety window.
- **Dry-run first**: logs the exact payload it *would* POST, sends it to you by
  email, POSTs nothing — run that for a few weeks before flipping the switch.
- **Failure alerting on every path**: any non-200, any login failure, any
  schema surprise → immediate email, and `auto_execute` flips itself back to
  `false` so it doesn't keep failing blind.
- A `manual freeze` — set `auto_execute: false`, push, done — for weeks you want
  to make a big call yourself.

### Risks to accept before turning it on

- Your FPL password sits in a GitHub secret (encrypted, not printed in logs, but
  it's there).
- FPL can change these endpoints or add bot protection (Cloudflare) at any time
  and the bot breaks mid-season — the failure alert tells you, but a broken
  auto-run that *looks* fine is the nightmare case, which is why dry-run-first
  and verdict-gating matter.
- A wrong transfer auto-executed can't be undone without a −4 (or a wildcard).

When you want this, say so and give me the guardrail choices above — I'll build
`execute.py` in dry-run mode first.
