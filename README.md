# visa-monitor

Checks the VisaMetric (Germany / Russia) appointment site every ~20 minutes and
sends a **Telegram** message if an available appointment date is **earlier than a
date you set**. Runs entirely on free GitHub Actions — no server.

## How it works

Each run (`monitor.py`):
1. Opens the English landing page and reads the CAPTCHA (base64 embedded in the page).
2. Solves it with a Claude vision model (Haiku), submits, retries on a bad code.
3. Selects your six trip-form dropdowns by their visible labels.
4. Reads "First Available Dates", and if the earliest is before your threshold,
   sends a Telegram message with a screenshot attached.

Screenshots from every run are uploaded as a workflow artifact (`screenshots`) for debugging.

## One-time setup

### 1. Create the repo and push these files
```bash
git init
git add .
git commit -m "visa monitor"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```
(Or create an empty repo on github.com and drag-drop the files in via the web UI.)

A **private** repo is recommended.

### 2. Pick your notification channel(s)
You can use **Telegram**, **email**, or both. Each turns on automatically when its
secrets are present — set up whichever you want.

**Telegram:**
1. Message **@BotFather**, send `/newbot`, follow prompts, copy the **bot token**.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   the `chat.id` value (or message **@userinfobot** to get your ID).

**Email (Gmail example):**
1. Enable 2-Factor Auth on the Google account.
2. Create an **App Password** (Google Account → Security → App passwords) — this
   is a 16-character password, *not* your normal one.
3. Use your Gmail address as `SMTP_USER` and the app password as `SMTP_PASS`.
   Other providers: set `SMTP_HOST`/`SMTP_PORT` as Variables (465 = SSL, 587 = STARTTLS).

### 3. Get an Anthropic API key
From the Anthropic Console. Vision CAPTCHA solves with Haiku cost fractions of a
cent each.

### 4. Add Secrets
Repo → **Settings → Secrets and variables → Actions → Secrets → New repository secret**:

| Secret | Needed for | Value |
|---|---|---|
| `ANTHROPIC_API_KEY` | always | your Anthropic key |
| `TELEGRAM_BOT_TOKEN` | Telegram | from BotFather |
| `TELEGRAM_CHAT_ID` | Telegram | your chat ID |
| `SMTP_USER` | email | your email address |
| `SMTP_PASS` | email | app password |

(Only `ANTHROPIC_API_KEY` plus one channel's secrets are required.)

### 5. (Optional) Set config Variables
Same screen, **Variables** tab. If unset, the defaults below are used:

| Variable | Default |
|---|---|
| `THRESHOLD_DATE` | `30.06.2026` (notify if earliest available is before this) |
| `SEL_APPLICATION_TYPE` | `Schengen Visa` |
| `SEL_COUNTRY` | `Germany` |
| `SEL_CITY_RESIDENCE` | `Moscow Region` |
| `SEL_OFFICE` | `Moscow` |
| `SEL_SERVICE_TYPE` | `NORMAL` |
| `SEL_APPLICANTS` | `1 applicant` |
| `EMAIL_TO` | _(unset)_ — recipient address; required for email |
| `EMAIL_FROM` | _(defaults to `SMTP_USER`)_ |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` (use `465` for SSL) |
| `LOG_LEVEL` | `INFO` (set `DEBUG` for verbose output) |

`THRESHOLD_DATE` accepts `DD.MM.YYYY`, `YYYY-MM-DD`, or `DD-MM-YYYY`.

## Reading the logs
Every run logs each stage as a named **STEP** with start/finish and duration, e.g.
`==> STEP START: landing page + CAPTCHA` … `==> STEP OK: ... (3.2s)`. It also logs
the CAPTCHA guess, every dropdown's available options and what was selected, the
dates parsed, and the notify result per channel. The final line is either
`OUTCOME: SUCCESS` or `OUTCOME: FAILURE at step '<name>': <error>`, so a failed run
tells you exactly where it broke. Set the `LOG_LEVEL` variable to `DEBUG` for extra
detail (raw vision output, character counts, etc.).

### 6. Test it
Repo → **Actions** → enable workflows if prompted → **visa-monitor** →
**Run workflow** (manual trigger). Then:
- Check the run **log** — it prints the CAPTCHA guess, every dropdown's options,
  and the dates it found.
- Download the **screenshots** artifact to see what each step looked like.

Once a manual run works, the 20-minute schedule takes over automatically.
Disable the workflow (Actions → ⋯ → Disable) once you've grabbed your slot.

## Notes & limits
- **GitHub runner IPs are US datacenter addresses.** If the site geo-blocks or
  Cloudflare-challenges them, the `01_landing.png` screenshot will show it. Fixes:
  add a residential/RU proxy, or run the same script on your own machine.
- The trip-form dropdown **labels/order** are inferred from screenshots. If a run
  logs `no option matched` or finds no dates, check `04_after_selections.png` and
  adjust the `SEL_*` variables (or the selector logic in `monitor.py`).
- Poll politely. 20-minute intervals with jitter is already gentle; don't lower it
  aggressively. Check the site's terms of use.
- It pings you on **every** run where an earlier date is found (by design).

## Run locally instead
```bash
pip install -r requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
export THRESHOLD_DATE=30.06.2026 HEADLESS=0   # HEADLESS=0 shows the browser
python monitor.py
```
