import json
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "download_dir": str(PROJECT_ROOT / "downloads"),
    "quality": "가능한 최고화질",
    "date_basis": "published",
    "max_pages": 0,
    "parallel_downloads": 1,
    "completion_actions": {
        "notify": "off",
        "open_folder": "off",
        "shutdown": "off",
    },
    "prefer_ytdlp": True,
    "use_playwright_fallback": True,
    "last_checkpoint": "작업 없음",
}


def load_config() -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(saved, dict):
                config.update(saved)
        except (OSError, json.JSONDecodeError):
            pass
    return config


def save_config(config: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
