"""
generate_carousel.py
════════════════════════════════════════════════════════════════
LinkedIn 9:16 Carousel Pipeline — Complete Overhaul

MODULE A  Parse input/topics.txt → today's topic
MODULE B  Gemini 2.5 Flash + Google Search → trending content
          + 3 structured image prompts (hook / detail / action)
MODULE C  Gemini 2.5 Flash Image → generate AI background for each slide
          Pillow   → overlay professional text on each background
          (waits for each generation to complete before proceeding)
MODULE D  rclone   → sync output/*.jpg to Google Drive folder

Output:  slide_1_DDMMYY.jpg  slide_2_DDMMYY.jpg  slide_3_DDMMYY.jpg
         caption_DDMMYY.txt
════════════════════════════════════════════════════════════════
"""

import io
import os
import sys
import json
import time
import logging
import subprocess
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

# ── Directories ───────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
INPUT_DIR  = ROOT / "input"
LOGS_DIR   = ROOT / "logs"

for _d in (OUTPUT_DIR, INPUT_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Logging (rotating file + stdout) ─────────────────────────
_log_file = LOGS_DIR / f"carousel_{date.today():%Y%m%d}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            _log_file, maxBytes=5 * 1024 * 1024,
            backupCount=7, encoding="utf-8"
        ),
    ],
)
log = logging.getLogger("carousel")

# ── Environment ───────────────────────────────────────────────
GOOGLE_API_KEY        = os.environ["GOOGLE_API_KEY"]
RCLONE_REMOTE         = os.getenv("RCLONE_REMOTE_NAME", "gdrive")
RCLONE_FOLDER         = os.getenv("RCLONE_FOLDER_ID", "").strip()
RCLONE_CONFIG_BASE64  = os.getenv("RCLONE_CONFIG_BASE64", "").strip()
BRAND_NAME            = os.getenv("BRAND_NAME", "YourBrand")
CTA_LINK              = os.getenv("CTA_LINK", "linkedin.com/in/yourprofile")

DATE_TAG    = date.today().strftime("%d%m%y")
DATE_HUMAN  = date.today().strftime("%B %d, %Y")
TODAY       = date.today().strftime("%A").lower()   # e.g. "monday"

# ── Canvas spec ───────────────────────────────────────────────
W, H = 1080, 1920   # 9:16 portrait

# ── Colour palette (professional dark / Google-blue) ─────────
PAL = {
    "bg"        : (8,  14,  26),    # #080E1A deep navy
    "bg_card"   : (16, 24,  48),    # #101830 dark card
    "accent"    : (37, 99,  235),   # #2563EB Google blue
    "accent2"   : (99, 179, 237),   # #63B3ED light blue
    "text"      : (249, 250, 251),  # #F9FAFB near-white
    "muted"     : (156, 163, 175),  # #9CA3AF gray
    "highlight" : (251, 191, 36),   # #FBBF24 amber
    "danger"    : (239, 68,  68),   # #EF4444 red
    "success"   : (34,  197, 94),   # #22C55E green
}

# ── Font discovery ────────────────────────────────────────────
_FONT_DIRS = [
    "/usr/share/fonts/truetype/liberation/",
    "/usr/share/fonts/truetype/ubuntu/",
    "/usr/share/fonts/truetype/dejavu/",
    "/usr/share/fonts/opentype/noto/",
    "C:/Windows/Fonts/",
    "/System/Library/Fonts/",
    "/Library/Fonts/",
]
_FONTS = {
    "bold"   : ["LiberationSans-Bold.ttf", "Ubuntu-Bold.ttf",
                "DejaVuSans-Bold.ttf", "ArialBD.ttf", "Arial Bold.ttf"],
    "regular": ["LiberationSans-Regular.ttf", "Ubuntu-Regular.ttf",
                "DejaVuSans.ttf", "Arial.ttf"],
}


def _font(style: str, size: int) -> ImageFont.FreeTypeFont:
    """Return the best available TrueType font. Never crashes."""
    for directory in _FONT_DIRS:
        for name in _FONTS.get(style, _FONTS["regular"]):
            p = Path(directory) / name
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    continue
    return ImageFont.load_default()


