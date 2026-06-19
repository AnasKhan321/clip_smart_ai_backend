import json
import os
from pathlib import Path


DEFAULT_APP_SETTINGS = {
    "maintenance_mode": False,
}


def _settings_path() -> Path:
    storage = os.getenv("STORAGE_PATH", "./storage")
    return Path(storage) / "app_settings.json"


def load_app_settings() -> dict:
    path = _settings_path()
    if not path.exists():
        return DEFAULT_APP_SETTINGS.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_APP_SETTINGS.copy()
    return {**DEFAULT_APP_SETTINGS, **data}


def save_app_settings(settings: dict) -> dict:
    merged = {**DEFAULT_APP_SETTINGS, **settings}
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)
    return merged
