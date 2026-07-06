import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .text_utils import decode_output, subprocess_env


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/dhdlrlgns2/TVCF_DOWN_0702.git"
BRANCH = "main"
CORE_FILES = (
    "main.py",
    "requirements.txt",
    "scripts/run_after_update.bat",
    "tvcf_downloader/gui.py",
    "tvcf_downloader/client.py",
)


def run_git(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        env=subprocess_env(),
    )
    completed = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        decode_output(result.stdout),
        decode_output(result.stderr),
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            completed.args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def run_git_global(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env=subprocess_env(),
    )
    completed = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        decode_output(result.stdout),
        decode_output(result.stderr),
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            completed.args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            env=subprocess_env(),
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


def ensure_origin() -> None:
    result = run_git(["remote", "get-url", "origin"])
    if result.returncode == 0:
        current = result.stdout.strip()
        if current != REPO_URL:
            run_git(["remote", "set-url", "origin", REPO_URL])
        return
    run_git(["remote", "add", "origin", REPO_URL])


def bootstrap_git_checkout() -> int:
    print("[UPDATE] This folder is not a git checkout. Preparing repository...")
    init = run_git_global(["init", "-b", BRANCH])
    if init.returncode != 0:
        print("[UPDATE] Git init failed. Starting current files.")
        if init.stderr.strip():
            print(init.stderr.strip())
        return 0

    ensure_origin()
    fetch = run_git(["fetch", "--quiet", "origin", BRANCH])
    if fetch.returncode != 0:
        print("[UPDATE] Fetch failed. Starting current files.")
        if fetch.stderr.strip():
            print(fetch.stderr.strip())
        return 0

    backup_tracked_files()
    checkout = run_git(["checkout", "-B", BRANCH, f"origin/{BRANCH}", "--force"])
    if checkout.returncode != 0:
        print("[UPDATE] Checkout failed. Starting current files.")
        if checkout.stderr.strip():
            print(checkout.stderr.strip())
        return 0

    run_git(["branch", "--set-upstream-to", f"origin/{BRANCH}", BRANCH])
    print("[UPDATE] Repository prepared from remote.")
    return 2


def backup_tracked_files() -> None:
    result = run_git(["ls-tree", "-r", "--name-only", f"origin/{BRANCH}"])
    if result.returncode != 0:
        return

    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    existing_files = [PROJECT_ROOT / name for name in files if (PROJECT_ROOT / name).is_file()]
    if not existing_files:
        return

    backup_root = PROJECT_ROOT / ".update_backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
    for source in existing_files:
        destination = backup_root / source.relative_to(PROJECT_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


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


def current_head() -> str:
    result = run_git(["rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


def core_files_ok() -> bool:
    return all((PROJECT_ROOT / name).is_file() for name in CORE_FILES)


def rollback_to(head: str) -> bool:
    if not head:
        return False
    print(f"[UPDATE] Rolling back to previous version {head[:12]}...")
    reset = run_git(["reset", "--hard", head])
    if reset.returncode != 0:
        print("[UPDATE] Rollback failed.")
        if reset.stderr.strip():
            print(reset.stderr.strip())
        return False
    print("[UPDATE] Rollback complete. Starting previous version.")
    return True


def check_and_update() -> int:
    if not git_available():
        print("[UPDATE] Git is not installed. Skipping update check.")
        return 0

    if not is_git_checkout():
        return bootstrap_git_checkout()

    ensure_origin()

    upstream = upstream_ref()
    if not upstream:
        run_git(["fetch", "--quiet", "origin", BRANCH])
        run_git(["branch", "--set-upstream-to", f"origin/{BRANCH}", BRANCH])
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

    previous_head = current_head()
    if previous_head:
        print(f"[UPDATE] Current version: {previous_head[:12]}")

    print(f"[UPDATE] Applying {behind} update(s)...")
    pull = run_git(["pull", "--ff-only"])
    if pull.returncode != 0:
        print("[UPDATE] Pull failed. Starting current version.")
        if pull.stderr.strip():
            print(pull.stderr.strip())
        return 0

    if not core_files_ok():
        print("[UPDATE] Update completed but required files are missing.")
        if rollback_to(previous_head):
            return 0
        print("[UPDATE] Required files are missing and rollback failed.")
        return 1

    print("[UPDATE] Update complete.")
    if pull.stdout.strip():
        print(pull.stdout.strip())
    return 2


def main() -> None:
    raise SystemExit(check_and_update())


if __name__ == "__main__":
    main()
