import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_git(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def is_git_checkout() -> bool:
    if not (PROJECT_ROOT / ".git").exists():
        return False
    result = run_git(["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def has_tracked_local_changes() -> bool:
    run_git(["update-index", "-q", "--refresh"])
    result = run_git(["diff-index", "--quiet", "HEAD", "--"])
    return result.returncode != 0


def upstream_ref() -> str:
    result = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def ahead_behind(upstream: str) -> tuple[int, int]:
    result = run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    if result.returncode != 0:
        return 0, 0
    left, right = result.stdout.strip().split()
    return int(left), int(right)


def check_and_update() -> int:
    if not git_available():
        print("[UPDATE] Git is not installed. Skipping update check.")
        return 0

    if not is_git_checkout():
        print("[UPDATE] This folder is not a git checkout. Skipping update check.")
        return 0

    upstream = upstream_ref()
    if not upstream:
        print("[UPDATE] No upstream branch is configured. Skipping update check.")
        return 0

    if has_tracked_local_changes():
        print("[UPDATE] Local code changes detected. Skipping auto update.")
        return 0

    print("[UPDATE] Checking git updates...")
    fetch = run_git(["fetch", "--quiet", "--prune"])
    if fetch.returncode != 0:
        print("[UPDATE] Fetch failed. Starting current version.")
        if fetch.stderr.strip():
            print(fetch.stderr.strip())
        return 0

    ahead, behind = ahead_behind(upstream)
    if behind <= 0:
        print("[UPDATE] Already up to date.")
        return 0

    if ahead > 0:
        print("[UPDATE] Local branch has commits not on upstream. Skipping auto update.")
        return 0

    print(f"[UPDATE] Applying {behind} update(s)...")
    pull = run_git(["pull", "--ff-only"])
    if pull.returncode != 0:
        print("[UPDATE] Pull failed. Starting current version.")
        if pull.stderr.strip():
            print(pull.stderr.strip())
        return 0

    print("[UPDATE] Update complete.")
    if pull.stdout.strip():
        print(pull.stdout.strip())
    return 2


def main() -> None:
    raise SystemExit(check_and_update())


if __name__ == "__main__":
    main()
