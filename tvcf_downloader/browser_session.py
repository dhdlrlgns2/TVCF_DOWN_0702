from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from .config import PROJECT_ROOT


LogCallback = Optional[Callable[[str], None]]
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", str(PROJECT_ROOT))) / "TVCF_DOWN_0702" / "ChromeProfile"
NETWORK_LOG_DIR = PROJECT_ROOT / "logs" / "browser_network"
BLOCK_MARKERS = (
    "자동화된 브라우저에서의 접속은 허용되지 않습니다",
    "Automated browser access is not allowed",
    "비정상적인 접근이 감지되었습니다",
)
SENSITIVE_QUERY_MARKERS = (
    "token",
    "auth",
    "key",
    "secret",
    "sig",
    "signature",
    "hash",
    "credential",
    "session",
    "jwt",
    "policy",
    "expires",
    "hdntl",
    "hdnea",
    "acl",
    "access",
)


class BrowserSessionError(RuntimeError):
    pass


class NetworkRecorder:
    def __init__(self, page, session_id: str) -> None:
        self.session_id = session_id
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._by_request_id: dict[str, dict[str, Any]] = {}
        self._cdp_sessions: list[Any] = []
        self.attach_page(page)

    def attach_page(self, page) -> None:
        cdp = page.context.new_cdp_session(page)
        cdp.on("Network.requestWillBeSent", self._on_request)
        cdp.on("Network.responseReceived", self._on_response)
        cdp.on("Network.loadingFinished", self._on_finished)
        cdp.on("Network.loadingFailed", self._on_failed)
        cdp.send("Network.enable")
        self._cdp_sessions.append(cdp)

    def close(self) -> None:
        for cdp in self._cdp_sessions:
            try:
                cdp.detach()
            except Exception:
                pass
        self._cdp_sessions.clear()

    def save_session(self) -> Path:
        NETWORK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = NETWORK_LOG_DIR / f"{self.session_id}_session.json"
        with self._lock:
            payload = {
                "session_id": self.session_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "purpose": "TVCF list metadata collection",
                "requests": [dict(record) for record in self._records],
            }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _on_request(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId", ""))
        request = event.get("request", {})
        record = {
            "request_id": request_id,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "url": sanitize_url(str(request.get("url", ""))),
            "method": str(request.get("method", "")),
            "resource_type": str(event.get("type", "")),
            "status": None,
            "content_type": "",
            "response_size": 0,
            "failure": "",
        }
        with self._lock:
            self._records.append(record)
            if request_id:
                self._by_request_id[request_id] = record

    def _on_response(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId", ""))
        response = event.get("response", {})
        headers = {str(key).lower(): str(value) for key, value in response.get("headers", {}).items()}
        with self._lock:
            record = self._by_request_id.get(request_id)
            if not record:
                return
            record["status"] = int(response.get("status", 0) or 0)
            record["content_type"] = str(response.get("mimeType", "") or headers.get("content-type", ""))
            content_length = headers.get("content-length", "")
            if content_length.isdigit():
                record["response_size"] = int(content_length)

    def _on_finished(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId", ""))
        with self._lock:
            record = self._by_request_id.get(request_id)
            if record:
                record["response_size"] = int(event.get("encodedDataLength", 0) or record["response_size"])

    def _on_failed(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId", ""))
        reason = str(event.get("errorText", ""))
        blocked = str(event.get("blockedReason", ""))
        if blocked:
            reason = f"{reason} / blockedReason={blocked}".strip(" /")
        with self._lock:
            record = self._by_request_id.get(request_id)
            if record:
                record["failure"] = reason


class BrowserSession:
    def __init__(self, profile_dir: Path = PROFILE_DIR) -> None:
        self.profile_dir = Path(profile_dir)
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.process: subprocess.Popen | None = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.recorder: NetworkRecorder | None = None

    def start(self, log: LogCallback = None) -> None:
        if self.page:
            return
        executable = find_chromium_executable()
        if not executable:
            raise BrowserSessionError("Chrome 또는 Chromium 기반 브라우저를 찾지 못했습니다.")

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port = available_port()
        args = [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "about:blank",
        ]
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        endpoint = f"http://127.0.0.1:{port}"
        wait_for_debug_endpoint(endpoint, self.process)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self.close()
            raise BrowserSessionError("Playwright가 설치되어 있지 않아 Chrome CDP에 연결할 수 없습니다.") from exc

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
        self.context = self.browser.contexts[0] if self.browser.contexts else None
        if not self.context:
            self.close()
            raise BrowserSessionError("Chrome 사용자 프로필 컨텍스트를 열지 못했습니다.")
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15000)
        self.recorder = NetworkRecorder(self.page, self.session_id)
        self.context.on("page", self._on_new_page)
        if log:
            log(f"TVCF 목록 확인용 Chrome 프로필 연결: {self.profile_dir}")

    def close(self) -> None:
        recorder = self.recorder
        self.recorder = None
        if recorder:
            try:
                recorder.save_session()
            except OSError:
                pass
            recorder.close()
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self.process = None

    def get_html(self, url: str, log: LogCallback = None, wait_seconds: int = 180) -> str:
        self.start(log=log)
        self._navigate(url, log=log)
        self.page.wait_for_timeout(1000)
        deadline = time.monotonic() + wait_seconds
        notified = False
        while True:
            html = self.page.content()
            if not contains_block_page(html):
                return html
            if not notified and log:
                log("Chrome에 TVCF 확인 화면이 표시되었습니다. 열린 창에서 안내를 직접 완료해주세요.")
                notified = True
            if time.monotonic() >= deadline:
                raise BrowserSessionError("Chrome 창에서도 TVCF 목록 페이지 확인이 완료되지 않았습니다.")
            self.page.wait_for_timeout(1000)

    def _navigate(self, url: str, log: LogCallback = None) -> None:
        if log:
            log(f"Chrome 목록 페이지 열기: {sanitize_url(url)}")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            if log:
                log(f"Chrome 페이지 로드 대기 종료: {sanitize_text(str(exc))}")

    def _on_new_page(self, page) -> None:
        if self.recorder:
            try:
                self.recorder.attach_page(page)
            except Exception:
                pass


def find_chromium_executable() -> Path | None:
    for name in ("chrome.exe", "msedge.exe", "chromium.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)

    roots = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
    ]
    relatives = (
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Chromium/Application/chrome.exe"),
    )
    for root in roots:
        if root:
            for relative in relatives:
                candidate = Path(root) / relative
                if candidate.is_file():
                    return candidate
    return None


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_debug_endpoint(endpoint: str, process: subprocess.Popen, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserSessionError(
                "Chrome 사용자 프로필을 열지 못했습니다. 앱 전용 Chrome 창이 열려 있다면 닫고 다시 시도해주세요."
            )
        try:
            with urlopen(f"{endpoint}/json/version", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("webSocketDebuggerUrl"):
                return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise BrowserSessionError(f"Chrome DevTools 연결 시간이 초과되었습니다: {last_error}")


def sanitize_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if any(marker in key.lower() for marker in SENSITIVE_QUERY_MARKERS):
                value = "[redacted]"
            query.append((key, value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except Exception:
        return url.split("#", 1)[0]


def sanitize_text(value: str) -> str:
    return re.sub(r"https?://[^\s'\"<>]+", lambda match: sanitize_url(match.group(0)), value)


def contains_block_page(html: str) -> bool:
    return any(marker in html for marker in BLOCK_MARKERS)
