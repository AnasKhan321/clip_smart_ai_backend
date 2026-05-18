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
        "shadow_offset": (8, 10),
        "shadow_alpha": 180,
        "shadow_blur": 6,
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
        "shadow_offset": (8, 10),
        "shadow_alpha": 200,
        "shadow_blur": 6,
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
        "shadow_offset": (6, 8),
        "shadow_alpha": 200,
        "shadow_blur": 5,
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
    fg = cfg["fg"]
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

    base_font_size = int(target_width * 0.05)
    font_size = max(12, int(base_font_size * font_scale))
    font = _load_font(cfg.get("font"), font_size)

    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    max_text_width = target_width - (2 * padding_x)

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
            line_heights.append(font_size)
            continue
        w = _line_text_width(draw, line, font, letter_spacing)
        max_line_w = max(max_line_w, w)
        bb = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bb[3] - bb[1])

    # Account for stroke / glow padding so glyphs aren't clipped
    extra = max(text_stroke_w, *(int(l[1] + l[2]) for l in glow_layers) if glow_layers else 0)
    inner_pad_text = extra

    box_w = max(max_line_w + 2 * padding_x, int(target_width * 0.3)) + 2 * inner_pad_text
    if not line_heights:
        total_text_h = font_size
    else:
        total_text_h = sum(line_heights) + (len(line_heights) - 1) * line_spacing
    box_h = total_text_h + 2 * padding_y + 2 * inner_pad_text

    margin = max(40, shadow_blur + abs(shadow_offset[1]) + 10)
    canvas_w = box_w + 2 * margin
    canvas_h = box_h + 2 * margin
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # Drop shadow (only when there is a solid box; otherwise shadow follows text via stroke)
    if bg is not None and shadow_alpha > 0 and (shadow_offset[0] or shadow_offset[1] or shadow_blur):
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

    # Box fill + outline
    if bg is not None:
        box_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
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
                y += font_size + line_spacing
                continue
            line_w = _line_text_width(ld, line, font, letter_spacing)
            x = margin + (box_w - line_w) // 2
            _draw_line_with_spacing(
                ld, (x, y), line, font, fill=color,
                stroke_width=stroke_w,
                stroke_fill=stroke_color,
                letter_spacing=letter_spacing,
            )
            y += (line_heights[i] if i < len(line_heights) else font_size) + line_spacing
        return layer

    for color, blur_r, expand in glow_layers:
        g_layer = _render_text_layer(color, stroke_w=int(expand), stroke_color=color)
        if blur_r > 0:
            g_layer = g_layer.filter(ImageFilter.GaussianBlur(int(blur_r)))
        img = Image.alpha_composite(img, g_layer)

    # Drop shadow for stroked text (no box case)
    if bg is None and shadow_alpha > 0 and (shadow_offset[0] or shadow_offset[1]):
        sh_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh_layer)
        y = text_origin_y + shadow_offset[1]
        for i, line in enumerate(lines):
            if not line:
                y += font_size + line_spacing
                continue
            line_w = _line_text_width(sd, line, font, letter_spacing)
            x = margin + (box_w - line_w) // 2 + shadow_offset[0]
            _draw_line_with_spacing(
                sd, (x, y), line, font, fill=(0, 0, 0, shadow_alpha),
                stroke_width=text_stroke_w,
                stroke_fill=(0, 0, 0, shadow_alpha),
                letter_spacing=letter_spacing,
            )
            y += (line_heights[i] if i < len(line_heights) else font_size) + line_spacing
        if shadow_blur > 0:
            sh_layer = sh_layer.filter(ImageFilter.GaussianBlur(shadow_blur))
        img = Image.alpha_composite(img, sh_layer)

    # Main text
    main_layer = _render_text_layer(
        fg, stroke_w=text_stroke_w, stroke_color=text_stroke if text_stroke else None
    )
    img = Image.alpha_composite(img, main_layer)

    # Tilt
    if tilt_deg:
        img = img.rotate(tilt_deg, resample=Image.BICUBIC, expand=True)

    img.save(output_path)
    return output_path, img.size[0], img.size[1]


def add_hook_to_video(
    video_path: str,
    text: str,
    output_path: str,
    position: str = "top",
    font_scale: float = 1.0,
    style: str = "serif_card",
) -> str:
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
