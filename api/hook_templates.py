"""Scene template listing — background templates with a video slot (green-screen punched to alpha)."""
import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/hook-templates", tags=["hook-templates"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "hooks"


class HookTemplateOut(BaseModel):
    id: str
    name: str
    canvas: list[int]
    video_rect: list[float]
    overlay_url: str


@router.get("", response_model=list[HookTemplateOut])
def list_hook_templates():
    if not TEMPLATES_DIR.exists():
        return []
    out = []
    for d in sorted(TEMPLATES_DIR.iterdir()):
        if not d.is_dir() or d.name == "_raw":
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        out.append(
            HookTemplateOut(
                id=meta["id"],
                name=meta["name"],
                canvas=meta["canvas"],
                video_rect=meta["video_rect"],
                overlay_url=f"/hook-templates-static/{meta['id']}/overlay.png",
            )
        )
    return out
