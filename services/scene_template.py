"""Composite a clip into a scene template's video slot (TV/tablet screen cutout).

Templates live in templates/hooks/<id>/{overlay.png,meta.json} (built by
scripts/build_hook_templates.py). overlay.png has its screen region punched to
alpha=0; meta.json carries the slot's axis-aligned bbox (video_rect) and exact
corner quad (screen_quad), plus a `rotated` flag for tilted screens (e.g. a
tablet held at an angle) that need a perspective warp, not a straight overlay.
"""
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Tuple

from services.media_tools import ffmpeg_path, ffprobe_path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "hooks"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _template_dir(template_id: str) -> Path:
    """template_id comes straight from the export request body — validate
    against an allowlist pattern and confirm the resolved path stays inside
    TEMPLATES_DIR before it touches the filesystem, closing path traversal."""
    if not _ID_RE.fullmatch(template_id or ""):
        raise ValueError(f"invalid scene template id: {template_id!r}")
    resolved_root = TEMPLATES_DIR.resolve()
    d = (TEMPLATES_DIR / template_id).resolve()
    if not str(d).startswith(str(resolved_root) + os.sep):
        raise ValueError(f"invalid scene template id: {template_id!r}")
    return d


def get_template_meta(template_id: str) -> dict:
    meta_path = _template_dir(template_id) / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"unknown scene template: {template_id}")
    return json.loads(meta_path.read_text())


def _probe_dims(video_path: str) -> Tuple[int, int]:
    res = subprocess.check_output(
        [ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
         video_path],
        timeout=30,
    ).decode().strip().split("\n")[0].split("x")
    return int(res[0]), int(res[1])


def apply_scene_template(video_path: str, template_id: str, output_path: str) -> str:
    meta = get_template_meta(template_id)
    canvas_w, canvas_h = meta["canvas"]
    overlay_png = str(_template_dir(template_id) / "overlay.png")

    rx, ry, rw, rh = meta["video_rect"]
    bbox_x, bbox_y = round(rx * canvas_w), round(ry * canvas_h)
    bbox_w, bbox_h = round(rw * canvas_w), round(rh * canvas_h)

    # overlay.png's hole is dilated by edge_feather_px past the exact screen
    # quad (kills anti-aliased green fringe on the template asset) — grow the
    # video rect by the same margin so it fully fills the larger hole instead
    # of leaving a thin gap of the black pad showing at the edge.
    feather = int(meta.get("edge_feather_px", 0))
    bbox_x -= feather
    bbox_y -= feather
    bbox_w += 2 * feather
    bbox_h += 2 * feather

    # even dims — libx264 requires it, and cover-fit crop can land on an odd pixel
    bbox_w -= bbox_w % 2
    bbox_h -= bbox_h % 2

    fit = (
        f"scale={bbox_w}:{bbox_h}:force_original_aspect_ratio=increase,"
        f"crop={bbox_w}:{bbox_h}"
    )

    if meta.get("rotated"):
        tl, tr, br, bl = _quad_corners_px(meta, canvas_w, canvas_h)
        # perspective's x2/y2 = bottom-LEFT, x3/y3 = bottom-RIGHT (not bl/br order)
        persp = (
            f"perspective="
            f"x0={tl[0] - bbox_x}:y0={tl[1] - bbox_y}:"
            f"x1={tr[0] - bbox_x}:y1={tr[1] - bbox_y}:"
            f"x2={bl[0] - bbox_x}:y2={bl[1] - bbox_y}:"
            f"x3={br[0] - bbox_x}:y3={br[1] - bbox_y}:"
            f"sense=destination"
        )
        fit = f"{fit},{persp}"

    filter_complex = (
        f"[0:v]{fit},pad={canvas_w}:{canvas_h}:{bbox_x}:{bbox_y}:black[vid];"
        f"[vid][1:v]overlay=0:0:format=auto[out]"
    )

    cmd = [
        ffmpeg_path(), "-y",
        "-i", video_path,
        "-i", overlay_png,
        "-filter_complex", filter_complex,
        "-map", "[out]", "-map", "0:a?",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    return output_path


def _quad_corners_px(meta: dict, canvas_w: int, canvas_h: int):
    tl, tr, br, bl = meta["screen_quad"]
    to_px = lambda p: (round(p[0] * canvas_w), round(p[1] * canvas_h))
    return to_px(tl), to_px(tr), to_px(br), to_px(bl)
