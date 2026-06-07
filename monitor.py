#!/usr/bin/env python3
"""
VisaMetric (Germany / Russia) appointment monitor.

One run:
  1. Open the English landing page.
  2. Read the CAPTCHA (base64 embedded in the page), preprocess it (3× scale +
     contrast boost), then solve it with a Claude vision model. Retry on failure
     without a full page reload — after a wrong code the server redirects back
     to the landing page with a fresh CAPTCHA already loaded.
  3. On the trip-information form, select the six dropdowns by visible label.
  4. Read "First Available Dates"; if the earliest is before THRESHOLD_DATE,
     notify via Telegram and/or email (whichever is configured).
"""

import base64
import io
import os
import re
import ssl
import smtplib
import sys
import time
import random
import logging
import datetime as dt
from contextlib import contextmanager
from email.message import EmailMessage

import requests
from PIL import Image, ImageEnhance, ImageFilter
from anthropic import Anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_URL      = os.environ.get("BASE_URL", "https://ru-appointment.visametric.com/en")
THRESHOLD_RAW = os.environ.get("THRESHOLD_DATE", "30.06.2026")

SELECTIONS = [
    ("Application type",     os.environ.get("SEL_APPLICATION_TYPE", "Schengen Visa")),
    ("Country of visit",     os.environ.get("SEL_COUNTRY",          "Germany")),
    ("City of residence",    os.environ.get("SEL_CITY_RESIDENCE",   "Moscow Region")),
    ("VisaMetric office",    os.environ.get("SEL_OFFICE",           "Moscow")),
    ("Service type",         os.environ.get("SEL_SERVICE_TYPE",     "NORMAL")),
    ("Number of applicants", os.environ.get("SEL_APPLICANTS",       "1 applicant")),
]

# Increased default from 4 → 6 to give more chances per run
CAPTCHA_RETRIES = int(os.environ.get("CAPTCHA_RETRIES", "6"))
MAX_JITTER_SEC  = int(os.environ.get("MAX_JITTER_SEC", "240"))
HEADLESS        = os.environ.get("HEADLESS", "1") != "0"
NOTIFY_ON_ERROR = os.environ.get("NOTIFY_ON_ERROR", "1") != "0"
NAV_TIMEOUT_MS  = int(os.environ.get("NAV_TIMEOUT_MS", "45000"))

# Vision model for CAPTCHA solving.
# claude-haiku-4-5-20251001 is cheap; if accuracy is still poor after
# preprocessing, try a stronger model — check console.anthropic.com/models
# for the exact current model string, e.g. claude-sonnet-4-... 
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")

# Secrets / channels
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("SMTP_USER")
SMTP_PASS  = os.environ.get("SMTP_PASS")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER or "")
EMAIL_TO       = os.environ.get("EMAIL_TO")
EMAIL_ERROR_TO = os.environ.get("EMAIL_ERROR_TO") or EMAIL_TO  # if unset, errors also go to EMAIL_TO

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
        sys.exit("FATAL: no notification channel configured.")


