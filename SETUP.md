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

## 2. Auto-execution (built, disabled by default)

`fplbot/execute.py` now exists: login flow, payload builders, guardrails. It is
**off by every default** and cannot submit anything until you deliberately arm
it. Build a track record on the recommendations first.

### Blockers before it can run at all

1. **An FPL team must exist** and `state.json` → `entry_id` must be set. Until
   then `scripts/try_execute.py` exits with *"create the FPL team first"*.
2. **`FPL_EMAIL` / `FPL_PASSWORD` must be reachable.** As repo secrets they only
   exist inside GitHub Actions — run the `fpl-execute-dryrun` workflow
   (Actions tab) to test from there. Locally, `export` them first.
3. **The scripted login must actually work.** If FPL emails a new-device code or
   shows a CAPTCHA, `execute.py` raises with a clear message and stops.

### Try it (dry-run — submits nothing)

Actions tab → **fpl-execute-dryrun** → Run workflow (leave *live* unticked). It
logs in, fetches your team, prints the exact transfer + lineup payloads it
*would* submit, and POSTs nothing. Watch this for a few gameweeks.

Locally:

```bash
export FPL_EMAIL='...' FPL_PASSWORD='...'
python scripts/try_execute.py          # dry-run
```

### Arming a live submit — ALL of these

| Where | Set |
|---|---|
| `state.json` | `"auto_execute": true` |
| `config.yaml` `execute:` | `armed: true` |
| `config.yaml` `execute:` | `dry_run: false` |
| guardrails (auto) | Claude verdict in `require_verdict` (default `[endorse]`), hits ≤ `max_auto_hit` (default 0), not inside `freeze_minutes` of the deadline |

Any failure → the recommendation email still goes out, an alert email is sent,
and `auto_execute` flips itself back to `false`.

---

### Background: why auto-execution is fragile

FPL has **no official write API**. Reading (prices, fixtures, your picks) is
open; making a transfer or setting a captain is not. `execute.py` scripts the
same **undocumented, reverse-engineered** flow the community `fpl` library uses:

1. `POST users.premierleague.com/accounts/login/` (`login`, `password`,
   `app=plfpl-web`, `redirect_uri`) → `pl_profile` cookie + session.
2. `GET /api/my-team/{entry_id}/` → current picks, bank, **selling prices**.
3. `POST /api/transfers/` →
   `{"entry", "event", "chips", "transfers":[{"element_in","element_out","purchase_price","selling_price"}]}`.
4. `POST /api/my-team/{entry_id}/` → XI (positions 1–11), bench (12–15),
   captain / vice.

Risks to accept before arming it:

- Your FPL password sits in a GitHub secret — encrypted, never printed in logs,
  but it's there.
- FPL can change these endpoints or add bot protection at any time and the bot
  breaks mid-season. The failure alert + self-disable handle that; a broken run
  that *looks* fine is why dry-run-first and verdict-gating matter.
- A wrong auto-executed transfer can't be undone without a −4 (or a wildcard).
