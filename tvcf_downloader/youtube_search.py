from __future__ import annotations

import json
import re
import subprocess
import threading
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .config import PROJECT_ROOT
from .downloader import DownloadCancelled, resolve_ytdlp
from .models import MediaItem
from .text_utils import decode_output, subprocess_env


LogCallback = Optional[Callable[[str], None]]
StopCallback = Optional[Callable[[], bool]]
SEARCH_RESULT_COUNT = 10
MIN_MATCH_SCORE = 0.57
SEARCH_LOG_DIR = PROJECT_ROOT / "logs" / "youtube_search"

_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}
_SEARCH_LOCK = threading.Lock()


class YouTubeSearchError(RuntimeError):
    pass


class YouTubeMatchError(YouTubeSearchError):
    pass


@dataclass(frozen=True)
class YouTubeCandidate:
    video_id: str
    url: str
    title: str
    channel: str
    description: str
    duration: float | None
    score: float = 0.0
    title_score: float = 0.0
    chapter_score: float = 0.0
    brand_score: float = 0.0
    token_score: float = 0.0
    duration_score: float = 0.0
    search_score: float = 0.0


def find_youtube_media(
    item: MediaItem,
    ytdlp_cmd: Sequence[str] | None = None,
    log: LogCallback = None,
    should_stop: StopCallback = None,
    force_refresh: bool = False,
) -> MediaItem:
    if should_stop and should_stop():
        raise DownloadCancelled("사용자 중단 요청")

    query = build_search_query(item)
    if not query:
        raise YouTubeMatchError("YouTube 검색에 사용할 TVCF 제목이 없습니다.")

    if log:
        log(f"YouTube 검색: {query}")
    queries = [query]
    entries = search_youtube(
        query,
        ytdlp_cmd=ytdlp_cmd,
        should_stop=should_stop,
        force_refresh=force_refresh,
    )

    candidates = [score_candidate(item, entry, query_weight=1.0) for entry in entries]
    candidates = [candidate for candidate in candidates if candidate.url]
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    # A shorter fallback query helps when TVCF's chapter contains campaign copy
    # that the official YouTube upload omitted from its title.
    if not candidates or candidates[0].score < 0.72:
        fallback_query = build_search_query(item, compact=True)
        if fallback_query and fallback_query != query:
            if log:
                log(f"YouTube 보조 검색: {fallback_query}")
            queries.append(fallback_query)
            fallback_entries = search_youtube(
                fallback_query,
                ytdlp_cmd=ytdlp_cmd,
                should_stop=should_stop,
                force_refresh=force_refresh,
            )
            seen_urls = {candidate.url for candidate in candidates}
            candidates.extend(
                candidate
                for candidate in (
                    score_candidate(item, entry, query_weight=0.55)
                    for entry in fallback_entries
                )
                if candidate.url and candidate.url not in seen_urls
            )
            candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    report_path = save_search_report(item, queries, candidates)

    if not candidates:
        raise YouTubeMatchError(f"YouTube 검색 결과가 없습니다. / 검색 기록: {report_path}")

    best = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    if not is_confident_match(item, best, second_score):
        raise YouTubeMatchError(
            "YouTube에서 같은 영상으로 확신할 수 있는 후보를 찾지 못했습니다. "
            f"최고 점수={best.score:.0%}, 후보={best.title} / 검색 기록: {report_path}"
        )

    if log:
        duration_label = f"{best.duration:.0f}초" if best.duration is not None else "길이 미상"
        log(f"YouTube 일치: {best.title} / {best.channel or '채널 미상'} / {duration_label} / {best.score:.0%}")

    return MediaItem(
        idx=item.idx,
        nidx=item.nidx,
        mcode=item.mcode,
        title=item.title,
        chapter=item.chapter,
        brand=item.brand,
        published_date=item.published_date,
        registered_date=item.registered_date,
        country_code=item.country_code,
        category_code=item.category_code,
        category_name=item.category_name,
        duration=item.duration,
        play_url=best.url,
        source_page=item.source_page,
        stream_urls={"youtube": best.url},
    )


def build_search_query(item: MediaItem, compact: bool = False) -> str:
    values = [item.title]
    if not compact:
        values.append(item.chapter)
    values.append(item.brand)
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_query_part(value)
        key = normalize_text(cleaned)
        if cleaned and key and key not in seen and not any(key in existing for existing in seen):
            parts.append(cleaned)
            seen.add(key)
    if not parts:
        return ""
    return " ".join([*parts, "광고"])


