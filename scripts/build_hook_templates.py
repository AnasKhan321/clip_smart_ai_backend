"""
Drop template PNG/JPGs (with a pure-green #00FF00 TV-screen slot) into
templates/hooks/_raw/, then run:

    python scripts/build_hook_templates.py

Idempotent: already-built templates (matched by source filename stem) are
skipped, so it's safe to rerun any time you add more raw images.
"""
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

RAW_DIR = Path(__file__).parent.parent / "templates" / "hooks" / "_raw"
OUT_DIR = Path(__file__).parent.parent / "templates" / "hooks"

GREEN_TOL = dict(g_min=140, rb_max=110, ratio=1.4)
ROTATED_THRESHOLD_DEG = 2.0  # angle beyond this needs perspective-warp compositing, not simple overlay


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def green_mask(arr: np.ndarray) -> np.ndarray:
    r, g, b = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
    return (
        (g > GREEN_TOL["g_min"])
        & (r < GREEN_TOL["rb_max"])
        & (b < GREEN_TOL["rb_max"])
        & (g > r * GREEN_TOL["ratio"])
        & (g > b * GREEN_TOL["ratio"])
    )


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as tl, tr, br, bl (standard perspective-transform order)."""
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl])


def find_screen_quad(mask: np.ndarray) -> tuple[np.ndarray, float]:
    """Largest green contour → minimum-area rotated rect (handles tilted screens,
    not just front-on). Returns (4 ordered corner points, rotation angle in degrees)."""
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("no green region found")
    largest = max(contours, key=cv2.contourArea)
    rect = cv2.minAreaRect(largest)  # (center, (w,h), angle)
    box = cv2.boxPoints(rect)
    corners = _order_corners(box)

    (_, _), (rw, rh), angle = rect
    # minAreaRect angle is ambiguous mod 90 — normalize to how far off-axis the
    # rect is from perfectly horizontal/vertical (0 = front-on, axis-aligned).
    off_axis = abs(angle % 90)
    off_axis = min(off_axis, 90 - off_axis)
    return corners, off_axis


def build_one(src: Path, force: bool = False) -> None:
    slug = slugify(src.stem)
    dest_dir = OUT_DIR / slug
    if (dest_dir / "meta.json").exists() and not force:
        print(f"skip (already built): {src.name}")
        return

    img = Image.open(src).convert("RGBA")
    arr = np.array(img)
    mask = green_mask(arr)
    corners, off_axis_deg = find_screen_quad(mask)
    rotated = off_axis_deg > ROTATED_THRESHOLD_DEG

    x0, y0 = float(corners[:, 0].min()), float(corners[:, 1].min())
    x1, y1 = float(corners[:, 0].max()), float(corners[:, 1].max())
    w, h = x1 - x0, y1 - y0

    ratio = w / h if h else 0
    if not (1.5 < ratio < 2.0):
        print(f"warn: {src.name} screen bbox ratio {ratio:.2f} isn't ~16:9 (got {w:.0f}x{h:.0f})")
    if rotated:
        print(f"note: {src.name} screen is tilted ({off_axis_deg:.1f} deg off-axis) — "
              f"needs perspective-warp compositing, not simple scale+overlay")

    # Punch exact mask, not the bounding box — a tilted screen's bbox also
    # covers bezel/background in its corner triangles; punching those to
    # alpha=0 too would let composited video bleed past the real screen edges.
    arr[mask, 3] = 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(dest_dir / "overlay.png")

    meta = {
        "id": slug,
        "name": src.stem.replace("_", " ").replace("-", " ").title(),
        "canvas": [img.width, img.height],
        "video_rect": [x0 / img.width, y0 / img.height, w / img.width, h / img.height],
        "screen_quad": [[round(float(px) / img.width, 5), round(float(py) / img.height, 5)] for px, py in corners],
        "rotated": rotated,
    }
    (dest_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"built: {slug}  bbox=({x0:.0f},{y0:.0f},{w:.0f},{h:.0f})  "
          f"rotated={rotated}  canvas={img.width}x{img.height}")


def main() -> None:
    force = "--force" in sys.argv
    if not RAW_DIR.exists():
        print(f"no raw dir at {RAW_DIR}, nothing to do")
        return
    files = [p for p in RAW_DIR.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
    if not files:
        print(f"no images in {RAW_DIR}")
        return
    for f in sorted(files):
        try:
            build_one(f, force=force)
        except ValueError as e:
            print(f"error: {f.name}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
