import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import PROJECT_ROOT
from .models import MediaItem


LOG_DIR = PROJECT_ROOT / "logs"
SESSION_DIR = LOG_DIR / "sessions"
ERROR_DIR = LOG_DIR / "errors"


NETWORK_MARKERS = (
    "IncompleteRead",
    "bytes read",
    "timed out",
    "timeout",
    "Remote end closed",
    "Connection",
    "URLError",
)


def classify_error(stage: str, exc: BaseException | str) -> str:
    message = str(exc)
    lower = message.lower()
    stage_lower = stage.lower()

    if exc.__class__.__name__ == "YouTubeMatchError":
        return "YouTube 일치 없음"
    if "스트림" in message and ("없" in message or "찾지" in message):
        return "스트림 없음"
    if "HTTP 404" in message:
        return "상세 오류"
    if "HTTP 403" in message:
        return "접근 거부"
    if "YouTube" in message and ("일치" in message or "검색 결과" in message or "검색에 사용할" in message):
        return "YouTube 일치 없음"
    if "HTTP 429" in message:
        return "요청 제한"
    if any(f"HTTP {code}" in message for code in ("500", "502", "503", "504")):
        return "네트워크 오류"
    if any(marker.lower() in lower for marker in NETWORK_MARKERS):
        return "네트워크 오류"
    if "yt-dlp" in lower or "youtube" in lower:
        return "yt-dlp 실패"
    if "ffmpeg" in lower or "invalid argument" in lower or "exit " in lower:
        return "ffmpeg 실패"
    if "손상" in message or "확인할 수 없습니다" in message:
        return "파일 손상"
    if "상세" in stage or "media" in stage_lower or "play" in lower:
        return "상세 오류"
    return "알 수 없는 오류"


def item_to_dict(item: Optional[MediaItem]) -> dict[str, Any]:
    if item is None:
        return {}
    return {
        "idx": item.idx,
        "nidx": item.nidx,
        "mcode": item.mcode,
        "title": item.title,
        "chapter": item.chapter,
        "brand": item.brand,
        "display_title": item.display_title,
        "published_date": item.published_date,
        "registered_date": item.registered_date,
        "country_code": item.country_code,
        "category_code": item.category_code,
        "category_name": item.category_name,
        "duration": item.duration,
        "play_url": item.play_url,
        "source_page": item.source_page,
        "stream_urls": dict(item.stream_urls),
    }


def save_error_case(
    item: Optional[MediaItem],
    stage: str,
    exc: BaseException | str,
    context: Optional[dict[str, Any]] = None,
) -> Path:
    ERROR_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    item_id = _safe_part((item.nidx or item.idx or item.mcode) if item else "unknown")
    category = classify_error(stage, exc)
    case_id = f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{item_id}_{_safe_part(category)}"
    json_path = ERROR_DIR / f"{case_id}.json"
    text_path = ERROR_DIR / f"{case_id}.txt"

    payload = {
        "case_id": case_id,
        "created_at": now.isoformat(timespec="seconds"),
        "category": category,
        "stage": stage,
        "error": str(exc),
        "traceback": _format_traceback(exc),
        "item": item_to_dict(item),
        "context": context or {},
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    text_path.write_text(_format_error_text(payload), encoding="utf-8")
    return json_path


class SessionLog:
    FLUSH_EVERY = 20

    def __init__(self, run_summary: str) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now()
        self.session_id = self.started_at.strftime("%Y%m%d_%H%M%S_%f")
        self.run_summary = run_summary
        self.record_count = 0
        self._dirty_count = 0
        self._closed = False
        self.path = SESSION_DIR / f"{self.session_id}.jsonl"
        self.meta_path = SESSION_DIR / f"{self.session_id}.json"
        self._stream = self.path.open("a", encoding="utf-8")
        self.save()

    def add(
        self,
        status: str,
        stage: str,
        item: Optional[MediaItem] = None,
        message: str = "",
        output_path: str = "",
        error_path: str = "",
    ) -> None:
        if self._closed:
            return
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "stage": stage,
            "message": message,
            "output_path": output_path,
            "error_path": error_path,
            "item": item_to_dict(item),
        }
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.record_count += 1
        self._dirty_count += 1
        if self._dirty_count >= self.FLUSH_EVERY:
            self.flush()

    def save(self) -> None:
        payload = {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "run_summary": self.run_summary,
            "record_count": self.record_count,
            "records_path": str(self.path),
        }
        self.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def flush(self) -> None:
        if self._closed:
            return
        self._stream.flush()
        self._dirty_count = 0
        self.save()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._stream.close()
            self._closed = True


def read_error_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_part(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_"))
    return cleaned[:80] or "unknown"


def _format_traceback(exc: BaseException | str) -> str:
    if isinstance(exc, BaseException):
        formatted = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return "".join(formatted).strip()
    return ""


def _format_error_text(payload: dict[str, Any]) -> str:
    item = payload.get("item", {})
    context = payload.get("context", {})
    lines = [
        f"Case ID: {payload.get('case_id')}",
        f"Created: {payload.get('created_at')}",
        f"Category: {payload.get('category')}",
        f"Stage: {payload.get('stage')}",
        "",
        "[Item]",
        f"Title: {item.get('display_title', '')}",
        f"ID: {item.get('nidx') or item.get('idx') or item.get('mcode') or ''}",
        f"Published: {item.get('published_date', '')}",
        f"Registered: {item.get('registered_date', '')}",
        f"Play URL: {item.get('play_url', '')}",
        "",
        "[Context]",
        json.dumps(context, ensure_ascii=False, indent=2),
        "",
        "[Error]",
        str(payload.get("error", "")),
    ]
    if payload.get("traceback"):
        lines.extend(["", "[Traceback]", payload["traceback"]])
    return "\n".join(lines) + "\n"
