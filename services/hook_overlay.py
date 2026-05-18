"""Viral hook text overlay.

Renders styled hook text via PIL → overlays onto video via single ffmpeg pass.
Styles support: custom font, fg/bg colors, box outline, text stroke, drop
shadow, multi-layer glow, rotation (sticker tilt), uppercase transform,
letter-spacing.
"""
import os
import subprocess
from pathlib import Path
from typing import Tuple, List, Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from services.media_tools import ffmpeg_path, ffprobe_path

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONT_SERIF = str(FONTS_DIR / "NotoSerif-Bold.ttf")
FONT_INTER = str(FONTS_DIR / "Inter-Black.ttf")
FONT_ANTON = str(FONTS_DIR / "Anton-Regular.ttf")
FONT_DEVANAGARI = str(FONTS_DIR / "NotoSansDevanagari.ttf")


def _has_devanagari(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text)


def _font_for_text(preferred_path: str, text: str) -> str:
    """PIL has no per-glyph fallback — single font must cover all glyphs or
    tofu boxes render. If text contains Devanagari, swap whichever bundled
    Latin-only font the style picked → NotoSansDevanagari (covers both)."""
    if _has_devanagari(text):
        # None of the bundled "look" fonts (Inter / Anton / NotoSerif-Bold Latin
        # subset) cover Devanagari. Always swap on Devanagari presence.
        return FONT_DEVANAGARI
    return preferred_path


