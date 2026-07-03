from .downloader import YTDLP_EXE, ensure_ytdlp
from .ffmpeg_manager import FFMPEG_EXE, FFPROBE_EXE, ensure_ffmpeg


def check_environment() -> int:
    print("[CHECK] Checking bundled tools...")
    try:
        ensure_ffmpeg(log=print, check_latest=True)
    except Exception as exc:  # noqa: BLE001 - show setup failure in launcher.
        print(f"[ERROR] Failed to prepare ffmpeg: {exc}")
        return 1

    if not FFMPEG_EXE.exists():
        print("[ERROR] ffmpeg.exe is missing.")
        return 1

    if not FFPROBE_EXE.exists():
        print("[ERROR] ffprobe.exe is missing.")
        return 1

    try:
        ensure_ytdlp(log=print)
    except Exception as exc:  # noqa: BLE001 - show setup failure in launcher.
        print(f"[ERROR] Failed to prepare yt-dlp: {exc}")
        return 1

    if not YTDLP_EXE.exists():
        print("[ERROR] yt-dlp.exe is missing.")
        return 1

    print("[CHECK] Tool check complete.")
    return 0


def main() -> None:
    raise SystemExit(check_environment())


if __name__ == "__main__":
    main()
