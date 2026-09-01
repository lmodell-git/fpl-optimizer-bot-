# Setup

1. [Connect the repo to GitHub](#1-connect-to-github) — get the weekly
   recommendation email running.
2. [Auto-execution](#2-auto-execution--not-supported) — why the bot doesn't make
   the transfers for you.

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

## 2. Auto-execution — not supported

The bot **recommends**; you make the moves in the FPL app. This was a deliberate
call after testing, not an oversight.

It was built (`fplbot/execute.py`, guarded, dry-run-first) and then removed. The
first login test failed because **`users.premierleague.com` no longer exists** —
the Premier League retired it, and with it the form-POST login every community
FPL tool (and the `fpl` Python library) relied on. Auth now runs through
`account.premierleague.com`, behind **Cloudflare + AWS API Gateway** — a
"verify you're human" challenge, not something a script gets through.

The only path left is **session-cookie mode**: log in with a browser, copy the
session cookie into a GitHub secret, have the bot reuse it until it expires
(days to weeks), refresh it by hand each time. Rejected here as too manual and
too silent when it breaks.

If you ever want cookie-mode, the removed `execute.py` (login flow, FPL-legal
transfer/lineup payload builders, guardrails) is in git history near the initial
scaffold commits — it just needs the login swapped for a pasted cookie.
