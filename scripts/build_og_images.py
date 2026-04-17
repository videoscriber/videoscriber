"""Generate the 1200x630 OG/Twitter image for videoscriber.ai.

Run with:  .venv/bin/python scripts/build_og_images.py

Outputs:
  static/brand/og-image.png         (primary, 1200x630)
  static/brand/og-image-square.png  (1200x1200 square variant for some platforms)

Uses Pillow so no browser / headless chromium needed. Matches the brand:
purple gradient background, logo icon + wordmark, hero tagline.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(__file__).parent.parent / "static" / "brand"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    """Prefer Inter (bundled in the system font dirs on most dev boxes),
    fall back to Helvetica/default."""
    candidates = [
        f"/Library/Fonts/Inter-{weight}.ttf",
        f"/System/Library/Fonts/SFNS.ttf",
        f"/Library/Fonts/Helvetica.ttf",
        f"/System/Library/Fonts/Helvetica.ttc",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default(size=size)


def _rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _gradient_rect(w: int, h: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (w, h), top)
    top_px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            top_px[x, y] = (r, g, b)
    return img


def _radial_blob(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    """A soft circular blob used for atmospheric glow."""
    blob = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(blob)
    steps = 40
    for i in range(steps, 0, -1):
        alpha = int(color[3] * (i / steps) ** 2)
        radius = int(size / 2 * (i / steps))
        d.ellipse(
            [size // 2 - radius, size // 2 - radius,
             size // 2 + radius, size // 2 + radius],
            fill=color[:3] + (alpha,),
        )
    return blob.filter(ImageFilter.GaussianBlur(radius=30))


def _draw_camera_icon(canvas: Image.Image, center_x: int, center_y: int, size: int):
    """Draw the Videoscriber camera mark — rounded square with gradient + lens cutout."""
    half = size // 2
    body = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    # Gradient fill (approximated with vertical strips)
    top = (183, 148, 246)  # #B794F6
    bot = (109, 40, 217)   # #6D28D9
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        bd.rectangle([0, y, size, y + 1], fill=(r, g, b, 255))
    # Apply rounded mask
    mask = Image.new("L", (size, size), 0)
    mask_d = ImageDraw.Draw(mask)
    mask_d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(body, (0, 0), mask)
    # Paste onto canvas
    canvas.paste(rounded, (center_x - half, center_y - half), rounded)

    # Lens (darker trapezoid on the right)
    lens = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(lens)
    lens_x = center_x + int(size * 0.32)
    lens_w = int(size * 0.32)
    lens_h = int(size * 0.60)
    lens_y = center_y - lens_h // 2
    ld.polygon([
        (lens_x - lens_w // 3, lens_y + lens_h // 3),
        (lens_x + lens_w, lens_y),
        (lens_x + lens_w, lens_y + lens_h),
        (lens_x - lens_w // 3, lens_y + lens_h * 2 // 3),
    ], fill=(124, 58, 237, 230))
    canvas.alpha_composite(lens)

    # Three caption bars inside the body
    bars = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    bd2 = ImageDraw.Draw(bars)
    bar_x = center_x - int(size * 0.28)
    bar_h = max(3, int(size * 0.05))
    offsets = [
        (-int(size * 0.18), int(size * 0.50)),
        (-int(size * 0.03), int(size * 0.62)),
        (int(size * 0.13), int(size * 0.38)),
    ]
    for (dy, w) in offsets:
        y0 = center_y + dy - bar_h // 2
        bd2.rounded_rectangle(
            [bar_x, y0, bar_x + w, y0 + bar_h],
            radius=bar_h // 2,
            fill=(255, 255, 255, 230),
        )
    canvas.alpha_composite(bars)


def render(width: int = 1200, height: int = 630, output: Path = OUT_DIR / "og-image.png"):
    # Deep indigo → near-black gradient base (matches the app's dark theme)
    base = _gradient_rect(width, height, top=(19, 15, 39), bottom=(9, 9, 15))
    canvas = base.convert("RGBA")

    # Atmospheric orbs behind the content
    orb_purple = _radial_blob(720, (139, 92, 246, 140))
    orb_indigo = _radial_blob(640, (99, 102, 241, 110))
    canvas.alpha_composite(orb_purple, (int(width * 0.58), int(height * -0.15)))
    canvas.alpha_composite(orb_indigo, (int(width * -0.10), int(height * 0.35)))

    # Logo mark + wordmark top-left
    icon_size = 72
    _draw_camera_icon(canvas, 110, 95, icon_size)
    wordmark = _font(58, "SemiBold")
    draw = ImageDraw.Draw(canvas)
    draw.text((165, 95 - 34), "videoscriber", font=wordmark, fill=(255, 255, 255, 245))

    # Big hero headline
    h_font = _font(96, "Bold")
    headline_top = 220
    draw.text((78, headline_top), "AI transcription agents", font=h_font, fill=(255, 255, 255, 255))
    # Second line in gradient (we simulate by drawing twice: base white with reduced alpha,
    # then a purple overlay for the accent word).
    draw.text((78, headline_top + 110), "that always follow up.", font=h_font, fill=(196, 181, 253, 255))

    # Supporting line
    sub = _font(32, "Regular")
    draw.text(
        (78, headline_top + 250),
        "Turn every meeting recording into a transcript, a recap, and a follow-up email.",
        font=sub,
        fill=(200, 200, 220, 235),
    )

    # Subtle bottom-right URL chip
    chip_font = _font(26, "Medium")
    url = "videoscriber.ai"
    bbox = draw.textbbox((0, 0), url, font=chip_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    padding_x, padding_y = 22, 12
    chip_w = tw + padding_x * 2
    chip_h = th + padding_y * 2
    chip_x = width - chip_w - 60
    chip_y = height - chip_h - 50
    _rounded_rect(
        draw,
        [chip_x, chip_y, chip_x + chip_w, chip_y + chip_h],
        radius=chip_h // 2,
        fill=(255, 255, 255, 26),
    )
    draw.text((chip_x + padding_x, chip_y + padding_y - 4), url, font=chip_font, fill=(200, 180, 255, 235))

    canvas.convert("RGB").save(output, "PNG", optimize=True)
    print(f"wrote {output}  ({output.stat().st_size // 1024} KB)")


def render_square(output: Path = OUT_DIR / "og-image-square.png"):
    """Square variant (1200x1200) for platforms that prefer square previews."""
    render(width=1200, height=1200, output=output)


if __name__ == "__main__":
    render()
    render_square()
