import atexit
import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .config import PROJECT_ROOT
from .models import MediaItem


HISTORY_PATH = PROJECT_ROOT / "download_history.json"
_HISTORY_LOCK = threading.RLock()
_HISTORY_CACHE: dict[str, Any] | None = None
_HISTORY_DIRTY_COUNT = 0
_HISTORY_FLUSH_EVERY = 10


def tvcf_id(item: MediaItem) -> str:
    return item.nidx or item.idx or item.mcode


def _load_history_from_disk() -> dict[str, Any]:
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


def load_history() -> dict[str, Any]:
    global _HISTORY_CACHE
    with _HISTORY_LOCK:
        if _HISTORY_CACHE is None:
            _HISTORY_CACHE = _load_history_from_disk()
        return _HISTORY_CACHE


def save_history(history: dict[str, Any]) -> None:
    global _HISTORY_CACHE, _HISTORY_DIRTY_COUNT
    with _HISTORY_LOCK:
        _HISTORY_CACHE = history
        _write_history_unlocked(history)
        _HISTORY_DIRTY_COUNT = 0


def flush_history() -> None:
    global _HISTORY_DIRTY_COUNT
    with _HISTORY_LOCK:
        if _HISTORY_CACHE is None or _HISTORY_DIRTY_COUNT <= 0:
            return
        _write_history_unlocked(_HISTORY_CACHE)
        _HISTORY_DIRTY_COUNT = 0


def _write_history_unlocked(history: dict[str, Any]) -> None:
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

    try:
        stat = path.stat()
    except OSError:
        return None

    if (
        record.get("file_size") == stat.st_size
        and record.get("file_mtime_ns") == stat.st_mtime_ns
        and stat.st_size >= 1024
    ):
        return path

    return path if validator(path) else None


def record_download(item: MediaItem, path: Path, quality: str, status: str) -> None:
    key = tvcf_id(item)
    if not key:
        return

    try:
        stat = path.stat() if path.exists() else None
        file_size = stat.st_size if stat else 0
        file_mtime_ns = stat.st_mtime_ns if stat else 0
    except OSError:
        file_size = 0
        file_mtime_ns = 0

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
            "file_mtime_ns": file_mtime_ns,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        _mark_history_dirty_unlocked(history)


def _mark_history_dirty_unlocked(history: dict[str, Any]) -> None:
    global _HISTORY_CACHE, _HISTORY_DIRTY_COUNT
    _HISTORY_CACHE = history
    _HISTORY_DIRTY_COUNT += 1
    if _HISTORY_DIRTY_COUNT >= _HISTORY_FLUSH_EVERY:
        _write_history_unlocked(history)
        _HISTORY_DIRTY_COUNT = 0


atexit.register(flush_history)