# ════════════════════════════════════════════════════════════════
# MODULE A — TOPIC DISCOVERY FROM INPUT FILE
# ════════════════════════════════════════════════════════════════

def load_today_topic() -> str:
    """
    Parse input/topics.txt and return today's topic.
    If the file is missing or today's day isn't listed, it uses a default fallback topic.
    """
    topics_file = INPUT_DIR / "topics.txt"
    
    # 1. Define a solid fallback topic just in case the file is missing
    fallback_topic = "Future of IT Managed Services and Zero-Trust Security"

    # 2. Check if file exists. If not, don't crash, just use the fallback.
    if not topics_file.exists():
        log.warning(f"⚠️ Topics file missing at {topics_file}. Using fallback topic.")
        return fallback_topic

    # 3. If it does exist, read the file
    topics: dict[str, str] = {}
    for line in topics_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        day, _, topic = line.partition("=")
        topics[day.strip().lower()] = topic.strip()

    # 4. Check if today's day (e.g., 'monday') is actually written in the file
    if TODAY not in topics:
        log.warning(f"⚠️ No topic found for '{TODAY}' in {topics_file.name}. Using fallback topic.")
        return fallback_topic

    # 5. Success
    topic = topics[TODAY]
    log.info(f"  ✓ Today is {TODAY.capitalize()} → topic: '{topic}'")
    return topic


# ════════════════════════════════════════════════════════════════
# MODULE B — GEMINI 2.5 FLASH CONTENT + PROMPT GENERATION
# ════════════════════════════════════════════════════════════════