# Hook style presets. Each preset can specify:
#   font: ttf path
#   bg: RGBA fill for box (None = no box)
#   fg: RGBA text color
#   box_outline / box_outline_w: solid stroke around box
#   text_stroke / text_stroke_w: stroke around glyphs
#   shadow_offset: (dx, dy) opaque drop shadow offset (0,0 disables)
#   shadow_alpha / shadow_blur: drop shadow look
#   glow: list of (color, blur_radius, expand) layers — multi-color neon
#   corner_radius
#   padding_x / padding_y
#   tilt_deg: post-render rotation (sticker vibe)
#   uppercase: force uppercase text
#   letter_spacing_px: pixel tracking between glyphs
HOOK_STYLES = {
    "serif_card": {
        "label": "Serif card",
        "font": FONT_SERIF,
        "bg": (255, 255, 255, 240),
        "fg": (15, 15, 15, 255),
        "shadow_offset": (6, 10),
        "shadow_alpha": 140,
        "shadow_blur": 14,
        "corner_radius": 22,
        "padding_x": 32,
        "padding_y": 26,
        "tilt_deg": -1.5,
    },
    "mrbeast_yellow": {
        "label": "MrBeast yellow",
        "font": FONT_INTER,
        "bg": None,
        "fg": (255, 220, 0, 255),
        "text_stroke": (0, 0, 0, 255),
        "text_stroke_w": 7,
        "shadow_offset": (5, 5),
        "shadow_alpha": 220,
        "shadow_blur": 4,
        "padding_x": 24,
        "padding_y": 18,
        "uppercase": True,
    },
    "sticker_pop": {
        "label": "Sticker pop",
        "font": FONT_INTER,
        "bg": (255, 222, 0, 255),
        "fg": (0, 0, 0, 255),
        "box_outline": (0, 0, 0, 255),
        "box_outline_w": 5,
        "shadow_offset": (12, 14),
        "shadow_alpha": 230,
        "shadow_blur": 0,
        "corner_radius": 18,
        "padding_x": 30,
        "padding_y": 22,
        "tilt_deg": -4.0,
        "uppercase": True,
    },
    "neon_glow": {
        "label": "Neon glow",
        "font": FONT_INTER,
        "bg": (10, 10, 20, 230),
        "fg": (255, 255, 255, 255),
        "glow": [
            ((255, 0, 200, 220), 12, 4),  # magenta wide
            ((0, 220, 255, 220), 6, 2),   # cyan tight
        ],
        "corner_radius": 14,
        "padding_x": 32,
        "padding_y": 24,
        "uppercase": True,
        "letter_spacing_px": 1,
    },
    "hot_pink": {
        "label": "Hot pink",
        "font": FONT_INTER,
        "bg": (255, 30, 130, 250),
        "fg": (255, 255, 255, 255),
        "shadow_offset": (12, 14),
        "shadow_alpha": 230,
        "shadow_blur": 0,
        "corner_radius": 22,
        "padding_x": 30,
        "padding_y": 22,
        "uppercase": True,
        "letter_spacing_px": 1,
    },
    "breaking_news": {
        "label": "Breaking news",
        "font": FONT_INTER,
        "bg": (210, 25, 25, 250),
        "fg": (255, 255, 255, 255),
        "box_outline": (255, 255, 255, 255),
        "box_outline_w": 3,
        "shadow_offset": (10, 12),
        "shadow_alpha": 230,
        "shadow_blur": 0,
        "corner_radius": 4,
        "padding_x": 34,
        "padding_y": 20,
        "uppercase": True,
        "letter_spacing_px": 2,
    },
    "headline_anton": {
        "label": "Headline",
        "font": FONT_ANTON,
        "bg": None,
        "fg": (255, 255, 255, 255),
        "text_stroke": (0, 0, 0, 255),
        "text_stroke_w": 8,
        "shadow_offset": (4, 4),
        "shadow_alpha": 220,
        "shadow_blur": 2,
        "padding_x": 16,
        "padding_y": 10,
        "uppercase": True,
        "letter_spacing_px": 2,
    },
    "glass_dark": {
        "label": "Glass",
        "font": FONT_INTER,
        "bg": (30, 30, 35, 180),
        "fg": (255, 255, 255, 255),
        "box_outline": (255, 255, 255, 60),
        "box_outline_w": 2,
        "shadow_offset": (0, 8),
        "shadow_alpha": 160,
        "shadow_blur": 14,
        "corner_radius": 24,
        "padding_x": 28,
        "padding_y": 22,
    },
    "sunset_gradient": {
        "label": "Sunset",
        "font": FONT_INTER,
        "bg_gradient": ((255, 110, 60, 250), (255, 60, 150, 250)),  # orange → pink
        "fg": (255, 255, 255, 255),
        "shadow_offset": (10, 12),
        "shadow_alpha": 230,
        "shadow_blur": 0,
        "corner_radius": 20,
        "padding_x": 30,
        "padding_y": 22,
        "uppercase": True,
        "letter_spacing_px": 1,
    },
    "ocean_gradient": {
        "label": "Ocean",
        "font": FONT_INTER,
        "bg_gradient": ((0, 200, 255, 250), (120, 80, 255, 250)),  # cyan → purple
        "fg": (255, 255, 255, 255),
        "shadow_offset": (10, 12),
        "shadow_alpha": 230,
        "shadow_blur": 0,
        "corner_radius": 20,
        "padding_x": 30,
        "padding_y": 22,
        "uppercase": True,
        "letter_spacing_px": 1,
    },
    "gold_luxe": {
        "label": "Gold luxe",
        "font": FONT_SERIF,
        "bg": (15, 15, 15, 250),
        "fg": (212, 175, 55, 255),
        "box_outline": (212, 175, 55, 255),
        "box_outline_w": 2,
        "shadow_offset": (8, 10),
        "shadow_alpha": 230,
        "shadow_blur": 0,
        "corner_radius": 0,
        "padding_x": 32,
        "padding_y": 22,
        "letter_spacing_px": 2,
        "uppercase": True,
    },
    "pastel_card": {
        "label": "Pastel",
        "font": FONT_INTER,
        "bg": (255, 220, 235, 245),
        "fg": (110, 30, 80, 255),
        "shadow_offset": (4, 6),
        "shadow_alpha": 140,
        "shadow_blur": 10,
        "corner_radius": 22,
        "padding_x": 28,
        "padding_y": 22,
        "tilt_deg": 1.5,
    },
    "matrix_green": {
        "label": "Matrix",
        "font": FONT_INTER,
        "bg": (0, 0, 0, 250),
        "fg": (0, 255, 90, 255),
        "glow": [((0, 255, 90, 200), 8, 2)],
        "corner_radius": 6,
        "padding_x": 28,
        "padding_y": 20,
        "uppercase": True,
        "letter_spacing_px": 1,
    },
    "claymorphism": {
        "label": "Clay",
        "font": FONT_INTER,
        "bg": (167, 198, 255, 250),  # soft blue pastel
        "fg": (28, 30, 60, 255),
        "shadow_offset": (6, 8),
        "shadow_alpha": 200,
        "shadow_blur": 0,
        "corner_radius": 26,
        "padding_x": 32,
        "padding_y": 24,
    },
    "brutalism_red": {
        "label": "Brutalist",
        "font": FONT_ANTON,
        "bg": (255, 0, 0, 255),
        "fg": (255, 255, 255, 255),
        "box_outline": (0, 0, 0, 255),
        "box_outline_w": 6,
        "shadow_offset": (14, 14),
        "shadow_alpha": 255,
        "shadow_blur": 0,
        "corner_radius": 0,
        "padding_x": 32,
        "padding_y": 18,
        "uppercase": True,
        "letter_spacing_px": 3,
    },
    "glitch_rgb": {
        "label": "Glitch",
        "font": FONT_INTER,
        "bg": (0, 0, 0, 245),
        "fg": (255, 255, 255, 255),
        # Two-layer chromatic aberration: red shifted left, cyan shifted right
        "glow": [
            ((255, 0, 60, 230), 0, 4),
            ((0, 220, 255, 230), 0, 4),
        ],
        "corner_radius": 4,
        "padding_x": 30,
        "padding_y": 22,
        "uppercase": True,
        "letter_spacing_px": 2,
    },
    "memphis_pop": {
        "label": "Memphis",
        "font": FONT_INTER,
        "bg": (255, 113, 206, 255),  # hot pink
        "fg": (28, 30, 60, 255),
        "box_outline": (134, 204, 202, 255),  # teal accent border
        "box_outline_w": 4,
        "shadow_offset": (8, 10),
        "shadow_alpha": 230,
        "shadow_blur": 0,
        "corner_radius": 16,
        "padding_x": 30,
        "padding_y": 22,
        "tilt_deg": -3.0,
        "uppercase": True,
        "letter_spacing_px": 1,
    },
}


