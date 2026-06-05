#!/usr/bin/env python3
"""
VisaMetric (Germany / Russia) appointment monitor.

One run:
  1. Open the English landing page.
  2. Read the CAPTCHA (base64 embedded in the page), solve it with a Claude
     vision model, submit. Retry a few times if the code is rejected.
  3. On the trip-information form, select the six dropdowns by visible label.
  4. Read "First Available Dates"; if the earliest is before THRESHOLD_DATE,
     notify via Telegram and/or email (whichever is configured).

Logging: every stage is wrapped in a named STEP. The log shows when each step
starts/finishes and how long it took. On any error, the final lines name the
exact step that failed. Set LOG_LEVEL=DEBUG for extra detail.
"""

import os
import re
import ssl
import sys
import time
import random
import logging
import smtplib
import datetime as dt
from contextlib import contextmanager
from email.message import EmailMessage

import requests
from anthropic import Anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_URL      = os.environ.get("BASE_URL", "https://ru-appointment.visametric.com/en")
THRESHOLD_RAW = os.environ.get("THRESHOLD_DATE", "30.06.2026")

SELECTIONS = [
    ("Application type", os.environ.get("SEL_APPLICATION_TYPE", "Schengen Visa")),
    ("Country of visit", os.environ.get("SEL_COUNTRY",          "Germany")),
    ("City of residence", os.environ.get("SEL_CITY_RESIDENCE",  "Moscow Region")),
    ("VisaMetric office", os.environ.get("SEL_OFFICE",          "Moscow")),
    ("Service type",     os.environ.get("SEL_SERVICE_TYPE",     "NORMAL")),
    ("Number of applicants", os.environ.get("SEL_APPLICANTS",   "1 applicant")),
]

CAPTCHA_RETRIES = int(os.environ.get("CAPTCHA_RETRIES", "4"))
MAX_JITTER_SEC  = int(os.environ.get("MAX_JITTER_SEC", "240"))
HEADLESS        = os.environ.get("HEADLESS", "1") != "0"
NOTIFY_ON_ERROR = os.environ.get("NOTIFY_ON_ERROR", "1") != "0"
NAV_TIMEOUT_MS  = int(os.environ.get("NAV_TIMEOUT_MS", "45000"))
VISION_MODEL    = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")

# Secrets / channels
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("SMTP_USER")
SMTP_PASS  = os.environ.get("SMTP_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER or "")
EMAIL_TO   = os.environ.get("EMAIL_TO")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SHOT_DIR = "screenshots"
os.makedirs(SHOT_DIR, exist_ok=True)
DATE_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")

# --------------------------------------------------------------------------- #
# Logging + step tracking
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("visa")

_CURRENT_STEP = "startup"


@contextmanager
def step(name: str):
    """Wrap a stage so the log clearly shows entry/exit/duration, and so the
    top-level handler can report exactly which step failed."""
    global _CURRENT_STEP
    _CURRENT_STEP = name
    log.info("==> STEP START: %s", name)
    t0 = time.monotonic()
    try:
        yield
    except Exception:
        log.error("==> STEP FAILED: %s  (after %.1fs)", name, time.monotonic() - t0)
        raise
    else:
        log.info("==> STEP OK:    %s  (%.1fs)", name, time.monotonic() - t0)


# --------------------------------------------------------------------------- #
# Validation / parsing
# --------------------------------------------------------------------------- #
def channels_configured():
    chans = []
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        chans.append("telegram")
    if SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO:
        chans.append("email")
    return chans


def require_env(chans):
    if not ANTHROPIC_API_KEY:
        sys.exit("FATAL: ANTHROPIC_API_KEY is not set.")
    if not chans:
        sys.exit("FATAL: no notification channel configured. Set Telegram "
                 "(TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) and/or email "
                 "(SMTP_USER + SMTP_PASS + EMAIL_TO).")


def parse_threshold(raw: str) -> dt.date:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    sys.exit(f"FATAL: could not parse THRESHOLD_DATE={raw!r} (use DD.MM.YYYY)")


def log_config(chans):
    log.info("--- visa-monitor configuration ---")
    log.info("Base URL        : %s", BASE_URL)
    log.info("Threshold       : %s", THRESHOLD_RAW)
    log.info("Selections      : %s", " | ".join(v for _, v in SELECTIONS))
    log.info("Channels        : %s", ", ".join(chans) or "NONE")
    log.info("Vision model    : %s", VISION_MODEL)
    log.info("Headless        : %s", HEADLESS)
    log.info("Captcha retries : %d", CAPTCHA_RETRIES)
    log.info("Max jitter      : %ds", MAX_JITTER_SEC)
    # presence-only (never print secret values)
    log.info("Secrets present : ANTHROPIC=%s TG_TOKEN=%s TG_CHAT=%s SMTP_USER=%s SMTP_PASS=%s EMAIL_TO=%s",
             bool(ANTHROPIC_API_KEY), bool(TELEGRAM_BOT_TOKEN), bool(TELEGRAM_CHAT_ID),
             bool(SMTP_USER), bool(SMTP_PASS), bool(EMAIL_TO))
    log.info("-----------------------------------")


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def send_telegram(subject: str, body: str, image_path=None) -> None:
    text = f"{subject}\n\n{body}"
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as fh:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": text[:1000]},
                files={"photo": fh}, timeout=60,
            )
    else:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "disable_web_page_preview": True}, timeout=30,
        )
    if r.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:200]}")