def generate_content(topic: str) -> dict:
    """
    Call Gemini 2.5 Flash with Google Search grounding to:
    1. Find the most relevant trending news on the topic today
    2. Structure it into 3-slide carousel content
    3. Generate a background image prompt for each slide

    Returns a validated dict with slide content and image prompts.
    Retries up to 3 times with 20-second backoff.
    """
    log.info(f"━━━━ MODULE B: Gemini 2.5 Flash — Topic: {topic} ━━━━")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GOOGLE_API_KEY)

    prompt = f"""
You are a senior cybersecurity analyst and LinkedIn content strategist at a Tier-1 IT company.

Today: {DATE_HUMAN}
Topic: "{topic}"

TASK:
Search the web RIGHT NOW for the single most impactful, trending news story or development
related to "{topic}" published in the last 7 days. Base your content strictly on real,
verifiable information.

Generate a 3-slide LinkedIn carousel (9:16 portrait, 1080×1920px) with this structure:
  Slide 1: VIRAL HOOK    — grabs attention in 1 second, references the real news
  Slide 2: WHAT'S HAPPENING — key facts, data points, impact (short bullet format)
  Slide 3: WHAT TO DO   — 3 actionable steps the reader can take NOW

Return ONLY a raw JSON object. No markdown. No backticks. No explanation.

{{
  "topic": "{topic}",
  "trending_headline": "<The real trending news headline you found — under 15 words>",
  "source": "<Publication name and approximate date>",
  "daily_angle": "<One sentence: what makes this story uniquely important today>",
  "caption": "<LinkedIn post caption — 2 punchy sentences + 5 relevant hashtags>",
  "slides": [
    {{
      "id": 1,
      "type": "hook",
      "badge": "<2-3 word badge — e.g. BREAKING or ALERT or MUST READ>",
      "headline": "<Viral hook headline — max 10 words, creates urgency or FOMO>",
      "subtext": "<Supporting sentence — max 18 words, adds credibility with a stat or detail>",
      "image_bg_prompt": "<50-word prompt for Imagen 3: describe a dark, professional, abstract {topic} visualization — NO TEXT in the image, cinematic lighting, deep navy/dark blue palette, subtle grid lines or circuit patterns, dramatic depth of field, 4K ultra detailed>"
    }},
    {{
      "id": 2,
      "type": "detail",
      "section_title": "WHAT'S HAPPENING",
      "points": [
        "<Specific data point or fact — under 15 words>",
        "<Second specific data point — under 15 words>",
        "<Third specific data point — under 15 words>"
      ],
      "image_bg_prompt": "<50-word prompt for Imagen 3: dark professional data/analytics visualization, NO TEXT, dark blue/indigo theme, abstract flowing data streams, corporate tech aesthetic>"
    }},
    {{
      "id": 3,
      "type": "action",
      "section_title": "WHAT YOU SHOULD DO",
      "steps": [
        "<Actionable step 1 — imperative verb, under 12 words>",
        "<Actionable step 2 — imperative verb, under 12 words>",
        "<Actionable step 3 — imperative verb, under 12 words>"
      ],
      "cta": "<Call to action — max 8 words, e.g. 'Save this for your security team'>",
      "image_bg_prompt": "<50-word prompt for Imagen 3: dark professional shield/protection visualization, NO TEXT, dark teal/blue theme, abstract geometric security patterns, enterprise tech feel>"
    }}
  ]
}}
"""

    for attempt in range(1, 4):
        try:
            log.info(f"  Gemini call attempt {attempt}/3 ...")
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.75,
                    max_output_tokens=2500,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            raw = response.text.strip()

            # Strip accidental markdown fences
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip()

            data = json.loads(raw)

            # Validate required structure
            assert "slides" in data, "Missing 'slides' key"
            assert len(data["slides"]) == 3, f"Expected 3 slides, got {len(data['slides'])}"
            for s in data["slides"]:
                assert "image_bg_prompt" in s, f"Slide {s.get('id')} missing image_bg_prompt"

            log.info(f"  ✓ Headline: {data.get('trending_headline', 'N/A')}")
            log.info(f"  ✓ Source  : {data.get('source', 'N/A')}")
            log.info(f"  ✓ Angle   : {data.get('daily_angle', 'N/A')}")

            # Save for audit
            (OUTPUT_DIR / f"content_{DATE_TAG}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return data

        except (json.JSONDecodeError, AssertionError, KeyError) as e:
            log.warning(f"  Attempt {attempt}/3 parse error: {e}")
        except Exception as e:
            log.warning(f"  Attempt {attempt}/3 API error: {e}")

        if attempt < 3:
            log.info("  Waiting 20s before retry ...")
            time.sleep(20)

    raise RuntimeError("MODULE B: Gemini failed after 3 attempts.")


# ════════════════════════════════════════════════════════════════
# MODULE C — IMAGE GENERATION (Gemini 2.5 Flash Image) + TEXT OVERLAY (Pillow)
# ════════════════════════════════════════════════════════════════

# ── C1: Gemini 2.5 Flash Image background generation ──────────

def generate_background(prompt: str, slide_num: int) -> Image.Image:
    """
    Call Gemini 2.5 Flash Image to generate a 9:16 background image.
    Blocks until generation is complete (synchronous API).
    Returns PIL Image or a Pillow-generated fallback gradient.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GOOGLE_API_KEY)

    for attempt in range(1, 4):
        try:
            log.info(f"  [Slide {slide_num}] Gemini 2.5 Flash Image generation (attempt {attempt}/3) ...")

            response = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio="9:16",
                    ),
                ),
            )

            # Access the generated image bytes
            image_bytes = None
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    image_bytes = part.inline_data.data
                    break

            if not image_bytes:
                raise ValueError("Gemini 2.5 Flash Image returned no image data")

            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            img = img.resize((W, H), Image.LANCZOS)
            log.info(f"  [Slide {slide_num}] ✓ Background generated via Gemini 2.5 Flash Image")
            return img

        except Exception as e:
            log.warning(f"  [Slide {slide_num}] Gemini 2.5 Flash Image attempt {attempt}/3: {e}")
            if attempt < 3:
                log.info("  Waiting 15s before retry ...")
                time.sleep(15)

    # Fallback: Pillow gradient background
    log.warning(f"  [Slide {slide_num}] Gemini 2.5 Flash Image failed — using gradient fallback")
    return _make_gradient_bg(slide_num)


def _make_gradient_bg(seed: int) -> Image.Image:
    """Generate a professional dark gradient background with Pillow."""
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    # Different shade per slide for visual variety
    top_colors    = [(8, 14, 26), (10, 16, 40), (6, 18, 38)]
    bottom_colors = [(14, 30, 70), (20, 14, 60), (12, 40, 68)]
    top    = top_colors[seed % 3]
    bottom = bottom_colors[seed % 3]
    for y in range(H):
        t = y / H
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    return img


# ── C2: Pillow rendering helpers ──────────────────────────────

def _draw_text_wrapped(draw: ImageDraw.Draw,
                        text: str, xy: tuple,
                        font: ImageFont.FreeTypeFont,
                        fill: tuple,
                        max_width: int,
                        line_gap: int = 14,
                        stroke_width: int = 0,
                        stroke_fill: tuple = (0, 0, 0)) -> int:
    """
    Draw word-wrapped text. Returns total pixel height consumed.
    Supports optional stroke (outline) for contrast.
    """
    words   = text.split()
    lines   = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    x, y    = xy
    line_h  = draw.textbbox((0, 0), "Ag", font=font)[3] + line_gap
    total   = 0
    for line in lines:
        draw.text(
            (x, y), line,
            font=font, fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        y     += line_h
        total += line_h
    return total


def _draw_rounded_rect(draw: ImageDraw.Draw,
                        x: int, y: int, w: int, h: int,
                        fill: tuple, radius: int = 16):
    try:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill)
    except AttributeError:
        draw.rectangle([x, y, x + w, y + h], fill=fill)


def _overlay_dark(img: Image.Image, alpha: int = 160) -> Image.Image:
    """Apply a semi-transparent dark overlay for text readability."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, alpha))
    base    = img.convert("RGBA")
    merged  = Image.alpha_composite(base, overlay)
    return merged.convert("RGB")


def _draw_top_accent(draw: ImageDraw.Draw, height: int = 8):
    draw.rectangle([0, 0, W, height], fill=PAL["accent"])


def _draw_footer(draw: ImageDraw.Draw, slide_num: int):
    """Brand name left, slide counter right."""
    bar_y = H - 80
    draw.rectangle([0, bar_y, W, H], fill=(8, 14, 26))
    draw.rectangle([0, bar_y, W, bar_y + 1], fill=PAL["accent"])
    f = _font("regular", 30)
    draw.text((52, bar_y + 24), BRAND_NAME, font=f, fill=PAL["muted"])
    counter = f"{slide_num} / 3"
    bw      = draw.textbbox((0, 0), counter, font=f)[2]
    draw.text((W - bw - 52, bar_y + 24), counter, font=f, fill=PAL["muted"])


def _draw_swipe_arrow(draw: ImageDraw.Draw):
    """Right-arrow indicator on slides 1-2."""
    f    = _font("bold", 72)
    text = "→"
    bx   = draw.textbbox((0, 0), text, font=f)
    tw   = bx[2] - bx[0]
    th   = bx[3] - bx[1]
    pad  = 18
    x    = W - tw - 60
    y    = H - th - 110
    _draw_rounded_rect(draw, x - pad, y - pad, tw + pad * 2, th + pad * 2,
                       fill=PAL["accent"], radius=50)
    draw.text((x, y), text, font=f, fill=PAL["text"])


# ── C3: Slide 1 — Hook ────────────────────────────────────────

def render_slide_1(bg: Image.Image, slide: dict) -> Image.Image:
    img  = _overlay_dark(bg, alpha=172)
    draw = ImageDraw.Draw(img)

    _draw_top_accent(draw)

    # Topic label
    f_label = _font("regular", 34)
    draw.text((60, 110), slide.get("topic_label", "").upper(),
              font=f_label, fill=PAL["accent2"])
    draw.rectangle([60, 156, 180, 162], fill=PAL["accent"])

    # BREAKING badge
    badge = slide.get("badge", "BREAKING").upper()
    f_badge = _font("bold", 30)
    bw    = draw.textbbox((0, 0), badge, font=f_badge)[2]
    _draw_rounded_rect(draw, 60, 200, bw + 32, 52, fill=PAL["danger"], radius=8)
    draw.text((76, 210), badge, font=f_badge, fill=PAL["text"])

    # Main headline — very large, centred vertically in upper two-thirds
    headline  = slide.get("headline", "")
    f_head    = _font("bold", 88)
    max_w     = W - 100
    # Measure line count for vertical centering
    words     = headline.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textbbox((0, 0), test, font=f_head)[2] <= max_w:
            cur = test
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    lh      = draw.textbbox((0, 0), "Ag", font=f_head)[3] + 16
    total_h = len(lines) * lh
    y_start = (H * 2 // 3 - total_h) // 2 + 80

    for line in lines:
        lw = draw.textbbox((0, 0), line, font=f_head)[2]
        draw.text(
            ((W - lw) // 2, y_start),
            line, font=f_head, fill=PAL["text"],
            stroke_width=3, stroke_fill=(0, 0, 0)
        )
        y_start += lh

    # Accent underline
    draw.rectangle([(W - 200) // 2, y_start + 12, (W + 200) // 2, y_start + 18],
                   fill=PAL["accent"])

    # Subtext
    subtext = slide.get("subtext", "")
    f_sub   = _font("regular", 44)
    _draw_text_wrapped(
        draw, subtext, (60, y_start + 40),
        font=f_sub, fill=PAL["muted"],
        max_width=W - 120, stroke_width=2, stroke_fill=(0, 0, 0)
    )

    _draw_swipe_arrow(draw)
    _draw_footer(draw, 1)
    return img


# ── C4: Slide 2 — Details ─────────────────────────────────────

def render_slide_2(bg: Image.Image, slide: dict) -> Image.Image:
    img  = _overlay_dark(bg, alpha=180)
    draw = ImageDraw.Draw(img)

    _draw_top_accent(draw)

    # Section badge
    f_section = _font("bold", 32)
    sec_title = slide.get("section_title", "WHAT'S HAPPENING")
    bw        = draw.textbbox((0, 0), sec_title, font=f_section)[2]
    _draw_rounded_rect(draw, 60, 110, bw + 36, 54, fill=PAL["accent"], radius=10)
    draw.text((78, 120), sec_title, font=f_section, fill=PAL["text"])

    # Headline area (topic context)
    headline = slide.get("headline", slide.get("section_title", "Key Facts"))
    f_head   = _font("bold", 66)
    _draw_text_wrapped(
        draw, headline, (60, 200),
        font=f_head, fill=PAL["text"],
        max_width=W - 120, stroke_width=2, stroke_fill=(0, 0, 0)
    )

    # Data points as styled cards
    points  = slide.get("points", [])
    card_y  = 420
    card_h  = 200
    gap     = 30
    f_point = _font("regular", 44)
    f_num   = _font("bold", 52)

    for idx, point in enumerate(points[:3]):
        # Card background
        _draw_rounded_rect(draw, 48, card_y, W - 96, card_h,
                           fill=(16, 24, 52), radius=20)
        # Left accent strip
        draw.rectangle([48, card_y, 60, card_y + card_h], fill=PAL["accent"])
        # Number badge
        num_txt = f"0{idx + 1}"
        draw.text((80, card_y + 20), num_txt, font=f_num, fill=PAL["accent"])
        # Point text
        _draw_text_wrapped(
            draw, point, (80, card_y + 85),
            font=f_point, fill=PAL["text"],
            max_width=W - 160, line_gap=10
        )
        card_y += card_h + gap

    _draw_swipe_arrow(draw)
    _draw_footer(draw, 2)
    return img


# ── C5: Slide 3 — Actions ─────────────────────────────────────

def render_slide_3(bg: Image.Image, slide: dict) -> Image.Image:
    img  = _overlay_dark(bg, alpha=185)
    draw = ImageDraw.Draw(img)

    _draw_top_accent(draw)

    # Section badge
    f_section = _font("bold", 32)
    sec_title = slide.get("section_title", "WHAT YOU SHOULD DO")
    bw        = draw.textbbox((0, 0), sec_title, font=f_section)[2]
    _draw_rounded_rect(draw, 60, 110, bw + 36, 54, fill=PAL["success"], radius=10)
    draw.text((78, 120), sec_title, font=f_section, fill=(8, 14, 26))

    # Steps
    steps   = slide.get("steps", [])
    step_y  = 230
    f_step  = _font("regular", 46)
    f_num   = _font("bold", 56)
    step_h  = 240
    gap     = 24
    icons   = ["①", "②", "③"]

    for idx, step in enumerate(steps[:3]):
        # Step card
        _draw_rounded_rect(draw, 48, step_y, W - 96, step_h,
                           fill=(16, 24, 52), radius=20)
        # Number in accent circle
        num = str(idx + 1)
        nf  = _font("bold", 52)
        draw.ellipse([60, step_y + 20, 130, step_y + 90], fill=PAL["accent"])
        nw  = draw.textbbox((0, 0), num, font=nf)[2]
        draw.text((60 + (70 - nw) // 2, step_y + 26), num, font=nf, fill=PAL["text"])
        # Step text
        _draw_text_wrapped(
            draw, step, (148, step_y + 28),
            font=f_step, fill=PAL["text"],
            max_width=W - 220, line_gap=10
        )
        step_y += step_h + gap

    # CTA block — pinned above footer
    cta_text = slide.get("cta", "Save this for your team")
    cta_y    = H - 210
    cta_h    = 120
    _draw_rounded_rect(draw, 48, cta_y, W - 96, cta_h,
                       fill=PAL["accent"], radius=20)
    f_cta = _font("bold", 46)
    cw    = draw.textbbox((0, 0), cta_text, font=f_cta)[2]
    draw.text(((W - cw) // 2, cta_y + 34), cta_text, font=f_cta, fill=PAL["text"])

    _draw_footer(draw, 3)
    return img


# ── C6: Orchestrate all 3 slides ─────────────────────────────

def generate_all_slides(content: dict) -> list[Path]:
    """
    For each of 3 slides:
      1. Call Gemini 2.5 Flash Image to generate background (blocks until complete)
      2. Overlay professional text with Pillow
      3. Save JPEG to output/
    Returns list of saved file paths.
    """
    log.info("\n━━━━ MODULE C: Image Generation + Rendering ━━━━")

    topic   = content.get("topic", "")
    slides  = content["slides"]
    paths   = []

    render_fns = {
        "hook"  : render_slide_1,
        "detail": render_slide_2,
        "action": render_slide_3,
    }

    for slide in slides:
        slide_id  = slide["id"]
        slide_type = slide["type"]
        out_path  = OUTPUT_DIR / f"slide_{slide_id}_{DATE_TAG}.jpg"

        log.info(f"\n  Slide {slide_id}/3  [{slide_type}]")
        log.info(f"  Waiting for Gemini 2.5 Flash Image generation ...")

        # Inject topic label for slide 1
        slide["topic_label"] = topic

        # Generate AI background — pipeline sleeps here until complete
        bg = generate_background(slide["image_bg_prompt"], slide_num=slide_id)

        # Render text overlay
        render_fn = render_fns.get(slide_type)
        if render_fn is None:
            log.error(f"  Unknown slide type: {slide_type}")
            continue

        rendered = render_fn(bg, slide)
        rendered.save(str(out_path), "JPEG", quality=95, optimize=True)
        size_kb  = out_path.stat().st_size // 1024
        log.info(f"  ✓ Slide {slide_id} saved: {out_path.name} ({size_kb} KB)")

        paths.append(out_path)

        # Rate-limit buffer between consecutive Imagen calls
        if slide_id < 3:
            log.info("  Sleeping 12s before next image generation ...")
            time.sleep(12)

    return paths


# ════════════════════════════════════════════════════════════════
# MODULE D — RCLONE CLOUD SYNC
# ════════════════════════════════════════════════════════════════

def sync_via_rclone(file_paths: list[Path]) -> bool:
    """
    Run rclone copy to push all JPEGs to the configured Google Drive folder.
    Uses __id_ syntax when RCLONE_FOLDER_ID is set.
    Deletes local files on success to prevent disk bloat.
    Returns True on success.
    """
    if not file_paths:
        log.warning("  No files to sync.")
        return False

    if RCLONE_CONFIG_BASE64:
        try:
            import base64
            config_dir = Path.home() / ".config" / "rclone"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "rclone.conf"
            config_path.write_bytes(base64.b64decode(RCLONE_CONFIG_BASE64))
            log.info("  ✓ Decoded and wrote RCLONE_CONFIG_BASE64 to local rclone.conf")
        except Exception as e:
            log.warning(f"  Failed to decode/write RCLONE_CONFIG_BASE64: {e}")

    dest = (
        f"{RCLONE_REMOTE}:__id_{RCLONE_FOLDER}"
        if RCLONE_FOLDER else
        f"{RCLONE_REMOTE}:"
    )

    cmd = [
        "rclone", "copy",
        str(OUTPUT_DIR),
        dest,
        "--include", "*.jpg",
        "--include", "*.txt",
        "--transfers", "1",
    ]

    log.info(f"\n━━━━ MODULE D: rclone Sync → {dest} ━━━━")
    log.info(f"  Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        log.info("  ✓ rclone sync successful.")
        # Clean up local files after confirmed upload
        for p in file_paths:
            try:
                p.unlink()
                log.info(f"  🗑  Deleted local: {p.name}")
            except Exception as e:
                log.warning(f"  Could not delete {p.name}: {e}")
        return True
    else:
        log.error(f"  ✗ rclone failed (code {result.returncode})")
        log.error(f"  stderr: {result.stderr[-800:]}")
        log.warning("  Local files preserved for manual recovery.")
        return False


def save_caption(content: dict, topic: str):
    """Save the LinkedIn post caption to a text file."""
    p = OUTPUT_DIR / f"caption_{DATE_TAG}.txt"
    p.write_text(
        f"LinkedIn Carousel — {DATE_HUMAN}\n"
        f"Topic  : {topic}\n"
        f"Source : {content.get('source', 'N/A')}\n"
        f"Angle  : {content.get('daily_angle', 'N/A')}\n"
        f"\n{'=' * 60}\n\n"
        f"{content.get('caption', '')}\n",
        encoding="utf-8"
    )
    log.info(f"  ✓ Caption saved: {p.name}")


# ════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ════════════════════════════════════════════════════════════════

def main():
    start = datetime.now()
    log.info("=" * 60)
    log.info(f"CAROUSEL PIPELINE STARTED — {start:%Y-%m-%d %H:%M:%S}")
    log.info(f"  Today  : {TODAY.capitalize()}  ({DATE_HUMAN})")
    log.info(f"  Brand  : {BRAND_NAME}")
    log.info("=" * 60)

    # MODULE A — load today's topic
    topic = load_today_topic()

    # MODULE B — generate content + image prompts
    content = generate_content(topic)

    # MODULE C — generate images + render slides
    slide_paths = generate_all_slides(content)

    if not slide_paths:
        log.critical("No slides generated. Exiting.")
        raise SystemExit(1)

    # Save caption
    save_caption(content, topic)

    # MODULE D — sync to Google Drive via rclone
    sync_via_rclone(slide_paths)

    elapsed = int((datetime.now() - start).total_seconds())
    log.info("\n" + "=" * 60)
    log.info(f"✅ PIPELINE COMPLETE in {elapsed}s")
    log.info(f"   Topic    : {topic}")
    log.info(f"   Headline : {content.get('trending_headline', 'N/A')}")
    log.info(f"   Slides   : {len(slide_paths)}/3")
    log.info(f"   Log      : {_log_file}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()