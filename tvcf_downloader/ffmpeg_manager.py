import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

from .config import PROJECT_ROOT
from .text_utils import decode_output, subprocess_env


LogCallback = Optional[Callable[[str], None]]

GITHUB_RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
BIN_DIR = PROJECT_ROOT / "bin"
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"
FFPROBE_EXE = BIN_DIR / "ffprobe.exe"
MANIFEST_PATH = BIN_DIR / "ffmpeg_manifest.json"
FFMPEG_UPDATE_DAYS = 7


class FFmpegInstallError(RuntimeError):
    pass


def ensure_ffmpeg(log: LogCallback = None, check_latest: bool = False, max_age_days: int = FFMPEG_UPDATE_DAYS) -> str:
    if FFMPEG_EXE.exists():
        version = ffmpeg_version(FFMPEG_EXE)
        if not check_latest or _manifest_is_fresh(MANIFEST_PATH, max_age_days):
            if log and version:
                log(f"ffmpeg 확인: {version}")
            return str(FFMPEG_EXE)

        try:
            asset = get_latest_windows_asset()
        except Exception as exc:  # noqa: BLE001 - keep existing ffmpeg if release lookup fails.
            if log:
                log(f"ffmpeg 최신 버전 확인 실패, 기존 파일을 계속 사용합니다: {exc}")
            return str(FFMPEG_EXE)

        manifest = _read_manifest()
        current_asset_name = manifest.get("asset", {}).get("name", "")
        if current_asset_name == asset.get("name"):
            _write_manifest(asset, version, checked_only=True)
            if log and version:
                log(f"ffmpeg 최신 상태: {version}")
            return str(FFMPEG_EXE)

        if log:
            log(f"ffmpeg 업데이트를 다운로드합니다: {asset['name']}")
        _install_ffmpeg_asset(asset, log=log)
        return str(FFMPEG_EXE)

    asset = get_latest_windows_asset()
    if log:
        log(f"ffmpeg가 없어 최신 빌드를 다운로드합니다: {asset['name']}")

    _install_ffmpeg_asset(asset, log=log)
    return str(FFMPEG_EXE)


def _install_ffmpeg_asset(asset: dict, log: LogCallback = None) -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tvcf_ffmpeg_") as temp_dir:
        temp_path = Path(temp_dir)
        zip_path = temp_path / asset["name"]
        _download_file(asset["browser_download_url"], zip_path, log=log)
        extracted = temp_path / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extracted)

        ffmpeg_source = _find_file(extracted, "ffmpeg.exe")
        ffprobe_source = _find_file(extracted, "ffprobe.exe")
        if not ffmpeg_source:
            raise FFmpegInstallError("압축 파일에서 ffmpeg.exe를 찾지 못했습니다.")

        shutil.copy2(ffmpeg_source, FFMPEG_EXE)
        if ffprobe_source:
            shutil.copy2(ffprobe_source, FFPROBE_EXE)

    version = ffmpeg_version(FFMPEG_EXE)
    manifest = {
        "source": "BtbN/FFmpeg-Builds",
        "release_api": GITHUB_RELEASE_API,
        "asset": asset,
        "version": version,
        "installed_at": datetime.now().isoformat(timespec="seconds"),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if log:
        log(f"ffmpeg 설치 완료: {version or FFMPEG_EXE}")


def get_latest_windows_asset() -> dict:
    request = Request(GITHUB_RELEASE_API, headers={"User-Agent": "TVCF-Downloader"})
    with urlopen(request, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))

    assets = release.get("assets", [])
    release_branch_assets = [
        asset
        for asset in assets
        if re.fullmatch(r"ffmpeg-n\d+(?:\.\d+)*-latest-win64-gpl-\d+(?:\.\d+)*\.zip", asset.get("name", ""))
    ]
    if release_branch_assets:
        release_branch_assets.sort(key=lambda asset: _version_tuple(asset["name"]), reverse=True)
        return _asset_summary(release, release_branch_assets[0])

    for asset in assets:
        if asset.get("name") == "ffmpeg-master-latest-win64-gpl.zip":
            return _asset_summary(release, asset)

    raise FFmpegInstallError("BtbN 최신 릴리스에서 Windows ffmpeg zip을 찾지 못했습니다.")


def ffmpeg_version(path: Path) -> str:
    try:
        process = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            env=subprocess_env(),
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    stdout = decode_output(process.stdout)
    return stdout.splitlines()[0].strip() if stdout else ""


def _download_file(url: str, destination: Path, log: LogCallback = None) -> None:
    request = Request(url, headers={"User-Agent": "TVCF-Downloader"})
    with urlopen(request, timeout=60) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        next_notice = 10
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            downloaded += len(chunk)
            if log and total:
                percent = int(downloaded * 100 / total)
                if percent >= next_notice:
                    log(f"ffmpeg 다운로드 {percent}%")
                    next_notice += 10


def _find_file(root: Path, filename: str) -> Optional[Path]:
    for path in root.rglob(filename):
        if path.is_file():
            return path
    return None


def _asset_summary(release: dict, asset: dict) -> dict:
    return {
        "release_name": release.get("name", ""),
        "published_at": release.get("published_at", ""),
        "name": asset.get("name", ""),
        "size": asset.get("size", 0),
        "updated_at": asset.get("updated_at", ""),
        "browser_download_url": asset.get("browser_download_url", ""),
    }


def _version_tuple(asset_name: str) -> tuple:
    match = re.search(r"ffmpeg-n(\d+(?:\.\d+)*)-latest", asset_name)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def _read_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(asset: dict, version: str, checked_only: bool = False) -> None:
    manifest = _read_manifest()
    if not checked_only:
        manifest["installed_at"] = datetime.now().isoformat(timespec="seconds")
    manifest.update(
        {
            "source": "BtbN/FFmpeg-Builds",
            "release_api": GITHUB_RELEASE_API,
            "asset": asset,
            "version": version,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


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