def search_youtube(
    query: str,
    ytdlp_cmd: Sequence[str] | None = None,
    should_stop: StopCallback = None,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    cache_key = normalize_text(query)
    with _SEARCH_LOCK:
        cached = _SEARCH_CACHE.get(cache_key)
        if cached is not None and not force_refresh:
            return [dict(entry) for entry in cached]

        if should_stop and should_stop():
            raise DownloadCancelled("사용자 중단 요청")
        command = list(ytdlp_cmd or resolve_ytdlp())
        process = subprocess.run(
            [
                *command,
                "--flat-playlist",
                "--dump-single-json",
                "--no-warnings",
                "--no-playlist-reverse",
                f"ytsearch{SEARCH_RESULT_COUNT}:{query}",
            ],
            capture_output=True,
            env=subprocess_env(),
            timeout=60,
        )
        if should_stop and should_stop():
            raise DownloadCancelled("사용자 중단 요청")
        if process.returncode != 0:
            message = decode_output(process.stderr).strip() or "알 수 없는 yt-dlp 검색 오류"
            raise YouTubeSearchError(f"YouTube 검색 실패: {message}")

        try:
            payload = json.loads(decode_output(process.stdout))
        except json.JSONDecodeError as exc:
            raise YouTubeSearchError("YouTube 검색 결과 JSON을 읽지 못했습니다.") from exc

        raw_entries = payload.get("entries", []) if isinstance(payload, dict) else []
        entries = [
            sanitize_entry(entry, rank)
            for rank, entry in enumerate(raw_entries)
            if isinstance(entry, dict)
        ]
        _SEARCH_CACHE[cache_key] = entries
        return [dict(entry) for entry in entries]


def score_candidate(item: MediaItem, entry: dict[str, Any], query_weight: float = 1.0) -> YouTubeCandidate:
    video_id = str(entry.get("id") or "")
    url = str(entry.get("url") or "")
    if video_id and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={video_id}"
    title = str(entry.get("title") or "")
    channel = str(entry.get("channel") or entry.get("uploader") or "")
    description = str(entry.get("description") or "")[:1200]
    duration = float(entry["duration"]) if isinstance(entry.get("duration"), (int, float)) else None

    searchable_title = " ".join((title, channel, description))
    title_score = text_similarity(item.title, searchable_title)
    chapter_score = text_similarity(item.chapter, searchable_title)
    brand_score = max(text_similarity(item.brand, channel), text_similarity(item.brand, searchable_title))
    token_score = token_overlap(" ".join((item.brand, item.title, item.chapter)), searchable_title)
    duration_score = duration_similarity(item.duration, duration)
    rank = int(entry.get("search_rank") or 0)
    search_score = max(0.0, query_weight * (1.0 - rank * 0.06))

    score = (
        title_score * 0.14
        + chapter_score * 0.22
        + brand_score * 0.16
        + token_score * 0.12
        + duration_score * 0.18
        + search_score * 0.18
    )
    return YouTubeCandidate(
        video_id=video_id,
        url=url,
        title=title,
        channel=channel,
        description=description,
        duration=duration,
        score=round(score, 6),
        title_score=round(title_score, 6),
        chapter_score=round(chapter_score, 6),
        brand_score=round(brand_score, 6),
        token_score=round(token_score, 6),
        duration_score=round(duration_score, 6),
        search_score=round(search_score, 6),
    )


def is_confident_match(item: MediaItem, candidate: YouTubeCandidate, second_score: float) -> bool:
    if candidate.score < MIN_MATCH_SCORE:
        return False
    if item.duration and candidate.duration is not None:
        allowed_delta = max(2.5, float(item.duration) * 0.18)
        if abs(float(item.duration) - candidate.duration) > allowed_delta:
            return False
    creative_score = candidate.chapter_score if normalize_text(item.chapter) else candidate.title_score
    if creative_score < 0.25 and not (
        candidate.search_score >= 0.85
        and candidate.brand_score >= 0.85
        and candidate.duration_score >= 0.9
    ):
        return False
    if creative_score >= 0.85 and candidate.search_score >= 0.85 and candidate.duration_score >= 0.9:
        return True
    # Similar scores are acceptable only when the best candidate is a very
    # strong textual and duration match (often duplicate official uploads).
    if second_score and candidate.score - second_score < 0.025:
        return candidate.score >= 0.74 and candidate.duration_score >= 0.9
    return True


def text_similarity(expected: str, actual: str) -> float:
    left = normalize_text(expected)
    right = normalize_text(actual)
    if not left or not right:
        return 0.0
    if left in right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    overlap = token_overlap(expected, actual)
    return min(1.0, max(ratio, overlap))


def token_overlap(expected: str, actual: str) -> float:
    left = meaningful_tokens(expected)
    right = meaningful_tokens(actual)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def duration_similarity(expected: float | None, actual: float | None) -> float:
    if expected is None or actual is None:
        return 0.35
    delta = abs(float(expected) - float(actual))
    if delta <= 1.5:
        return 1.0
    tolerance = max(4.0, float(expected) * 0.25)
    return max(0.0, 1.0 - (delta - 1.5) / tolerance)


def meaningful_tokens(value: str) -> set[str]:
    ignored = {"광고", "영상", "공식", "full", "ver", "version", "the", "new"}
    return {
        token
        for token in re.findall(r"[0-9a-z가-힣]+", normalize_text(value))
        if len(token) > 1 and token not in ignored
    }


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return " ".join(re.findall(r"[0-9a-z가-힣]+", value))


def clean_query_part(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\r\n\t]+", " ", value)
    return re.sub(r"\s+", " ", value).strip(" |/")


def sanitize_entry(entry: dict[str, Any], search_rank: int = 0) -> dict[str, Any]:
    return {
        "id": str(entry.get("id") or ""),
        "url": str(entry.get("url") or ""),
        "title": str(entry.get("title") or ""),
        "channel": str(entry.get("channel") or entry.get("uploader") or ""),
        "uploader": str(entry.get("uploader") or ""),
        "description": str(entry.get("description") or "")[:1200],
        "duration": entry.get("duration") if isinstance(entry.get("duration"), (int, float)) else None,
        "view_count": entry.get("view_count") if isinstance(entry.get("view_count"), int) else None,
        "search_rank": search_rank,
    }


def save_search_report(item: MediaItem, queries: list[str], candidates: list[YouTubeCandidate]) -> Path:
    SEARCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    item_id = safe_part(item.nidx or item.idx or item.mcode or item.display_title)
    path = SEARCH_LOG_DIR / f"{item_id}.json"
    payload = {
        "tvcf_item": {
            "id": item.nidx or item.idx or item.mcode,
            "title": item.title,
            "chapter": item.chapter,
            "brand": item.brand,
            "duration": item.duration,
            "published_date": item.published_date,
            "registered_date": item.registered_date,
        },
        "queries": queries,
        "minimum_score": MIN_MATCH_SCORE,
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def safe_part(value: str) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in ("-", "_"))
    return cleaned[:80] or "unknown"