def send_email(subject: str, body: str, image_path=None) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM or SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype="image", subtype="png",
                               filename=os.path.basename(image_path))
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


def notify(chans, subject, body, image_path=None) -> None:
    """Send to every configured channel; log per-channel result; never raise."""
    for ch in chans:
        try:
            if ch == "telegram":
                send_telegram(subject, body, image_path)
            elif ch == "email":
                send_email(subject, body, image_path)
            log.info("Notify: %s OK", ch)
        except Exception as e:
            log.error("Notify: %s FAILED: %s", ch, e)


# --------------------------------------------------------------------------- #
# CAPTCHA
# --------------------------------------------------------------------------- #
def solve_captcha(b64_png: str) -> str:
    log.debug("Sending %d bytes of base64 to vision model", len(b64_png))
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=VISION_MODEL, max_tokens=20,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": b64_png}},
            {"type": "text", "text":
             ("This image is a CAPTCHA containing exactly 4 characters "
              "(letters and/or digits). Reply with ONLY those 4 characters "
              "in uppercase. No spaces, no explanation.")},
        ]}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    code = re.sub(r"[^A-Z0-9]", "", raw.strip().upper())[:4]
    log.debug("Vision raw=%r -> code=%r", raw.strip(), code)
    return code


# --------------------------------------------------------------------------- #
# Page helpers
# --------------------------------------------------------------------------- #
def grab_captcha_b64(page) -> str:
    page.wait_for_function(
        """() => {
            const el = document.querySelector('.imageCaptcha');
            return el && el.src && el.src.startsWith('data:image');
        }""", timeout=NAV_TIMEOUT_MS)
    return page.get_attribute(".imageCaptcha", "src").split(",", 1)[1]


def trip_selects(page):
    out = []
    for h in page.query_selector_all("select"):
        if (h.get_attribute("id") or "") == "language" or \
           (h.get_attribute("name") or "") == "language":
            continue
        out.append(h)
    return out


def select_by_text(handle, wanted: str) -> bool:
    options = handle.eval_on_selector_all(
        "option", "els => els.map(e => e.textContent.trim()).filter(Boolean)")
    log.info("    available options: %s", options)
    w = wanted.strip().lower()
    for opt in options:
        if opt.strip().lower() == w:
            handle.select_option(label=opt)
            log.info("    selected (exact): %r", opt)
            return True
    for opt in options:
        if w in opt.strip().lower():
            handle.select_option(label=opt)
            log.info("    selected (fuzzy '%s' -> %r)", wanted, opt)
            return True
    log.warning("    NO option matched %r", wanted)
    return False