def parse_threshold(raw: str) -> dt.date:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    sys.exit(f"FATAL: could not parse THRESHOLD_DATE={raw!r}")


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
    log.info("Email success→  : %s", EMAIL_TO or "(not set)")
    log.info("Email errors→   : %s", EMAIL_ERROR_TO or "(not set)")
    log.info("Secrets present : ANTHROPIC=%s TG_TOKEN=%s TG_CHAT=%s "
             "SMTP_USER=%s SMTP_PASS=%s EMAIL_TO=%s",
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


def send_email(subject: str, body: str, image_path=None, to: str = None) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM or SMTP_USER
    msg["To"] = to or EMAIL_TO
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


def notify(chans, subject, body, image_path=None, email_to: str = None) -> None:
    for ch in chans:
        try:
            if ch == "telegram":
                send_telegram(subject, body, image_path)
            elif ch == "email":
                send_email(subject, body, image_path, to=email_to)
            log.info("Notify: %s OK", ch)
        except Exception as e:
            log.error("Notify: %s FAILED: %s", ch, e)


# --------------------------------------------------------------------------- #
# CAPTCHA preprocessing
# --------------------------------------------------------------------------- #
def preprocess_captcha(b64_png: str, attempt: int) -> str:
    """
    Scale the CAPTCHA image 3× and boost contrast before sending to the vision
    model. More pixels → much better OCR accuracy on small/distorted characters.
    Also saves the preprocessed PNG to screenshots/ so you can inspect what the
    model actually saw.
    """
    raw_bytes = base64.b64decode(b64_png)
    img = Image.open(io.BytesIO(raw_bytes)).convert("L")  # grayscale

    log.info("    CAPTCHA raw size: %dx%d px", img.width, img.height)

    # Remove the salt-and-pepper dot noise that confuses OCR (median filter is
    # ideal for this kind of speckle while preserving digit edges)
    img = img.filter(ImageFilter.MedianFilter(size=3))

    # Scale up 3× with high-quality resampling
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)

    # Boost contrast so digit outlines separate from the 3D drop-shadow
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    img = img.convert("RGB")

    log.info("    CAPTCHA after preprocessing: %dx%d px (denoised)", img.width, img.height)

    # Save for debugging (visible in the screenshots artifact)
    out_path = f"{SHOT_DIR}/captcha_{attempt}.png"
    img.save(out_path, format="PNG")
    log.info("    CAPTCHA saved to %s", out_path)

    # Re-encode to base64 for the API
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------- #
# CAPTCHA solving (Claude vision)
# --------------------------------------------------------------------------- #
def solve_captcha(b64_png: str) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=VISION_MODEL,
        max_tokens=20,
        messages=[{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": b64_png}},
            {"type": "text", "text": (
                "This CAPTCHA contains exactly 4 digits. "
                "IMPORTANT: only the digits 0-9 are used — there are NO letters at all. "
                "What looks like O is the digit 0. What looks like I or L is the digit 1. "
                "What looks like E or B is 8 or 3. What looks like S is 5. What looks like Z is 2. "
                "Reply with ONLY the 4 digits — no letters, no spaces, no explanation."
            )},
        ]}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    # Strip everything except digits — if model still returns a letter, retry
    code = re.sub(r"[^0-9]", "", raw.strip())[:4]
    log.debug("    Vision raw output: %r -> cleaned: %r", raw.strip(), code)
    return code


# --------------------------------------------------------------------------- #
# Page helpers
# --------------------------------------------------------------------------- #
def grab_captcha_b64(page) -> str:
    """Wait for the inline base64 CAPTCHA image to appear and return its data."""
    page.wait_for_function(
        """() => {
            const el = document.querySelector('.imageCaptcha');
            return el && el.src && el.src.startsWith('data:image');
        }""",
        timeout=NAV_TIMEOUT_MS,
    )
    src = page.get_attribute(".imageCaptcha", "src")
    return src.split(",", 1)[1]


def dismiss_swal(page) -> bool:
    """Dismiss a SweetAlert2 popup if present. Returns True if one was found."""
    popup = page.query_selector(".swal2-popup")
    if not popup:
        return False
    log.info("    Dismissing error dialog")
    try:
        page.click(".swal2-confirm", timeout=2000)
        page.wait_for_timeout(400)
    except Exception:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:
            pass
    return True


def trip_selects(page):
    """All trip-form <select>s, excluding the language switcher, in DOM order."""
    out = []
    for h in page.query_selector_all("select"):
        if (h.get_attribute("id") or "") == "language" or \
           (h.get_attribute("name") or "") == "language":
            continue
        out.append(h)
    return out


def _norm(s: str) -> str:
    """Collapse all whitespace (spaces, newlines, tabs) to a single space."""
    return re.sub(r"\s+", " ", s).strip().lower()


def fire_change(handle):
    """Fire native + jQuery change so cascading AJAX dropdowns react."""
    try:
        handle.evaluate(
            "el => { el.dispatchEvent(new Event('change', {bubbles: true}));"
            " if (window.jQuery) { try { window.jQuery(el).trigger('change'); } catch(e){} } }"
        )
    except Exception:
        pass


