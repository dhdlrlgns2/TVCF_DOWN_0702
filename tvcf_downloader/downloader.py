import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence
from urllib.request import Request, urlopen

from .config import PROJECT_ROOT
from .ffmpeg_manager import ensure_ffmpeg
from .history import find_record, record_download, valid_record_path
from .models import MediaItem
from .text_utils import decode_output, subprocess_env


LogCallback = Optional[Callable[[str], None]]
StopCallback = Optional[Callable[[], bool]]


INVALID_FILENAME_CHARS = r'[\\/*?:"<>|]'


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(DownloadError):
    pass


@dataclass
class DownloadResult:
    path: Path
    skipped: bool = False
    repaired: bool = False
    history_skipped: bool = False


YTDLP_EXE = PROJECT_ROOT / "bin" / "yt-dlp.exe"
YTDLP_MANIFEST_PATH = PROJECT_ROOT / "bin" / "yt-dlp_manifest.json"
YTDLP_DOWNLOAD_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_UPDATE_DAYS = 7


def clean_filename(text: str, max_length: int = 120) -> str:
    text = re.sub(INVALID_FILENAME_CHARS, "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:max_length].rstrip() or "untitled")


def build_output_path(item: MediaItem, download_dir: str, date_basis: str) -> Path:
    date_part = item.date_label(date_basis).replace("-", "")
    title_part = clean_filename(item.display_title)
    id_part = item.idx or item.nidx or item.mcode
    filename = clean_filename(f"{date_part}_{title_part}_{id_part}") + ".mp4"
    return Path(download_dir) / filename


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise DownloadError(f"파일명을 만들 수 없습니다: {path}")


def resolve_ffprobe(ffmpeg: str) -> str:
    ffmpeg_path = Path(ffmpeg)
    bundled = ffmpeg_path.with_name("ffprobe.exe")
    if bundled.exists():
        return str(bundled)

    found = shutil.which("ffprobe")
    return found or ""


def is_valid_media_file(path: Path, ffprobe: str) -> bool:
    if not path.exists() or path.stat().st_size < 1024:
        return False
    if not ffprobe:
        return True

    try:
        duration_process = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            env=subprocess_env(),
            timeout=20,
            check=True,
        )
        duration = decode_output(duration_process.stdout).strip()
        has_video_process = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            env=subprocess_env(),
            timeout=20,
            check=True,
        )
        has_video = decode_output(has_video_process.stdout).strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return False

    try:
        return float(duration) > 0 and "video" in has_video.lower()
    except ValueError:
        return False


def verify_downloaded_file(path: Path, ffprobe: str) -> None:
    if not is_valid_media_file(path, ffprobe):
        raise DownloadError(f"다운로드 결과 파일이 손상되었거나 확인할 수 없습니다: {path}")


def cleanup_stale_download_files(output_path: Path) -> None:
    stale_paths = {
        output_path.with_name(f"{output_path.stem}.part{output_path.suffix}"),
        output_path.with_suffix(output_path.suffix + ".part"),
    }
    for pattern in (
        f"{output_path.stem}.f*.*",
        f"{output_path.stem}.*.part",
        f"{output_path.stem}.part*",
    ):
        stale_paths.update(output_path.parent.glob(pattern))

    for stale_path in stale_paths:
        if stale_path == output_path:
            continue
        if stale_path.exists() and stale_path.is_file():
            try:
                stale_path.unlink()
            except OSError:
                pass


def choose_stream(item: MediaItem, quality: str) -> str:
    if quality in {"가능한 최고화질", "best", "BEST"}:
        for fallback in ("HD", "SD", "mobile", "stream", "youtube", "urf", "url", "extSrc"):
            if fallback in item.stream_urls and item.stream_urls[fallback]:
                return item.stream_urls[fallback]

    if quality in item.stream_urls and item.stream_urls[quality]:
        return item.stream_urls[quality]
    for fallback in ("HD", "SD", "mobile", "stream", "youtube", "urf", "url", "extSrc"):
        if fallback in item.stream_urls and item.stream_urls[fallback]:
            return item.stream_urls[fallback]
    for value in item.stream_urls.values():
        if value:
            return value
    raise DownloadError("다운로드할 스트림 URL이 없습니다.")


def resolve_ytdlp(log: LogCallback = None) -> Sequence[str]:
    if YTDLP_EXE.exists():
        return [str(YTDLP_EXE)]

    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return _download_ytdlp(log)

    return [sys.executable, "-m", "yt_dlp"]


