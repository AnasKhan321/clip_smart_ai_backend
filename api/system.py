from fastapi import APIRouter
from pydantic import BaseModel

from services.app_settings import load_app_settings

router = APIRouter(prefix="/system", tags=["system"])


class AppStatusOut(BaseModel):
    maintenance_mode: bool


@router.get("/status", response_model=AppStatusOut)
def app_status():
    settings = load_app_settings()
    return AppStatusOut(maintenance_mode=bool(settings.get("maintenance_mode")))