def extract_dates(page):
    body_text = page.inner_text("body")
    input_vals = page.eval_on_selector_all(
        "input", "els => els.map(e => e.value || '').join(' ')")
    blob = body_text + " " + input_vals
    log.debug("Scanning %d chars for dates", len(blob))
    found = []
    for d, m, y in DATE_RE.findall(blob):
        try:
            found.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return sorted(set(found))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run() -> int:
    chans = channels_configured()
    require_env(chans)
    log_config(chans)
    threshold = parse_threshold(THRESHOLD_RAW)
    log.info("Decision rule: notify if earliest available < %s", threshold.isoformat())

    with step("jitter"):
        j = random.randint(0, MAX_JITTER_SEC)
        log.info("Sleeping %ds before starting", j)
        time.sleep(j)

    with sync_playwright() as p:
        with step("launch browser"):
            browser = p.chromium.launch(headless=HEADLESS)
            ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            page = ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)

        reached_form = False
        with step("landing page + CAPTCHA"):
            for attempt in range(1, CAPTCHA_RETRIES + 1):
                log.info("CAPTCHA attempt %d/%d", attempt, CAPTCHA_RETRIES)
                page.goto(BASE_URL, wait_until="domcontentloaded")
                log.info("Loaded: url=%s title=%r", page.url, page.title())
                page.screenshot(path=f"{SHOT_DIR}/01_landing.png")

                b64 = grab_captcha_b64(page)
                log.info("CAPTCHA image grabbed (%d b64 chars)", len(b64))
                code = solve_captcha(b64)
                log.info("CAPTCHA solved as %r", code)
                if len(code) != 4:
                    log.warning("Solved code is not 4 chars; retrying")
                    continue

                page.fill("#mailConfirmCodeControl", code)
                page.click("#confirmationbtn")
                log.info("Submitted code, waiting for trip form...")
                try:
                    page.wait_for_selector("text=/TRIP INFORMATION/i", timeout=15000)
                    reached_form = True
                    log.info("Reached trip form: url=%s title=%r", page.url, page.title())
                    break
                except PWTimeout:
                    page.screenshot(path=f"{SHOT_DIR}/02_captcha_fail_{attempt}.png")
                    log.warning("Trip form not detected (likely wrong code); retrying")

            if not reached_form:
                page.screenshot(path=f"{SHOT_DIR}/02_no_form.png")
                raise RuntimeError(
                    f"Failed to pass CAPTCHA after {CAPTCHA_RETRIES} attempts.")

        page.screenshot(path=f"{SHOT_DIR}/03_trip_form.png")

        with step("fill dropdowns"):
            for i, (field_name, wanted) in enumerate(SELECTIONS):
                selects = trip_selects(page)
                log.info("Dropdown %d/%d [%s] want=%r (found %d selects on page)",
                         i + 1, len(SELECTIONS), field_name, wanted, len(selects))
                if i >= len(selects):
                    raise RuntimeError(
                        f"Only {len(selects)} dropdowns found, need at least "
                        f"{len(SELECTIONS)}. See screenshots/03_trip_form.png")
                select_by_text(selects[i], wanted)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PWTimeout:
                    pass
                time.sleep(1.5)
            page.wait_for_timeout(2500)
            page.screenshot(path=f"{SHOT_DIR}/04_after_selections.png", full_page=True)

        with step("read dates"):
            dates = extract_dates(page)
            log.info("Parsed %d date(s): %s", len(dates),
                     [d.isoformat() for d in dates])
            if not dates:
                raise RuntimeError(
                    "No dates parsed. Selections may not have triggered the date "
                    "lookup, or the format differs. See "
                    "screenshots/04_after_selections.png")
            earliest = dates[0]
            log.info("Earliest available: %s  |  threshold: %s",
                     earliest.isoformat(), threshold.isoformat())

        with step("decide + notify"):
            if earliest < threshold:
                human = ", ".join(d.strftime("%d-%m-%Y") for d in dates[:6])
                subject = "\U0001F6A8 Earlier German visa slot available!"
                body = (f"Earliest: {earliest.strftime('%d-%m-%Y')} "
                        f"(target was {threshold.strftime('%d-%m-%Y')})\n"
                        f"Available: {human}\n"
                        f"Book: {BASE_URL}")
                log.info("EARLIER DATE FOUND -> notifying via %s",
                         ", ".join(chans))
                notify(chans, subject, body, f"{SHOT_DIR}/04_after_selections.png")
            else:
                log.info("Earliest (%s) is not before threshold (%s); no notification.",
                         earliest.isoformat(), threshold.isoformat())

        with step("teardown"):
            ctx.close()
            browser.close()

    log.info("OUTCOME: SUCCESS")
    return 0


if __name__ == "__main__":
    t_start = time.monotonic()
    try:
        rc = run()
        log.info("Total run time: %.1fs", time.monotonic() - t_start)
        sys.exit(rc)
    except SystemExit:
        raise
    except Exception as e:
        log.error("OUTCOME: FAILURE at step '%s': %s", _CURRENT_STEP, e)
        log.info("Total run time: %.1fs", time.monotonic() - t_start)
        if NOTIFY_ON_ERROR:
            chans = channels_configured()
            if chans:
                notify(chans, "\u26A0\uFE0F visa-monitor run failed",
                       f"Failed at step: {_CURRENT_STEP}\nError: {e}")
        # Exit 0: a single transient failure shouldn't spam GitHub with red
        # 'workflow failed' emails. The log + screenshots tell the full story.
        sys.exit(0)
