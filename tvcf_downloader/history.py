import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .config import PROJECT_ROOT
from .models import MediaItem


HISTORY_PATH = PROJECT_ROOT / "download_history.json"
_HISTORY_LOCK = threading.Lock()


def tvcf_id(item: MediaItem) -> str:
    return item.nidx or item.idx or item.mcode


def load_history() -> dict[str, Any]:
    if not HISTORY_PATH.exists():
        return {"items": {}}
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        backup_path = HISTORY_PATH.with_suffix(f".broken_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            shutil.move(str(HISTORY_PATH), str(backup_path))
        except OSError:
            pass
        return {"items": {}}

    if not isinstance(data, dict):
        return {"items": {}}
    items = data.get("items")
    if not isinstance(items, dict):
        data["items"] = {}
    return data


def save_history(history: dict[str, Any]) -> None:
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def find_record(item: MediaItem) -> Optional[dict[str, Any]]:
    key = tvcf_id(item)
    if not key:
        return None
    with _HISTORY_LOCK:
        history = load_history()
    record = history.get("items", {}).get(key)
    return record if isinstance(record, dict) else None


def valid_record_path(record: dict[str, Any], validator: Callable[[Path], bool]) -> Optional[Path]:
    saved_path = record.get("saved_path", "")
    if not saved_path:
        return None
    path = Path(saved_path)
    if not path.exists() or not path.is_file():
        return None
    return path if validator(path) else None


def record_download(item: MediaItem, path: Path, quality: str, status: str) -> None:
    key = tvcf_id(item)
    if not key:
        return

    try:
        file_size = path.stat().st_size if path.exists() else 0
    except OSError:
        file_size = 0

    with _HISTORY_LOCK:
        history = load_history()
        items = history.setdefault("items", {})
        items[key] = {
            "tvcf_id": key,
            "title": item.display_title,
            "date": item.date_label(),
            "detail_url": item.play_url or item.source_page,
            "saved_path": str(path),
            "quality": quality,
            "status": status,
            "file_size": file_size,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_history(history)
