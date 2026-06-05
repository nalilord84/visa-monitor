#!/usr/bin/env python3
"""
CAPTCHA debugger — run this once to diagnose OCR issues.

What it does (no form submission, safe to run repeatedly):
  1. Opens the landing page visibly (headless=False so you can see it)
  2. Saves the FULL PAGE screenshot
  3. Extracts the .imageCaptcha src and saves it as debug/captcha_raw.png
  4. Preprocesses it (3x scale + contrast) and saves debug/captcha_processed.png
  5. Asks Claude what BOTH images say, so you can compare
  6. Prints a side-by-side result

Look at the PNG files — if captcha_raw.png matches what you see on screen,
the grab is working. If the model still reads it wrong, it's a model quality issue.
"""
import base64, io, os, sys

BASE_URL      = os.environ.get("BASE_URL", "https://ru-appointment.visametric.com/en")
VISION_MODEL  = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")
API_KEY       = os.environ.get("ANTHROPIC_API_KEY")

if not API_KEY:
    sys.exit("ERROR: ANTHROPIC_API_KEY not set")

try:
    from PIL import Image, ImageEnhance
    from anthropic import Anthropic
    from playwright.sync_api import sync_playwright
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install playwright anthropic Pillow")

os.makedirs("debug", exist_ok=True)

# ── 1. Grab the CAPTCHA from the live page ──────────────────────────────────
print(f"\nOpening {BASE_URL} ...")
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)  # visible so you can compare
    ctx     = browser.new_context(locale="en-US")
    page    = ctx.new_page()
    page.goto(BASE_URL, wait_until="domcontentloaded")

    # Full page screenshot — what the browser sees
    page.screenshot(path="debug/full_page.png")
    print("  Saved: debug/full_page.png  (full page, visible in browser)")

    # Wait for the inline base64 CAPTCHA to be injected by JS
    page.wait_for_function(
        """() => {
            const el = document.querySelector('.imageCaptcha');
            return el && el.src && el.src.startsWith('data:image');
        }""",
        timeout=15000,
    )
    src     = page.get_attribute(".imageCaptcha", "src")
    media   = src.split(";")[0].replace("data:", "")   # e.g. "image/png"
    b64_raw = src.split(",", 1)[1]
    print(f"  .imageCaptcha src: {media}, {len(b64_raw)} base64 chars")

    browser.close()

# ── 2. Save raw image ───────────────────────────────────────────────────────
raw_bytes = base64.b64decode(b64_raw)
with open("debug/captcha_raw.png", "wb") as f:
    f.write(raw_bytes)

img_raw = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
print(f"  Raw CAPTCHA: {img_raw.width}x{img_raw.height} px → debug/captcha_raw.png")

# ── 3. Preprocess: 3× scale + contrast boost ───────────────────────────────
img_proc = img_raw.resize((img_raw.width * 3, img_raw.height * 3), Image.LANCZOS)
img_proc = ImageEnhance.Contrast(img_proc).enhance(2.5)
img_proc = ImageEnhance.Sharpness(img_proc).enhance(2.0)
img_proc.save("debug/captcha_processed.png")

buf = io.BytesIO()
img_proc.save(buf, "PNG")
b64_proc = base64.b64encode(buf.getvalue()).decode()
print(f"  Processed:   {img_proc.width}x{img_proc.height} px → debug/captcha_processed.png")

# ── 4. Ask the model about both versions ────────────────────────────────────
client = Anthropic(api_key=API_KEY)
PROMPT = (
    "This CAPTCHA contains exactly 4 digits (only digits 0–9, no letters). "
    "Reply with ONLY those 4 digits. Nothing else."
)

print(f"\nAsking {VISION_MODEL} ...\n")
results = {}
for label, b64 in [("RAW (original small size)", b64_raw),
                   ("PROCESSED (3× scale + contrast)", b64_proc)]:
    msg = client.messages.create(
        model=VISION_MODEL, max_tokens=10,
        messages=[{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    answer = "".join(b.text for b in msg.content if b.type == "text").strip()
    results[label] = answer
    print(f"  {label}")
    print(f"    Model says: {answer!r}")

print("\n" + "─"*60)
print("WHAT TO CHECK")
print("─"*60)
print("1. Open debug/full_page.png — note the 4-digit CAPTCHA you see.")
print("2. Open debug/captcha_raw.png — does it show the SAME digits?")
print("   If NO → the grab is reading the wrong element or stale image.")
print("   If YES → the model is just reading badly (OCR quality issue).")
print("3. Open debug/captcha_processed.png — the enlarged version.")
print("4. Compare model answers above to what you can read in the images.")
print()
print(f"Results summary:")
for label, ans in results.items():
    print(f"  {label[:30]:30s}  →  {ans}")