def select_by_text(handle, wanted: str) -> bool:
    options = handle.eval_on_selector_all(
        "option", "els => els.map(e => e.textContent.trim()).filter(Boolean)")
    log.info("    available options: %s", options)
    w = _norm(wanted)
    chosen, how = None, None
    for opt in options:
        if _norm(opt) == w:
            chosen, how = opt, "exact"
            break
    if chosen is None:
        for opt in options:
            if w in _norm(opt):
                chosen, how = opt, "fuzzy"
                break
    if chosen is None:
        log.warning("    NO option matched %r in %s", wanted, options)
        return False
    handle.select_option(label=chosen)
    fire_change(handle)  # ensure the cascade AJAX fires
    log.info("    selected (%s): %r", how, chosen)
    return True


def wait_dropdown_ready(page, index, timeout_ms=10000) -> bool:
    """Wait until the select at DOM index (excl. language) has >1 option."""
    try:
        page.wait_for_function(
            f"""() => {{
                const sel = Array.from(document.querySelectorAll('select'))
                    .filter(s => s.id !== 'language' && s.name !== 'language');
                return sel.length > {index} && sel[{index}].options.length > 1;
            }}""",
            timeout=timeout_ms,
        )
        return True
    except PWTimeout:
        return False


def extract_dates(page):
    body_text = page.inner_text("body")
    input_vals = page.eval_on_selector_all(
        "input", "els => els.map(e => e.value || '').join(' ')")
    blob = body_text + " " + input_vals
    log.debug("    Scanning %d chars for dates", len(blob))
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

        # ---- Stage 1: CAPTCHA ---------------------------------------------- #
        reached_form = False
        with step("landing page + CAPTCHA"):

            # Initial page load
            log.info("Loading landing page: %s", BASE_URL)
            page.goto(BASE_URL, wait_until="domcontentloaded")
            log.info("Loaded: url=%s  title=%r", page.url, page.title())
            page.screenshot(path=f"{SHOT_DIR}/01_landing.png")

            # The site shows an intro SweetAlert2 overlay to fresh browser sessions
            # (no cookies). Dismiss it so it doesn't block the confirm button.
            overlay = page.query_selector(".swal2-container")
            if overlay and overlay.is_visible():
                log.info("Intro overlay detected on page load — dismissing")
                page.keyboard.press("Escape")
                page.wait_for_timeout(600)
            else:
                log.debug("No intro overlay detected")

            for attempt in range(1, CAPTCHA_RETRIES + 1):
                log.info("--- CAPTCHA attempt %d / %d ---", attempt, CAPTCHA_RETRIES)

                # Grab & preprocess CAPTCHA
                b64_raw = grab_captcha_b64(page)
                log.info("CAPTCHA grabbed (%d raw b64 chars)", len(b64_raw))
                b64_proc = preprocess_captcha(b64_raw, attempt)

                # Solve
                code = solve_captcha(b64_proc)
                log.info("CAPTCHA solved as %r (len=%d)", code, len(code))

                if len(code) != 4:
                    log.warning("Solved code is not 4 chars — skipping submit, "
                                "refreshing page for a new CAPTCHA")
                    page.reload(wait_until="domcontentloaded")
                    continue

                # Submit — use JS click so any residual overlay can't block it
                page.fill("#mailConfirmCodeControl", code)
                log.info("Filled input, submitting via JS click...")
                page.evaluate("document.querySelector('#confirmationbtn').click()")

                # Wait for the page response (form POST causes a navigation)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except PWTimeout:
                    log.warning("Navigation timed out after submit")

                # Short pause so inline JS (e.g. SweetAlert init) has time to run
                page.wait_for_timeout(700)

                # Decide: success or wrong code?
                body_text = page.inner_text("body")
                if "TRIP INFORMATION" in body_text.upper():
                    reached_form = True
                    log.info("SUCCESS: reached trip form. url=%s", page.url)
                    break

                # Wrong code — take screenshot, dismiss dialog
                page.screenshot(path=f"{SHOT_DIR}/02_captcha_fail_{attempt}.png")
                had_dialog = dismiss_swal(page)
                log.warning(
                    "Wrong code (dialog=%s). "
                    "Server has returned a fresh CAPTCHA — retrying without reload.",
                    had_dialog,
                )
                # No page.goto() needed: the redirect-back-to-landing page
                # already contains a new CAPTCHA, ready for the next iteration.

            if not reached_form:
                page.screenshot(path=f"{SHOT_DIR}/02_no_form.png")
                raise RuntimeError(
                    f"Could not pass CAPTCHA after {CAPTCHA_RETRIES} attempts. "
                    f"Check screenshots/captcha_*.png to see what the model saw. "
                    f"If the characters were misread, try setting "
                    f"VISION_MODEL to a stronger model — see console.anthropic.com/models."
                )

        page.screenshot(path=f"{SHOT_DIR}/03_trip_form.png")

        # ---- Stage 2: dropdowns -------------------------------------------- #
        with step("fill dropdowns"):
            for i, (field_name, wanted) in enumerate(SELECTIONS):
                # Dropdowns 2-6 are populated by AJAX after the previous one is
                # selected. If this dropdown is still empty, the previous cascade
                # (sometimes) failed — re-fire the previous selection and wait again.
                if i >= 1:
                    log.info("    Waiting for dropdown %d/%d [%s] to populate...",
                             i + 1, len(SELECTIONS), field_name)
                    ready = wait_dropdown_ready(page, i, timeout_ms=10000)
                    for retry in range(1, 4):  # up to 3 re-triggers
                        if ready:
                            break
                        log.warning("    Dropdown %d empty — re-triggering previous "
                                    "selection (retry %d/3)", i + 1, retry)
                        prev = trip_selects(page)
                        if i - 1 < len(prev):
                            fire_change(prev[i - 1])
                        ready = wait_dropdown_ready(page, i, timeout_ms=10000)
                    if ready:
                        log.info("    Dropdown %d ready", i + 1)
                    else:
                        log.warning("    Dropdown %d still empty after retries; proceeding", i + 1)

                selects = trip_selects(page)
                log.info("Dropdown %d/%d [%s] want=%r  (%d selects on page)",
                         i + 1, len(SELECTIONS), field_name, wanted, len(selects))
                if i >= len(selects):
                    raise RuntimeError(
                        f"Only {len(selects)} dropdowns found, need {len(SELECTIONS)}. "
                        "See screenshots/03_trip_form.png")
                select_by_text(selects[i], wanted)
                time.sleep(0.5)  # brief pause for change event to propagate

            page.wait_for_timeout(3000)
            page.screenshot(path=f"{SHOT_DIR}/04_after_selections.png", full_page=True)

        # ---- Stage 3: read dates ------------------------------------------- #
        with step("read dates"):
            dates = extract_dates(page)
            log.info("Parsed %d date(s): %s",
                     len(dates), [d.isoformat() for d in dates])
            if not dates:
                # Check whether the site explicitly says no slots exist.
                # "There is no availbale date." (site has a typo, match loosely)
                body_lower = page.inner_text("body").lower()
                # Match the site's actual typo ("availbale") and any future correction.
                # "no avail" is the common prefix of both spellings.
                if "no avail" in body_lower or "there is no" in body_lower:
                    log.info("Site reports no available dates — nothing to notify.")
                else:
                    raise RuntimeError(
                        "No dates parsed and no 'no available date' message found. "
                        "See screenshots/04_after_selections.png.")
            else:
                log.info("Earliest: %s  |  threshold: %s",
                         dates[0].isoformat(), threshold.isoformat())

        # ---- Stage 4: notify ----------------------------------------------- #
        with step("decide + notify"):
            if not dates:
                log.info("No dates available on site — no notification sent.")
            elif dates[0] < threshold:
                earliest = dates[0]
                human = ", ".join(d.strftime("%d-%m-%Y") for d in dates[:6])
                subject = "\U0001F6A8 Earlier German visa slot available!"
                body = (f"Earliest: {earliest.strftime('%d-%m-%Y')} "
                        f"(target was {threshold.strftime('%d-%m-%Y')})\n"
                        f"Available: {human}\n"
                        f"Book: {BASE_URL}")
                log.info("EARLIER DATE FOUND -> notifying via %s",
                         ", ".join(chans))
                notify(chans, subject, body,
                       f"{SHOT_DIR}/04_after_selections.png")
            else:
                log.info("Earliest (%s) is not before threshold (%s). No notification.",
                         dates[0].isoformat(), threshold.isoformat())

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
                       f"Failed at step: {_CURRENT_STEP}\nError: {e}",
                       email_to=EMAIL_ERROR_TO)
        sys.exit(0)