def ensure_ytdlp(log: LogCallback = None, max_age_days: int = YTDLP_UPDATE_DAYS) -> Sequence[str]:
    if not YTDLP_EXE.exists():
        return _download_ytdlp(log)

    if _manifest_is_fresh(YTDLP_MANIFEST_PATH, max_age_days):
        if log:
            version = ytdlp_version([str(YTDLP_EXE)])
            safe_log(log, f"yt-dlp 확인: {version or YTDLP_EXE}")
        return [str(YTDLP_EXE)]

    if log:
        safe_log(log, "yt-dlp 최신 버전을 확인합니다.")
    try:
        return _download_ytdlp(log)
    except DownloadError as exc:
        if log:
            safe_log(log, f"yt-dlp 업데이트 실패, 기존 파일을 계속 사용합니다: {exc}")
        return [str(YTDLP_EXE)]


def download_media(
    item: MediaItem,
    download_dir: str,
    quality: str,
    date_basis: str,
    prefer_ytdlp: bool = True,
    log: LogCallback = None,
    should_stop: StopCallback = None,
    force: bool = False,
) -> DownloadResult:
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    stream_url = choose_stream(item, quality)
    output_path = build_output_path(item, download_dir, date_basis)
    ffmpeg = ensure_ffmpeg(log=log)
    ffprobe = resolve_ffprobe(ffmpeg)
    youtube_source = is_youtube_url(stream_url)
    repaired = False

    if not force:
        record = find_record(item)
        if record:
            history_path = valid_record_path(record, lambda path: is_valid_media_file(path, ffprobe))
            if history_path:
                if log:
                    safe_log(log, f"다운로드 이력에 정상 파일이 있어 건너뜁니다: {history_path}")
                return DownloadResult(history_path, skipped=True, history_skipped=True)

    if output_path.exists():
        if force:
            if log:
                safe_log(log, f"재다운로드 요청으로 기존 파일을 삭제합니다: {output_path}")
            output_path.unlink()
            repaired = True
        elif is_valid_media_file(output_path, ffprobe):
            if log:
                safe_log(log, f"이미 정상 파일이 있어 건너뜁니다: {output_path}")
            record_download(item, output_path, quality, "건너뜀")
            return DownloadResult(output_path, skipped=True)
        else:
            if log:
                safe_log(log, f"기존 파일이 손상되어 삭제 후 다시 다운로드합니다: {output_path}")
            output_path.unlink()
            repaired = True

    cleanup_stale_download_files(output_path)

    errors = []
    if prefer_ytdlp or youtube_source:
        ytdlp_cmd = resolve_ytdlp(log=log)
        if ytdlp_cmd:
            try:
                if log:
                    safe_log(log, "yt-dlp로 다운로드 시도")
                _download_with_ytdlp(ytdlp_cmd, stream_url, output_path, item, ffmpeg, log, should_stop)
                verify_downloaded_file(output_path, ffprobe)
                record_download(item, output_path, quality, "완료")
                return DownloadResult(output_path, repaired=repaired)
            except DownloadError as exc:
                if isinstance(exc, DownloadCancelled):
                    raise
                errors.append(str(exc))
                if output_path.exists() and not is_valid_media_file(output_path, ffprobe):
                    if log:
                        safe_log(log, "yt-dlp 결과 파일이 손상되어 삭제합니다.")
                    output_path.unlink()
        elif log:
            safe_log(log, "yt-dlp를 찾지 못해 ffmpeg로 진행합니다.")

    if youtube_source:
        raise DownloadError("유튜브 원본 영상은 yt-dlp가 필요합니다.")

    if ffmpeg:
        for attempt in range(1, 3):
            try:
                if log:
                    suffix = "" if attempt == 1 else " (손상 파일 재시도)"
                    safe_log(log, f"ffmpeg로 다운로드 시도{suffix}")
                _download_with_ffmpeg(ffmpeg, stream_url, output_path, item, log, should_stop)
                verify_downloaded_file(output_path, ffprobe)
                record_download(item, output_path, quality, "완료")
                return DownloadResult(output_path, repaired=repaired)
            except DownloadError as exc:
                if isinstance(exc, DownloadCancelled):
                    raise
                message = str(exc)
                if attempt == 1 and ("손상" in message or "확인할 수 없습니다" in message):
                    if output_path.exists():
                        output_path.unlink()
                    cleanup_stale_download_files(output_path)
                    if log:
                        safe_log(log, "[재시도 2/2] 파일 손상으로 다시 다운로드합니다.")
                    continue
                errors.append(message)
                break

    raise DownloadError("; ".join(errors))


