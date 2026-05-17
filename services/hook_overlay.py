"""Viral hook text overlay.

Renders white rounded box with black serif text via PIL, then overlays onto
video as single ffmpeg pass. Adapted from openshorts/hooks.py — dropped the
runtime urllib font fetch in favor of bundled fonts/NotoSerif-Bold.ttf.
"""
import os
import subprocess
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from services.media_tools import ffmpeg_path, ffprobe_path

FONT_PATH = str(Path(__file__).resolve().parent.parent / "fonts" / "NotoSerif-Bold.ttf")

# Hook style presets. All use the bundled serif. Differ in bg fill,
# text color, outline, corner radius, padding, drop shadow.
HOOK_STYLES = {
    "serif_white": {
        "label": "Serif card",
        "bg": (255, 255, 255, 240),
        "fg": (0, 0, 0, 255),
        "outline": None,
        "outline_w": 0,
        "shadow": True,
        "corner_radius": 20,
        "padding_x": 30,
        "padding_y": 25,
    },
    "black_box": {
        "label": "Black box",
        "bg": (0, 0, 0, 235),
        "fg": (255, 255, 255, 255),
        "outline": None,
        "outline_w": 0,
        "shadow": True,
        "corner_radius": 8,
        "padding_x": 28,
        "padding_y": 22,
    },
    "sticker_yellow": {
        "label": "Yellow sticker",
        "bg": (255, 222, 0, 250),
        "fg": (0, 0, 0, 255),
        "outline": (0, 0, 0, 255),
        "outline_w": 3,
        "shadow": True,
        "corner_radius": 14,
        "padding_x": 28,
        "padding_y": 22,
    },
    "breaking_news": {
        "label": "Breaking news",
        "bg": (210, 30, 30, 245),
        "fg": (255, 255, 255, 255),
        "outline": None,
        "outline_w": 0,
        "shadow": True,
        "corner_radius": 0,
        "padding_x": 32,
        "padding_y": 22,
    },
    "outline_only": {
        "label": "No box",
        "bg": None,
        "fg": (255, 255, 255, 255),
        "outline": (0, 0, 0, 255),
        "outline_w": 4,
        "shadow": False,
        "corner_radius": 0,
        "padding_x": 20,
        "padding_y": 12,
    },
}


def get_hook_style(style_id: str) -> dict:
    return HOOK_STYLES.get(style_id, HOOK_STYLES["serif_white"])


def _probe_dims(video_path: str) -> Tuple[int, int]:
    res = subprocess.check_output(
        [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
         video_path],
        timeout=30,
    ).decode().strip().split("\n")[0].split("x")
    return int(res[0]), int(res[1])


def create_hook_image(
    text: str,
    target_width: int,
    output_path: str,
    font_scale: float = 1.0,
    style: str = "serif_white",
) -> Tuple[str, int, int]:
    """Render hook PNG with chosen style. Returns (png_path, canvas_w, canvas_h)."""
    cfg = get_hook_style(style)
    padding_x = cfg["padding_x"]
    padding_y = cfg["padding_y"]
    line_spacing = 20
    corner_radius = cfg["corner_radius"]
    shadow_enabled = cfg["shadow"]
    shadow_offset = (5, 5)
    bg = cfg["bg"]
    fg = cfg["fg"]
    text_outline = cfg["outline"] if bg is None else None
    text_outline_w = cfg["outline_w"] if bg is None else 0
    box_outline = cfg["outline"] if (bg is not None and cfg["outline"]) else None
    box_outline_w = cfg["outline_w"] if (bg is not None and cfg["outline"]) else 0

    base_font_size = int(target_width * 0.05)
    font_size = max(12, int(base_font_size * font_scale))

    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except Exception:
        font = ImageFont.load_default()

    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    max_text_width = target_width - (2 * padding_x)

    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split()
        current: list[str] = []
        for word in words:
            test = " ".join(current + [word])
            bbox = draw.textbbox((0, 0), test, font=font)
            if (bbox[2] - bbox[0]) <= max_text_width:
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
    line_heights: list[int] = []
    for line in lines:
        if not line:
            line_heights.append(font_size)
            continue
        bbox = draw.textbbox((0, 0), line, font=font)
        max_line_w = max(max_line_w, bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])

    box_w = max(max_line_w + 2 * padding_x, int(target_width * 0.3))
    if not line_heights:
        total_text_h = font_size
    else:
        total_text_h = sum(line_heights) + (len(line_heights) - 1) * line_spacing
    box_h = total_text_h + 2 * padding_y

    canvas_w = box_w + 40
    canvas_h = box_h + 40
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    if bg is not None and shadow_enabled:
        shadow_draw = ImageDraw.Draw(img)
        shadow_box = [
            (20 + shadow_offset[0], 20 + shadow_offset[1]),
            (20 + box_w + shadow_offset[0], 20 + box_h + shadow_offset[1]),
        ]
        shadow_draw.rounded_rectangle(shadow_box, radius=corner_radius, fill=(0, 0, 0, 100))
        img = img.filter(ImageFilter.GaussianBlur(5))

    final_draw = ImageDraw.Draw(img)
    if bg is not None:
        main_box = [(20, 20), (20 + box_w, 20 + box_h)]
        final_draw.rounded_rectangle(
            main_box, radius=corner_radius, fill=bg,
            outline=box_outline, width=box_outline_w if box_outline else 0,
        )

    current_y = 20 + padding_y - 2
    for i, line in enumerate(lines):
        if not line:
            current_y += font_size + line_spacing
            continue
        bbox = final_draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        line_h = line_heights[i] if i < len(line_heights) else (bbox[3] - bbox[1])
        x = 20 + (box_w - line_w) // 2
        if text_outline and text_outline_w > 0:
            final_draw.text(
                (x, current_y), line, font=font, fill=fg,
                stroke_width=text_outline_w, stroke_fill=text_outline,
            )
        else:
            final_draw.text((x, current_y), line, font=font, fill=fg)
        current_y += line_h + line_spacing

    img.save(output_path)
    return output_path, canvas_w, canvas_h


def add_hook_to_video(
    video_path: str,
    text: str,
    output_path: str,
    position: str = "top",
    font_scale: float = 1.0,
    style: str = "serif_white",
) -> str:
    """Overlay hook image onto video. Returns output_path on success."""
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
