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

def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert HEX color string to RGB tuple."""
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))


def generate_content(topic: str) -> dict:
    """
    Call Gemini 2.5 Flash with Google Search grounding to:
    1. Find the most relevant trending news on the topic today
    2. Structure it into 3-slide carousel content
    3. Generate a dynamic theme (colors and light/dark mode) suitable for the content
    4. Generate a background image prompt for each slide matching the theme style
    
    Returns a validated dict with slide content, theme, and image prompts.
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

Also, design a custom professional visual theme palette for the slides based on the content/vibe.
The theme must specify a mode ("light" or "dark") and a set of matching colors (in #HEX format).
Ensure contrast is extremely high:
- If mode is "light":
  - bg: light grey/blue/cream (e.g. #F4F6F9, #FAF9F6)
  - bg_card: clean white (#FFFFFF)
  - text: deep dark charcoal/blue (#0F172A, #1E293B)
  - muted: slate grey (#475569)
  - accent: professional blue/indigo (#2563EB, #4F46E5)
  - accent2: light blue/cyan (#0284C7, #0891B2)
  - success: dark green (#15803D)
  - danger: dark red (#B91C1C)
- If mode is "dark":
  - bg: deep navy/charcoal (#080E1A, #0F172A)
  - bg_card: dark card (#1E293B, #162448)
  - text: near-white (#F9FAFB)
  - muted: cool grey (#9CA3AF)
  - accent: vibrant blue (#2563EB)
  - accent2: light blue (#63B3ED)
  - success: vibrant green (#22C55E)
  - danger: vibrant red (#EF4444)

Return ONLY a raw JSON object. Do not include raw control characters or unescaped newlines in string fields.

{{
  "topic": "{topic}",
  "trending_headline": "trending news headline under 15 words",
  "source": "publication name and date",
  "daily_angle": "what makes this story uniquely important today",
  "caption": "LinkedIn post caption with hashtags",
  "theme": {{
    "mode": "light or dark",
    "bg": "bg color hex starting with hash",
    "bg_card": "bg_card color hex starting with hash",
    "accent": "accent color hex starting with hash",
    "accent2": "accent2 color hex starting with hash",
    "text": "text color hex starting with hash",
    "muted": "muted color hex starting with hash",
    "highlight": "highlight color hex starting with hash",
    "danger": "danger color hex starting with hash",
    "success": "success color hex starting with hash"
  }},
  "slides": [
    {{
      "id": 1,
      "type": "hook",
      "badge": "breaking or alert badge",
      "headline": "viral hook headline",
      "subtext": "supporting sentence",
      "image_bg_prompt": "Describe a professional model positioned on the right half of the image looking professional and thoughtful, with floating glowing 3D graphic icons and clean corporate vector shapes representing the slide topic positioned next to them on the right. The background must be a clean, solid, flat theme bg color backdrop, with the left half of the image being completely empty solid theme bg color negative space. Studio lighting, professional studio shot, 4K resolution, NO text."
    }},
    {{
      "id": 2,
      "type": "detail",
      "section_title": "WHAT'S HAPPENING",
      "points": [
        "data point 1",
        "data point 2",
        "data point 3"
      ],
      "image_bg_prompt": "Describe a different professional model on the right half of the image, holding or interacting with a glowing device that projects 3D charts, icons, and analytics graphics. The background must be a clean, solid, flat theme bg color backdrop, with the left half of the image being completely empty solid theme bg color negative space. Soft studio lighting, 4K resolution, NO text."
    }},
    {{
      "id": 3,
      "type": "action",
      "section_title": "WHAT YOU SHOULD DO",
      "steps": [
        "actionable step 1",
        "actionable step 2",
        "actionable step 3"
      ],
      "cta": "call to action under 8 words",
      "image_bg_prompt": "Describe a different professional model on the right half of the image looking confident, with a large, glowing abstract shield, checklist, or action graphic icon representing the slide content floating behind them on the right. The background must be a clean, solid, flat theme bg color backdrop, with the left half of the image being completely empty solid theme bg color negative space. Studio lighting, 4K resolution, NO text."
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

            data = json.loads(raw, strict=False)

            # Validate required structure
            assert "slides" in data, "Missing 'slides' key"
            assert len(data["slides"]) == 3, f"Expected 3 slides, got {len(data['slides'])}"
            for s in data["slides"]:
                assert "image_bg_prompt" in s, f"Slide {s.get('id')} missing image_bg_prompt"

            log.info(f"  ✓ Headline: {data.get('trending_headline', 'N/A')}")
            log.info(f"  ✓ Source  : {data.get('source', 'N/A')}")
            log.info(f"  ✓ Angle   : {data.get('daily_angle', 'N/A')}")
            log.info(f"  ✓ Theme Mode: {data.get('theme', {}).get('mode', 'N/A')}")

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


# ════════════════════════════════════════════════════════════════
# MODULE C — IMAGE GENERATION (Gemini 2.5 Flash Image) + TEXT OVERLAY (Pillow)
# ════════════════════════════════════════════════════════════════

# ── C1: Gemini 2.5 Flash Image background generation ──────────

import random

def generate_background(prompt: str, slide_num: int, pal: dict, mode: str) -> Image.Image:
    """
    Download a random background from gdrive:libg using rclone.
    Blocks until generation is complete.
    Returns PIL Image or a Pillow-generated fallback gradient.
    """
    for attempt in range(1, 4):
        try:
            log.info(f"  [Slide {slide_num}] Fetching random background from {RCLONE_REMOTE}:libg (attempt {attempt}/3) ...")

            list_cmd = ["rclone", "lsjson", f"{RCLONE_REMOTE}:libg"]
            result = subprocess.run(list_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError(f"rclone lsjson failed: {result.stderr}")
            
            files = json.loads(result.stdout)
            image_files = [f for f in files if not f.get("IsDir") and f.get("Name", "").lower().endswith(('.jpg', '.jpeg', '.png'))]
            
            if not image_files:
                raise ValueError(f"No image files found in {RCLONE_REMOTE}:libg")
                
            chosen = random.choice(image_files)
            file_name = chosen["Name"]
            
            log.info(f"  [Slide {slide_num}] Selected background: {file_name}")
            
            temp_dir = OUTPUT_DIR / "temp"
            temp_dir.mkdir(exist_ok=True)
            
            copy_cmd = ["rclone", "copy", f"{RCLONE_REMOTE}:libg/{file_name}", str(temp_dir)]
            copy_result = subprocess.run(copy_cmd, capture_output=True, text=True)
            if copy_result.returncode != 0:
                raise ValueError(f"rclone copy failed: {copy_result.stderr}")
                
            local_path = temp_dir / file_name
            
            img = Image.open(local_path).convert("RGB")
            
            img_ratio = img.width / img.height
            target_ratio = W / H
            if img_ratio > target_ratio:
                new_w = int(target_ratio * img.height)
                left = (img.width - new_w) // 2
                img = img.crop((left, 0, left + new_w, img.height))
            elif img_ratio < target_ratio:
                new_h = int(img.width / target_ratio)
                top = (img.height - new_h) // 2
                img = img.crop((0, top, img.width, top + new_h))
                
            img = img.resize((W, H), Image.LANCZOS)
            log.info(f"  [Slide {slide_num}] ✓ Background loaded from {RCLONE_REMOTE}:libg")
            
            local_path.unlink()
            
            return img

        except Exception as e:
            log.warning(f"  [Slide {slide_num}] rclone fetch attempt {attempt}/3: {e}")
            if attempt < 3:
                log.info("  Waiting 5s before retry ...")
                time.sleep(5)

    # Fallback: Pillow gradient background
    log.warning(f"  [Slide {slide_num}] rclone fetch failed — using gradient fallback")
    return _make_gradient_bg(pal, mode, slide_num)


def _make_gradient_bg(pal: dict, mode: str, seed: int) -> Image.Image:
    """Generate a professional gradient background with Pillow using the theme palette."""
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    
    # Base bg color
    base = pal["bg"]
    accent = pal["accent"]
    
    # Blend colors
    if mode == "light":
        top = base
        bottom = (
            int(base[0] * 0.9 + accent[0] * 0.1),
            int(base[1] * 0.9 + accent[1] * 0.1),
            int(base[2] * 0.9 + accent[2] * 0.1)
        )
    else:
        top = base
        bottom = (
            int(base[0] * 0.85 + accent[0] * 0.15),
            int(base[1] * 0.85 + accent[1] * 0.15),
            int(base[2] * 0.85 + accent[2] * 0.15)
        )
        
    for y in range(H):
        t = y / H
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    return img


# ── C2: Pillow rendering helpers ──────────────

def _overlay_theme(img: Image.Image, mode: str, pal: dict, alpha: int = 210) -> Image.Image:
    """Apply a horizontal gradient overlay: opaque on the left (for text readability), fading to transparent on the right (so the model is crisp)."""
    overlay = Image.new("RGBA", img.size)
    draw = ImageDraw.Draw(overlay)
    
    # Get base background color
    base = pal["bg"]
    
    for x in range(W):
        # Left 45% is opaque overlay. From 45% to 75% it fades out. Right 25% is completely transparent.
        if x < int(W * 0.45):
            curr_alpha = alpha
        elif x > int(W * 0.75):
            curr_alpha = 0
        else:
            t = (x - int(W * 0.45)) / (int(W * 0.75) - int(W * 0.45))
            curr_alpha = int(alpha * (1 - t))
            
        draw.line([(x, 0), (x, H)], fill=(base[0], base[1], base[2], curr_alpha))
        
    base_img = img.convert("RGBA")
    merged = Image.alpha_composite(base_img, overlay)
    return merged.convert("RGB")


def _draw_top_accent(draw: ImageDraw.Draw, pal: dict, height: int = 8):
    draw.rectangle([0, 0, W, height], fill=pal["accent"])


def _draw_profile_badge(draw: ImageDraw.Draw, pal: dict, mode: str):
    """Pin a professional profile photo placeholder and handle to the top-right."""
    avatar_x = W - 320
    avatar_y = 90
    avatar_r = 34
    
    # Circle base
    draw.ellipse([avatar_x - avatar_r, avatar_y - avatar_r, avatar_x + avatar_r, avatar_y + avatar_r], fill=pal["accent"])
    
    # Draw initials inside avatar circle
    f_init = _font("bold", 28)
    initials = BRAND_NAME[:2].upper()
    ib = draw.textbbox((0, 0), initials, font=f_init)
    iw, ih = ib[2] - ib[0], ib[3] - ib[1]
    draw.text((avatar_x - iw // 2, avatar_y - ih // 2 - 3), initials, font=f_init, fill=(255, 255, 255))
    
    # Name and Handle
    f_name = _font("bold", 24)
    f_handle = _font("regular", 20)
    text_color = pal["text"]
    muted_color = pal["muted"]
    
    draw.text((avatar_x + avatar_r + 15, avatar_y - 20), BRAND_NAME, font=f_name, fill=text_color)
    draw.text((avatar_x + avatar_r + 15, avatar_y + 8), f"@{BRAND_NAME.lower()}", font=f_handle, fill=muted_color)


def _draw_step_count_badge(draw: ImageDraw.Draw, pal: dict, steps_count: int = 3):
    """Draw a large italicised step count badge at the bottom-left of the first slide."""
    x = 80
    y = H - 390
    
    f_num = _font("bold", 120)
    f_lbl1 = _font("bold", 28)
    f_lbl2 = _font("regular", 40)
    
    num_str = str(steps_count)
    draw.text((x, y), num_str, font=f_num, fill=pal["accent2"])
    
    num_w = draw.textbbox((0, 0), num_str, font=f_num)[2] - draw.textbbox((0, 0), num_str, font=f_num)[0]
    
    draw.text((x + num_w + 15, y + 20), "FOLLOW THESE", font=f_lbl1, fill=pal["muted"])
    draw.text((x + num_w + 15, y + 55), "Steps!", font=f_lbl2, fill=pal["text"])


def _draw_bottom_badges(draw: ImageDraw.Draw, pal: dict, mode: str):
    """Draw 'SWIPE LEFT' pill tag pinned above the footer."""
    y = H - 200
    
    # Left tag: "SWIPE LEFT" in highlight background with dark text
    f_badge = _font("bold", 28)
    swipe_text = "SWIPE LEFT"
    tw = draw.textbbox((0, 0), swipe_text, font=f_badge)[2] - draw.textbbox((0, 0), swipe_text, font=f_badge)[0]
    
    bg_color = pal["highlight"]
    _draw_rounded_rect(draw, 80, y, tw + 36, 56, fill=bg_color, radius=28)
    draw.text((80 + 18, y + 13), swipe_text, font=f_badge, fill=(0, 0, 0))


def _draw_footer(draw: ImageDraw.Draw, slide_num: int, pal: dict):
    """Brand name left, slide counter right."""
    bar_y = H - 80
    draw.rectangle([0, bar_y, W, H], fill=pal["bg_card"])
    draw.rectangle([0, bar_y, W, bar_y + 1], fill=pal["accent"])
    f = _font("regular", 30)
    draw.text((52, bar_y + 24), BRAND_NAME, font=f, fill=pal["muted"])
    counter = f"{slide_num} / 3"
    bw      = draw.textbbox((0, 0), counter, font=f)[2]
    draw.text((W - bw - 52, bar_y + 24), counter, font=f, fill=pal["muted"])


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


# ── C3: Slide 1 — Hook ────────────────────────────────────────

def render_slide_1(bg: Image.Image, slide: dict, pal: dict, mode: str) -> Image.Image:
    # Use horizontal gradient overlay so right side model stays clean
    img  = _overlay_theme(bg, mode, pal, alpha=190)
    draw = ImageDraw.Draw(img)

    _draw_top_accent(draw, pal)
    _draw_profile_badge(draw, pal, mode)

    # Topic label
    f_label = _font("regular", 32)
    draw.text((80, 160), slide.get("topic_label", "").upper(),
              font=f_label, fill=pal["accent2"])
    draw.rectangle([80, 206, 200, 212], fill=pal["accent"])

    # BREAKING badge
    badge = slide.get("badge", "BREAKING").upper()
    f_badge = _font("bold", 28)
    bw    = draw.textbbox((0, 0), badge, font=f_badge)[2] - draw.textbbox((0, 0), badge, font=f_badge)[0]
    _draw_rounded_rect(draw, 80, 240, bw + 32, 50, fill=pal["danger"], radius=8)
    draw.text((96, 250), badge, font=f_badge, fill=(255, 255, 255))

    # Main headline — very large, left-aligned, maximum width limited to keep right side clear
    headline  = slide.get("headline", "")
    f_head    = _font("bold", 82)
    max_w     = int(W * 0.54) # Limit text width to 580px
    
    # Headline wrapping
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
    
    lh      = draw.textbbox((0, 0), "Ag", font=f_head)[3] + 14
    y_start = 330

    stroke_w = 2
    stroke_fill = (255, 255, 255) if mode == "light" else (0, 0, 0)

    for line in lines:
        draw.text(
            (80, y_start),
            line, font=f_head, fill=pal["text"],
            stroke_width=stroke_w, stroke_fill=stroke_fill
        )
        y_start += lh

    # Subtext
    subtext = slide.get("subtext", "")
    f_sub   = _font("regular", 40)
    _draw_text_wrapped(
        draw, subtext, (80, y_start + 30),
        font=f_sub, fill=pal["muted"],
        max_width=max_w, stroke_width=2, stroke_fill=stroke_fill
    )

    _draw_step_count_badge(draw, pal, steps_count=3)
    _draw_bottom_badges(draw, pal, mode)
    _draw_footer(draw, 1, pal)
    return img


# ── C4: Slide 2 — Details ─────────────────────────────────────

def render_slide_2(bg: Image.Image, slide: dict, pal: dict, mode: str) -> Image.Image:
    img  = _overlay_theme(bg, mode, pal, alpha=195)
    draw = ImageDraw.Draw(img)

    _draw_top_accent(draw, pal)
    _draw_profile_badge(draw, pal, mode)

    # Section badge
    f_section = _font("bold", 30)
    sec_title = slide.get("section_title", "WHAT'S HAPPENING")
    bw        = draw.textbbox((0, 0), sec_title, font=f_section)[2] - draw.textbbox((0, 0), sec_title, font=f_section)[0]
    _draw_rounded_rect(draw, 80, 160, bw + 36, 52, fill=pal["accent"], radius=10)
    draw.text((98, 170), sec_title, font=f_section, fill=(255, 255, 255))

    stroke_fill = (255, 255, 255) if mode == "light" else (0, 0, 0)

    # Headline area (topic context)
    headline = slide.get("headline", slide.get("section_title", "Key Facts"))
    f_head   = _font("bold", 64)
    max_w     = int(W * 0.54) # Limit text width to 580px
    _draw_text_wrapped(
        draw, headline, (80, 240),
        font=f_head, fill=pal["text"],
        max_width=max_w, stroke_width=2, stroke_fill=stroke_fill
    )

    # Data points as styled cards on the left half
    points  = slide.get("points", [])
    card_y  = 440
    card_h  = 180
    gap     = 24
    f_point = _font("regular", 36)
    f_num   = _font("bold", 44)

    for idx, point in enumerate(points[:3]):
        # Card background
        _draw_rounded_rect(draw, 80, card_y, max_w, card_h,
                           fill=pal["bg_card"], radius=20)
        # Left accent strip
        draw.rectangle([80, card_y, 90, card_y + card_h], fill=pal["accent"])
        # Number badge
        num_txt = f"0{idx + 1}"
        draw.text((110, card_y + 15), num_txt, font=f_num, fill=pal["accent"])
        # Point text
        _draw_text_wrapped(
            draw, point, (110, card_y + 70),
            font=f_point, fill=pal["text"],
            max_width=max_w - 50, line_gap=8
        )
        card_y += card_h + gap

    _draw_bottom_badges(draw, pal, mode)
    _draw_footer(draw, 2, pal)
    return img


# ── C5: Slide 3 — Actions ─────────────────────────────────────

def render_slide_3(bg: Image.Image, slide: dict, pal: dict, mode: str) -> Image.Image:
    img  = _overlay_theme(bg, mode, pal, alpha=200)
    draw = ImageDraw.Draw(img)

    _draw_top_accent(draw, pal)
    _draw_profile_badge(draw, pal, mode)

    # Section badge
    f_section = _font("bold", 30)
    sec_title = slide.get("section_title", "WHAT YOU SHOULD DO")
    bw        = draw.textbbox((0, 0), sec_title, font=f_section)[2] - draw.textbbox((0, 0), sec_title, font=f_section)[0]
    _draw_rounded_rect(draw, 80, 160, bw + 36, 52, fill=pal["success"], radius=10)
    sec_txt_color = (0, 0, 0) if mode == "dark" else (255, 255, 255)
    draw.text((98, 170), sec_title, font=f_section, fill=sec_txt_color)

    # Steps on the left half
    steps   = slide.get("steps", [])
    step_y  = 240
    f_step  = _font("regular", 36)
    f_num   = _font("bold", 44)
    step_h  = 200
    gap     = 24
    max_w   = int(W * 0.54) # Limit text width to 580px

    for idx, step in enumerate(steps[:3]):
        # Step card
        _draw_rounded_rect(draw, 80, step_y, max_w, step_h,
                           fill=pal["bg_card"], radius=20)
        # Number in accent circle
        num = str(idx + 1)
        nf  = _font("bold", 40)
        draw.ellipse([95, step_y + 15, 150, step_y + 70], fill=pal["accent"])
        nw  = draw.textbbox((0, 0), num, font=nf)[2] - draw.textbbox((0, 0), num, font=nf)[0]
        draw.text((95 + (55 - nw) // 2, step_y + 20), num, font=nf, fill=(255, 255, 255))
        # Step text
        _draw_text_wrapped(
            draw, step, (165, step_y + 22),
            font=f_step, fill=pal["text"],
            max_width=max_w - 100, line_gap=8
        )
        step_y += step_h + gap

    # CTA block — pinned above bottom badge area
    cta_text = slide.get("cta", "Save this for your team")
    cta_y    = H - 290
    cta_h    = 80
    _draw_rounded_rect(draw, 80, cta_y, max_w, cta_h,
                       fill=pal["accent"], radius=20)
    f_cta = _font("bold", 34)
    cw    = draw.textbbox((0, 0), cta_text, font=f_cta)[2] - draw.textbbox((0, 0), cta_text, font=f_cta)[0]
    draw.text((80 + (max_w - cw) // 2, cta_y + 23), cta_text, font=f_cta, fill=(255, 255, 255))

    _draw_bottom_badges(draw, pal, mode)
    _draw_footer(draw, 3, pal)
    return img


# ── C6: Orchestrate all 3 slides ─────────────────────────────

def generate_all_slides(content: dict) -> list[Path]:
    """
    For each of 3 slides:
      1. Call Gemini 2.5 Flash Image to generate background (blocks until complete)
      2. Overlay professional text with Pillow using the generated theme
      3. Save JPEG to output/
    Returns list of saved file paths.
    """
    log.info("\n━━━━ MODULE C: Image Generation + Rendering ━━━━")

    topic   = content.get("topic", "")
    slides  = content["slides"]
    paths   = []

    # Dynamic theme parsing
    theme = content.get("theme", {})
    mode = theme.get("mode", "dark")
    
    # Fallback palette if the API fails to provide one or provides invalid HEX colors
    try:
        pal = {
            "bg"        : hex_to_rgb(theme.get("bg", "#080E1A")),
            "bg_card"   : hex_to_rgb(theme.get("bg_card", "#101830")),
            "accent"    : hex_to_rgb(theme.get("accent", "#2563EB")),
            "accent2"   : hex_to_rgb(theme.get("accent2", "#63B3ED")),
            "text"      : hex_to_rgb(theme.get("text", "#F9FAFB")),
            "muted"     : hex_to_rgb(theme.get("muted", "#9CA3AF")),
            "highlight" : hex_to_rgb(theme.get("highlight", "#FBBF24")),
            "danger"    : hex_to_rgb(theme.get("danger", "#EF4444")),
            "success"   : hex_to_rgb(theme.get("success", "#22C55E")),
        }
    except Exception as e:
        log.warning(f"Failed to parse theme color HEX codes: {e}. Using default dark palette.")
        pal = {
            "bg"        : (8,  14,  26),
            "bg_card"   : (16, 24,  48),
            "accent"    : (37, 99,  235),
            "accent2"   : (99, 179, 237),
            "text"      : (249, 250, 251),
            "muted"     : (156, 163, 175),
            "highlight" : (251, 191, 36),
            "danger"    : (239, 68,  68),
            "success"   : (34,  197, 94),
        }
        mode = "dark"

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
        # Pass the dynamic palette to fallback background builder if Gemini Image fails
        try:
            bg = generate_background(slide["image_bg_prompt"], slide_id, pal, mode)
        except Exception:
            log.warning("Image generation crashed. Using gradient fallback.")
            bg = _make_gradient_bg(pal, mode, slide_id)

        # Render text overlay
        render_fn = render_fns.get(slide_type)
        if render_fn is None:
            log.error(f"  Unknown slide type: {slide_type}")
            continue

        rendered = render_fn(bg, slide, pal, mode)
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

    dest = f"{RCLONE_REMOTE}:LinkedCarousel"
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