def _download_with_ytdlp(
    ytdlp_cmd: Sequence[str],
    stream_url: str,
    output_path: Path,
    item: MediaItem,
    ffmpeg: str,
    log: LogCallback,
    should_stop: StopCallback = None,
) -> None:
    cmd = [
        *ytdlp_cmd,
        "--force-overwrites",
        "--referer",
        item.play_url or item.source_page or "https://tvcf.co.kr/",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "--add-header",
        "Origin:https://tvcf.co.kr",
        "--ffmpeg-location",
        str(Path(ffmpeg).parent),
        "--merge-output-format",
        "mp4",
        "-o",
        str(output_path),
        stream_url,
    ]
    _run_command(cmd, log, should_stop)


def _download_with_ffmpeg(
    ffmpeg: str,
    stream_url: str,
    output_path: Path,
    item: MediaItem,
    log: LogCallback,
    should_stop: StopCallback = None,
) -> None:
    temp_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
    legacy_temp_path = output_path.with_suffix(output_path.suffix + ".part")
    for stale_path in (temp_path, legacy_temp_path):
        if stale_path.exists():
            stale_path.unlink()

    headers = (
        f"Referer: {item.play_url or item.source_page or 'https://tvcf.co.kr/'}\r\n"
        "Origin: https://tvcf.co.kr\r\n"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "-headers",
        headers,
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "4xx,5xx",
        "-reconnect_delay_max",
        "8",
        "-rw_timeout",
        "15000000",
        "-i",
        stream_url,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-f",
        "mp4",
        str(temp_path),
    ]
    _run_command(cmd, log, should_stop)
    temp_path.replace(output_path)


def _run_command(cmd: Sequence[str], log: LogCallback, should_stop: StopCallback = None) -> None:
    if should_stop and should_stop():
        raise DownloadCancelled("사용자 중단 요청으로 다운로드 명령을 시작하지 않았습니다.")

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=subprocess_env(),
        creationflags=creationflags,
    )

    assert process.stdout is not None
    last_progress_log = 0.0
    pending_progress_line = ""
    for raw_line in process.stdout:
        if should_stop and should_stop():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise DownloadCancelled("사용자 중단 요청으로 다운로드 명령을 종료했습니다.")

        line = decode_output(raw_line).strip()
        if line and log:
            if _is_progress_log_line(line):
                now = time.monotonic()
                if now - last_progress_log < 0.7:
                    pending_progress_line = line
                    continue
                last_progress_log = now
                pending_progress_line = ""
            safe_log(log, line)

    code = process.wait()
    if code == 0 and pending_progress_line and log:
        safe_log(log, pending_progress_line)
    if code != 0:
        raise DownloadError(f"명령 실행 실패(exit {code})")


def _is_progress_log_line(line: str) -> bool:
    return (
        line.startswith("[download]")
        or line.startswith("frame=")
        or " speed=" in line
        or " ETA " in line
    )


def safe_log(log: Callable[[str], None], message: str) -> None:
    text = decode_output(message)
    try:
        log(text)
    except UnicodeEncodeError:
        log(text.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))


def is_youtube_url(url: str) -> bool:
    return "youtube.com/" in url or "youtu.be/" in url


def _download_ytdlp(log: LogCallback = None) -> Sequence[str]:
    YTDLP_EXE.parent.mkdir(parents=True, exist_ok=True)
    if log:
        safe_log(log, "yt-dlp가 없어 자동으로 다운로드합니다.")

    temp_path = YTDLP_EXE.with_suffix(".exe.tmp")
    request = Request(YTDLP_DOWNLOAD_URL, headers={"User-Agent": "TVCF-Downloader"})
    try:
        with urlopen(request, timeout=60) as response, temp_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        temp_path.replace(YTDLP_EXE)
    except Exception as exc:  # noqa: BLE001 - surface a clear downloader error.
        if temp_path.exists():
            temp_path.unlink()
        raise DownloadError(f"yt-dlp 자동 다운로드 실패: {exc}") from exc

    version = ytdlp_version([str(YTDLP_EXE)])
    YTDLP_MANIFEST_PATH.write_text(
        json.dumps(
            {
                "source": "yt-dlp/yt-dlp",
                "download_url": YTDLP_DOWNLOAD_URL,
                "installed_at": datetime.now().isoformat(timespec="seconds"),
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "version": version,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if log:
        safe_log(log, f"yt-dlp 설치 완료: {version or YTDLP_EXE}")
    return [str(YTDLP_EXE)]


def ytdlp_version(cmd: Sequence[str]) -> str:
    try:
        process = subprocess.run(
            [*cmd, "--version"],
            capture_output=True,
            env=subprocess_env(),
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return decode_output(process.stdout).strip()


def _manifest_is_fresh(path: Path, max_age_days: int) -> bool:
    if max_age_days <= 0 or not path.exists():
        return False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        checked_at = manifest.get("checked_at") or manifest.get("installed_at")
        checked = datetime.fromisoformat(checked_at)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return datetime.now() - checked < timedelta(days=max_age_days)
