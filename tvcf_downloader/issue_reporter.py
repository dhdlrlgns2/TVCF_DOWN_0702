import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

from .config import load_config
from .diagnostics import read_error_case


ISSUES_API_URL = "https://api.github.com/repos/dhdlrlgns2/TVCF_DOWN_0702/issues"
BODY_LIMIT = 60000


@dataclass
class IssueReportResult:
    created: bool
    url: str
    message: str


def report_error_cases(error_paths: Iterable[Path], run_summary: str = "") -> IssueReportResult:
    paths = [Path(path) for path in error_paths]
    if not paths:
        raise ValueError("신고할 오류 로그가 없습니다.")

    cases = [read_error_case(path) for path in paths]
    title = _build_title(cases)
    body = _build_body(cases, run_summary)

    token = _github_token()
    if not token:
        raise RuntimeError("GitHub 이슈 토큰이 없습니다. config.json의 github_issue_token 또는 TVCF_GITHUB_TOKEN을 설정해주세요.")

    url = _create_issue(title, body, token)
    return IssueReportResult(True, url, f"GitHub 이슈를 생성했습니다: {url}")


def _create_issue(title: str, body: str, token: str) -> str:
    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "labels": ["download-error"],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        ISSUES_API_URL,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "TVCF-Downloader",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result.get("html_url", "")


def _build_title(cases: list[dict]) -> str:
    first = cases[0]
    category = first.get("category", "다운로드 오류")
    if len(cases) == 1:
        item = first.get("item", {})
        title = item.get("display_title") or item.get("nidx") or item.get("idx") or "알 수 없는 영상"
        return f"[TVCF 오류] {category} - {title}"[:250]
    return f"[TVCF 오류 묶음] {category} 외 {len(cases) - 1}건"[:250]


def _build_body(cases: list[dict], run_summary: str) -> str:
    payload = {
        "run_summary": run_summary,
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
        },
        "error_count": len(cases),
        "cases": cases,
    }
    body = "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"
    if len(body) > BODY_LIMIT:
        compact_body = "```json\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n```"
        if len(compact_body) <= BODY_LIMIT:
            return compact_body
        return compact_body[: BODY_LIMIT - 80] + "\n\n...truncated by TVCF downloader\n```"
    return body


def _github_token() -> str:
    token = os.environ.get("TVCF_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    config = load_config()
    value = config.get("github_issue_token", "")
    return str(value).strip()