def get_hook_style(style_id: str) -> dict:
    return HOOK_STYLES.get(style_id, HOOK_STYLES["serif_card"])


def _probe_dims(video_path: str) -> Tuple[int, int]:
    res = subprocess.check_output(
        [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
         video_path],
        timeout=30,
    ).decode().strip().split("\n")[0].split("x")
    return int(res[0]), int(res[1])


def _load_font(path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path or FONT_SERIF, size)
    except Exception:
        try:
            return ImageFont.truetype(FONT_SERIF, size)
        except Exception:
            return ImageFont.load_default()


def _line_text_width(draw: ImageDraw.ImageDraw, text: str,
                     font: ImageFont.FreeTypeFont, letter_spacing: int) -> int:
    if not text:
        return 0
    if letter_spacing <= 0:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0]
    total = 0
    for ch in text:
        bb = draw.textbbox((0, 0), ch, font=font)
        total += (bb[2] - bb[0]) + letter_spacing
    return max(0, total - letter_spacing)


def _draw_line_with_spacing(draw: ImageDraw.ImageDraw, xy: Tuple[int, int],
                            text: str, font: ImageFont.FreeTypeFont,
                            fill, stroke_width: int = 0, stroke_fill=None,
                            letter_spacing: int = 0) -> None:
    if letter_spacing <= 0:
        draw.text(xy, text, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        return
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        bb = draw.textbbox((0, 0), ch, font=font)
        x += (bb[2] - bb[0]) + letter_spacing


def create_hook_image(
    text: str,
    target_width: int,
    output_path: str,
    font_scale: float = 1.0,
    style: str = "serif_card",
) -> Tuple[str, int, int]:
    """Render hook PNG with chosen style. Returns (png_path, canvas_w, canvas_h)."""
    cfg = get_hook_style(style)

    if cfg.get("uppercase"):
        text = text.upper()

    padding_x = int(cfg.get("padding_x", 28))
    padding_y = int(cfg.get("padding_y", 22))
    line_spacing = 16
    corner_radius = int(cfg.get("corner_radius", 0))
    bg = cfg.get("bg")
    bg_gradient = cfg.get("bg_gradient")
    fg = cfg["fg"]
    # Treat gradient as "has box" for layout/shadow purposes.
    has_box = bg is not None or bg_gradient is not None
    text_stroke = cfg.get("text_stroke")
    text_stroke_w = int(cfg.get("text_stroke_w", 0))
    box_outline = cfg.get("box_outline")
    box_outline_w = int(cfg.get("box_outline_w", 0))
    shadow_offset = cfg.get("shadow_offset", (0, 0))
    shadow_alpha = int(cfg.get("shadow_alpha", 0))
    shadow_blur = int(cfg.get("shadow_blur", 0))
    glow_layers: List = cfg.get("glow", [])
    tilt_deg = float(cfg.get("tilt_deg", 0.0))
    letter_spacing = int(cfg.get("letter_spacing_px", 0))

    # 2x supersample: render everything at double size, downscale at end with
    # LANCZOS. PIL's default rasterizer is grainy at 1x; supersample → smoother
    # edges, cleaner strokes, no jaggies on tilted text. ~2x memory, ~4x ops
    # but render is a one-shot per export (~50ms) so cost negligible.
    SUPER = 2
    # Font sized as % of video width. 4.6% on 1080-wide ≈ 50px → matches the
    # compact sticker proportions seen in the live preview. Higher values
    # forced text to wrap to 2 lines for short hooks.
    base_font_size = int(target_width * 0.046)
    font_size = max(14, int(base_font_size * font_scale))
    font_size_ss = font_size * SUPER
    font_path = _font_for_text(cfg.get("font") or FONT_SERIF, text)
    font = _load_font(font_path, font_size_ss)
    # Scale ALL pixel measurements by SUPER for the supersampled canvas.
    padding_x *= SUPER
    padding_y *= SUPER
    line_spacing *= SUPER
    corner_radius *= SUPER
    text_stroke_w *= SUPER
    box_outline_w *= SUPER
    shadow_offset = (shadow_offset[0] * SUPER, shadow_offset[1] * SUPER)
    shadow_blur *= SUPER
    letter_spacing *= SUPER
    target_width_ss = target_width * SUPER

    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    max_text_width = target_width_ss - (2 * padding_x)

    # Word wrap with letter-spacing awareness
    lines: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split()
        current: List[str] = []
        for word in words:
            test = " ".join(current + [word])
            if _line_text_width(draw, test, font, letter_spacing) <= max_text_width:
                current.append(word)
            else:
                if current:
                    lines.append(" ".join(current))
                    current = [word]
                else:
                    lines.append(word)
                    current = []
        if current:
            lines.append(" ".join(current))

    max_line_w = 0
    line_heights: List[int] = []
    for line in lines:
        if not line:
            line_heights.append(font_size_ss)
            continue
        w = _line_text_width(draw, line, font, letter_spacing)
        max_line_w = max(max_line_w, w)
        bb = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bb[3] - bb[1])

    # Account for stroke / glow padding so glyphs aren't clipped
    glow_pad = max((int(l[1] + l[2]) for l in glow_layers), default=0)
    inner_pad_text = max(text_stroke_w, glow_pad)

    # Box hugs content (no min-width). Short hooks → small compact sticker,
    # long hooks → wide banner. Matches preview's content-fit behavior.
    box_w = max_line_w + 2 * padding_x + 2 * inner_pad_text
    if not line_heights:
        total_text_h = font_size_ss
    else:
        total_text_h = sum(line_heights) + (len(line_heights) - 1) * line_spacing
    box_h = total_text_h + 2 * padding_y + 2 * inner_pad_text

    margin = max(40, shadow_blur + abs(shadow_offset[1]) + 10)
    canvas_w = box_w + 2 * margin
    canvas_h = box_h + 2 * margin
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # Drop shadow (only when there is a solid box; otherwise shadow follows text via stroke)
    if has_box and shadow_alpha > 0 and (shadow_offset[0] or shadow_offset[1] or shadow_blur):
        shadow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_layer)
        sb = [
            (margin + shadow_offset[0], margin + shadow_offset[1]),
            (margin + box_w + shadow_offset[0], margin + box_h + shadow_offset[1]),
        ]
        sd.rounded_rectangle(sb, radius=corner_radius, fill=(0, 0, 0, shadow_alpha))
        if shadow_blur > 0:
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(shadow_blur))
        img = Image.alpha_composite(img, shadow_layer)

    # Box fill (solid or gradient) + outline
    if has_box:
        box_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        if bg_gradient is not None:
            # Build a per-pixel horizontal linear gradient over the box rect,
            # then mask it with a rounded-rect so corners stay clean.
            c0, c1 = bg_gradient
            grad = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
            gp = grad.load()
            for x in range(box_w):
                t = x / max(1, box_w - 1)
                r = int(c0[0] + (c1[0] - c0[0]) * t)
                g = int(c0[1] + (c1[1] - c0[1]) * t)
                b = int(c0[2] + (c1[2] - c0[2]) * t)
                a = int(c0[3] + (c1[3] - c0[3]) * t)
                for y in range(box_h):
                    gp[x, y] = (r, g, b, a)
            mask = Image.new("L", (box_w, box_h), 0)
            mdraw = ImageDraw.Draw(mask)
            mdraw.rounded_rectangle(
                [(0, 0), (box_w - 1, box_h - 1)], radius=corner_radius, fill=255
            )
            box_layer.paste(grad, (margin, margin), mask)
            if box_outline and box_outline_w > 0:
                od = ImageDraw.Draw(box_layer)
                od.rounded_rectangle(
                    [(margin, margin), (margin + box_w, margin + box_h)],
                    radius=corner_radius, outline=box_outline, width=box_outline_w,
                )
        else:
            bd = ImageDraw.Draw(box_layer)
            rect = [(margin, margin), (margin + box_w, margin + box_h)]
            bd.rounded_rectangle(
                rect, radius=corner_radius, fill=bg,
                outline=box_outline if box_outline else None,
                width=box_outline_w if box_outline else 0,
            )
        img = Image.alpha_composite(img, box_layer)

    # Glow layers (render text onto blurred copies, composite under main text)
    text_origin_y = margin + padding_y + inner_pad_text - 2

    def _render_text_layer(color, stroke_w=0, stroke_color=None) -> Image.Image:
        layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        y = text_origin_y
        for i, line in enumerate(lines):
            if not line:
                y += font_size_ss + line_spacing
                continue
            line_w = _line_text_width(ld, line, font, letter_spacing)
            x = margin + (box_w - line_w) // 2
            _draw_line_with_spacing(
                ld, (x, y), line, font, fill=color,
                stroke_width=stroke_w,
                stroke_fill=stroke_color,
                letter_spacing=letter_spacing,
            )
            y += (line_heights[i] if i < len(line_heights) else font_size_ss) + line_spacing
        return layer

    for color, blur_r, expand in glow_layers:
        g_layer = _render_text_layer(color, stroke_w=int(expand), stroke_color=color)
        if blur_r > 0:
            g_layer = g_layer.filter(ImageFilter.GaussianBlur(int(blur_r)))
        img = Image.alpha_composite(img, g_layer)

    # Drop shadow for stroked text (no box case)
    if not has_box and shadow_alpha > 0 and (shadow_offset[0] or shadow_offset[1]):
        sh_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh_layer)
        y = text_origin_y + shadow_offset[1]
        for i, line in enumerate(lines):
            if not line:
                y += font_size_ss + line_spacing
                continue
            line_w = _line_text_width(sd, line, font, letter_spacing)
            x = margin + (box_w - line_w) // 2 + shadow_offset[0]
            _draw_line_with_spacing(
                sd, (x, y), line, font, fill=(0, 0, 0, shadow_alpha),
                stroke_width=text_stroke_w,
                stroke_fill=(0, 0, 0, shadow_alpha),
                letter_spacing=letter_spacing,
            )
            y += (line_heights[i] if i < len(line_heights) else font_size_ss) + line_spacing
        if shadow_blur > 0:
            sh_layer = sh_layer.filter(ImageFilter.GaussianBlur(shadow_blur))
        img = Image.alpha_composite(img, sh_layer)

    # Main text
    main_layer = _render_text_layer(
        fg, stroke_w=text_stroke_w, stroke_color=text_stroke if text_stroke else None
    )
    img = Image.alpha_composite(img, main_layer)

    # Tilt — PIL rotate is CCW for positive angle, CSS rotate is CW. Negate
    # so backend output matches frontend live-preview direction (sticker tilts
    # left/right the same way user picked in modal).
    if tilt_deg:
        img = img.rotate(-tilt_deg, resample=Image.BICUBIC, expand=True)

    # Downscale supersampled canvas → final size with LANCZOS for smooth AA.
    if SUPER > 1:
        new_w = max(1, img.size[0] // SUPER)
        new_h = max(1, img.size[1] // SUPER)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    img.save(output_path)
    return output_path, img.size[0], img.size[1]


def add_hook_to_video(
    video_path: str,
    text: str,
    output_path: str,
    position: str = "top",
    font_scale: float = 1.0,
    style: str = "serif_card",
    aspect_ratio: str = "9:16",
    y_pct: Optional[float] = None,
) -> str:
    """Overlay hook onto video.

    Vertical placement (overlay_y) resolution order:
      1. If y_pct given (0-100), use that fraction of video_h directly.
      2. Else fall back to named `position` preset: top=10%, center=50%, bottom=70%.
    For `square_in_vertical` aspect, presets shift into the inner 1:1 area so
    the hook doesn't land on the black bars.
    """
    if not text or not text.strip():
        raise ValueError("hook text empty")
    if not os.path.exists(video_path):
        raise FileNotFoundError(video_path)

    video_w, video_h = _probe_dims(video_path)
    target_box_w = int(video_w * 0.9)

    hook_png = str(Path(output_path).with_suffix(".hook.png"))
    try:
        _, box_w, box_h = create_hook_image(
            text, target_box_w, hook_png, font_scale, style=style,
        )

        overlay_x = (video_w - box_w) // 2

        if y_pct is not None:
            # Slider mode: anchor top-edge of hook at y_pct of video height,
            # clamped so the whole box stays in frame.
            pct = max(0.0, min(100.0, float(y_pct))) / 100.0
            overlay_y = int(pct * video_h)
            overlay_y = max(0, min(video_h - box_h, overlay_y))
        elif aspect_ratio == "square_in_vertical":
            margin = 30
            square_h = video_w
            square_top = max(0, (video_h - square_h) // 2)
            square_bot = square_top + square_h
            if position == "center":
                overlay_y = (video_h - box_h) // 2
            elif position == "bottom":
                overlay_y = max(square_top, square_bot - box_h - margin)
            else:
                overlay_y = square_top + margin
        else:
            if position == "center":
                overlay_y = (video_h - box_h) // 2
            elif position == "bottom":
                overlay_y = int(video_h * 0.70)
            else:
                overlay_y = int(video_h * 0.10)

        cmd = [
            ffmpeg_path(), "-y",
            "-i", video_path,
            "-i", hook_png,
            "-filter_complex", f"[0:v][1:v]overlay={overlay_x}:{overlay_y}",
            "-c:a", "copy",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        return output_path
    finally:
        if os.path.exists(hook_png):
            try:
                os.remove(hook_png)
            except OSError:
                pass
