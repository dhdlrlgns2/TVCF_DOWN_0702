import json
import re
import time
from datetime import date
from http.client import IncompleteRead
from html import unescape
from typing import Any, Callable, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

try:
    import requests
    from requests.exceptions import HTTPError as RequestsHTTPError
    from requests.exceptions import RequestException as RequestsRequestException
    from requests.exceptions import Timeout as RequestsTimeout
except ImportError:  # pragma: no cover - urllib fallback keeps bootstrap usable before pip install.
    requests = None
    RequestsHTTPError = None
    RequestsRequestException = None
    RequestsTimeout = None


from .browser_session import BrowserSession
from .models import MediaItem, parse_tvcf_date
from .text_utils import decode_output


LogCallback = Optional[Callable[[str], None]]
StopCallback = Optional[Callable[[], bool]]
NETWORK_EXCEPTIONS = (HTTPError, IncompleteRead, TimeoutError, URLError, OSError) + (
    (RequestsRequestException,) if RequestsRequestException else ()
)
LIST_PAGE_ROWS = 200


class TVCFError(RuntimeError):
    pass


class TVCFClient:
    BASE_URL = "https://tvcf.co.kr"

    _record_re = re.compile(r'\{\\"idx\\":\\"(?P<idx>\d+)\\".*?\\"_score\\":(?:null|\[[^\]]*\])\}', re.S)
    _automation_block_markers = (
        "자동화된 브라우저에서의 접속은 허용되지 않습니다",
        "Automated browser access is not allowed",
        "비정상적인 접근이 감지되었습니다",
    )

    def __init__(self, timeout: int = 25, delay: float = 0.0) -> None:
        self.timeout = timeout
        self.delay = delay
        self._browser_session: BrowserSession | None = None
        self._browser_mode_announced = False
        self.headers = {
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
        }
        self._session = requests.Session() if requests else None
        if self._session:
            self._session.headers.update(self.headers)

    def close(self) -> None:
        session = self._session
        browser_session = self._browser_session
        self._session = None
        self._browser_session = None
        if session:
            session.close()
        if browser_session:
            browser_session.close()

    @property
    def browser_session(self) -> BrowserSession:
        if not self._browser_session:
            self._browser_session = BrowserSession()
        return self._browser_session

    def __enter__(self) -> "TVCFClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def list_page(
        self,
        page: int,
        rows: int = LIST_PAGE_ROWS,
        sort_by: str = "registrated_date",
        log: LogCallback = None,
    ) -> List[MediaItem]:
        params = {
            "country_code_value": "410",
            "lang": "ko",
            "mediaType_value": "1",
            "page": str(page),
            "rows": str(rows),
            "sort_by": sort_by,
        }
        html = self._get_text(f"{self.BASE_URL}/worked/video", params=params, log=log)
        return self.parse_list_page(html)

    def collect_period(
        self,
        start_date,
        end_date,
        date_basis: str,
        max_pages: int,
        log: LogCallback = None,
        should_stop: StopCallback = None,
    ) -> List[MediaItem]:
        items: List[MediaItem] = []
        seen = set()
        newest_seen = None
        sort_by = "published_date" if date_basis == "published" else "registrated_date"

        page = 1
        while max_pages <= 0 or page <= max_pages:
            if should_stop and should_stop():
                break

            if log:
                log(f"목록 {page}페이지 확인 중")

            page_items = self.list_page(page, sort_by=sort_by, log=log)
            if not page_items:
                if log:
                    log("목록에서 더 이상 항목을 찾지 못했습니다.")
                break

            candidate_dates = []
            for item in page_items:
                item_date = item.date_value(date_basis)

                key = item.nidx or item.idx or item.mcode
                if not key or key in seen:
                    continue

                if item.country_code != "410" or item.category_code != "1":
                    continue

                if item_date:
                    candidate_dates.append(item_date)
                    if newest_seen is None or item_date > newest_seen:
                        newest_seen = item_date

                if item_date and start_date <= item_date <= end_date:
                    items.append(item)
                    seen.add(key)

            if log and candidate_dates:
                log(
                    f"{page}페이지 한국 광고 날짜 범위({date_basis}): "
                    f"{min(candidate_dates)} ~ {max(candidate_dates)}"
                )

            if candidate_dates and max(candidate_dates) < start_date:
                if log:
                    log(
                        "선택한 시작일보다 오래된 페이지에 도달해 목록 탐색을 멈췄습니다. "
                        f"현재 확인된 최신 {date_basis} 날짜: {newest_seen}"
                    )
                break

            if self.delay > 0:
                time.sleep(self.delay)
            page += 1

        if not items and log:
            if newest_seen:
                log(
                    "조건에 맞는 한국 광고가 없습니다. "
                    f"현재 목록에서 확인한 최신 {date_basis} 날짜는 {newest_seen}입니다."
                )
            else:
                log("조건에 맞는 한국 광고를 찾지 못했습니다.")

        return sorted(
            items,
            key=lambda item: (
                item.date_value(date_basis) is None,
                item.date_value(date_basis) or date.max,
                item.display_title,
                item.nidx or item.idx or item.mcode,
            ),
        )

    def get_media(
        self,
        identifier: str,
        use_playwright_fallback: bool = True,
        log: LogCallback = None,
    ) -> MediaItem:
        raise TVCFError(
            "TVCF 상세 영상 조회는 사용하지 않습니다. "
            "기간 목록의 제목과 메타데이터를 YouTube 검색 경로에 전달해주세요."
        )

    def parse_list_page(self, html: str) -> List[MediaItem]:
        structured_items = self._items_from_next_flight(html)
        if structured_items:
            return structured_items

        items: List[MediaItem] = []
        seen = set()

        for match in self._record_re.finditer(html):
            block = match.group(0)
            item = self._item_from_list_block(block)
            key = item.nidx or item.idx or item.mcode
            if key and key not in seen:
                items.append(item)
                seen.add(key)

        return items

    def _items_from_next_flight(self, html: str) -> List[MediaItem]:
        items: List[MediaItem] = []
        seen = set()
        for record in self._list_records_from_next_flight(html):
            item = self._item_from_list_record(record)
            key = item.nidx or item.idx or item.mcode
            if key and key not in seen:
                items.append(item)
                seen.add(key)
        return items

    def _list_records_from_next_flight(self, html: str) -> Iterable[dict[str, Any]]:
        flight_text = "".join(self._next_flight_strings(html))
        if not flight_text:
            return []

        records: list[dict[str, Any]] = []
        for match in re.finditer(r'"results"\s*:\s*\[', flight_text):
            start = flight_text.find("[", match.start())
            array_text = self._balanced_json_slice(flight_text, start, "[", "]")
            if not array_text:
                continue
            try:
                value = json.loads(array_text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, list):
                records.extend(record for record in value if isinstance(record, dict))
        return records

    @classmethod
    def _next_flight_strings(cls, html: str) -> Iterable[str]:
        marker = "self.__next_f.push("
        position = 0
        while True:
            start = html.find(marker, position)
            if start < 0:
                break
            argument_start = start + len(marker)
            argument, position = cls._next_call_argument(html, argument_start)
            if not argument:
                break
            try:
                payload = json.loads(argument)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], str):
                yield payload[1]

    @staticmethod
    def _next_call_argument(text: str, start: int) -> tuple[str, int]:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            elif char == ")" and depth == 0:
                return text[start:index], index + 1
        return "", len(text)

    @staticmethod
    def _balanced_json_slice(text: str, start: int, open_char: str, close_char: str) -> str:
        if start < 0 or start >= len(text) or text[start] != open_char:
            return ""

        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return ""

    def _get_text(self, url: str, params: Optional[dict] = None, log: LogCallback = None) -> str:
        if url.startswith(self.BASE_URL):
            if log and not self._browser_mode_announced:
                log("TVCF 페이지는 사용자 Chrome 프로필과 CDP 연결로 확인합니다.")
                self._browser_mode_announced = True
            return self._get_text_with_browser(url, params=params, log=log)

        if params and not self._session:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"

        last_error: Exception | None = None
        attempt = 1
        max_attempts = 3
        while attempt <= max_attempts:
            try:
                if self._session:
                    return self._get_text_with_session(url, params=params)
                return self._get_text_with_urllib(url)
            except TVCFError as exc:
                if "자동 목록 접속을 차단" not in str(exc):
                    raise
                if log:
                    log("TVCF 일반 HTTP 접속이 차단되어 사용자 Chrome 세션으로 전환합니다.")
                return self._get_text_with_browser(url, params=params, log=log)
            except NETWORK_EXCEPTIONS as exc:
                last_error = exc
                max_attempts, wait_seconds, reason = self._retry_policy(exc)
                if attempt < max_attempts:
                    if log:
                        log(f"[재시도 {attempt + 1}/{max_attempts}] {reason}로 다시 시도합니다.")
                    time.sleep(wait_seconds * attempt)
                attempt += 1

        if last_error:
            raise TVCFError(
                f"페이지를 읽지 못했습니다: {url} / "
                f"원인={self._retry_reason(last_error)} / 재시도={max(0, attempt - 2)}회 / {last_error}"
            ) from last_error
        raise TVCFError(f"페이지를 읽지 못했습니다: {url}")

    def _get_text_with_browser(self, url: str, params: Optional[dict], log: LogCallback) -> str:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        return self.browser_session.get_html(url, log=log)

    def _get_text_with_session(self, url: str, params: Optional[dict] = None) -> str:
        if not self._session:
            raise TVCFError("HTTP 세션이 초기화되지 않았습니다.")
        response = self._session.get(url, params=params, timeout=self.timeout)
        self._raise_if_automation_blocked(response.status_code, response.text)
        response.raise_for_status()
        if not response.encoding:
            response.encoding = "utf-8"
        return response.text

    def _get_text_with_urllib(self, url: str) -> str:
        request = Request(url, headers=self.headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                try:
                    raw = response.read()
                except IncompleteRead as exc:
                    raw = exc.partial
                    if not raw or len(raw) < 2048:
                        raise
                content_type = response.headers.get("Content-Type", "")
                encoding = "utf-8"
                match = re.search(r"charset=([\w-]+)", content_type, re.I)
                if match:
                    encoding = match.group(1)
                return decode_output(raw, preferred=encoding)
        except HTTPError as exc:
            if exc.code == 403:
                body = decode_output(exc.read(), preferred="utf-8")
                self._raise_if_automation_blocked(exc.code, body)
            raise

    @classmethod
    def _raise_if_automation_blocked(cls, status_code: int, body: str) -> None:
        if status_code != 403:
            return
        if any(marker in body for marker in cls._automation_block_markers):
            raise TVCFError(
                "HTTP 403: TVCF가 프로그램의 자동 목록 접속을 차단했습니다. "
                "일반 브라우저 접속만 허용한다는 서버 응답이므로 같은 요청을 재시도하지 않습니다."
            )

    @staticmethod
    def _retry_policy(exc: Exception) -> tuple[int, float, str]:
        if RequestsHTTPError and isinstance(exc, RequestsHTTPError):
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None)
            if code:
                return TVCFClient._http_retry_policy(int(code))
        if isinstance(exc, HTTPError):
            return TVCFClient._http_retry_policy(exc.code)
        if RequestsTimeout and isinstance(exc, RequestsTimeout):
            return 3, 1.0, "timeout"
        if isinstance(exc, TimeoutError):
            return 3, 1.0, "timeout"
        if isinstance(exc, IncompleteRead):
            return 3, 1.0, "네트워크 오류"
        if RequestsRequestException and isinstance(exc, RequestsRequestException):
            return 3, 1.0, "네트워크 오류"
        if isinstance(exc, (URLError, OSError)):
            return 3, 1.0, "네트워크 오류"
        return 1, 0.0, "알 수 없는 오류"

    @staticmethod
    def _http_retry_policy(code: int) -> tuple[int, float, str]:
        if code == 404:
            return 1, 0.0, "HTTP 404"
        if code == 403:
            return 1, 0.0, "HTTP 403"
        if code == 429:
            return 3, 3.0, "HTTP 429"
        if 500 <= code <= 599:
            return 3, 1.2, f"HTTP {code}"
        return 2, 1.0, f"HTTP {code}"

    @classmethod
    def _retry_reason(cls, exc: Exception) -> str:
        return cls._retry_policy(exc)[2]

    def _item_from_list_block(self, block: str) -> MediaItem:
        category_code = self._array_first(block, "category_code")
        country_code = self._field(block, "country_code")
        nidx = self._field(block, "nidx")

        return MediaItem(
            idx=self._field(block, "idx"),
            nidx=nidx,
            mcode=self._field(block, "mcode"),
            title=self._field(block, "title"),
            chapter=self._field(block, "chapter"),
            brand=self._first_array_text(block, "brand"),
            published_date=self._field(block, "published_date"),
            registered_date=self._field(block, "registrated_date"),
            country_code=country_code,
            category_code=category_code,
            category_name=self._field(block, "category_code_name"),
            duration=self._float_field(block, "duration"),
            play_url=urljoin(self.BASE_URL, f"/play/{nidx}") if nidx else "",
            source_page=urljoin(self.BASE_URL, f"/play/{nidx}") if nidx else "",
        )

    def _item_from_list_record(self, record: dict[str, Any]) -> MediaItem:
        category_code = self._string_value(self._first_value(record.get("category_code")))
        country_code = self._string_value(record.get("country_code"))
        nidx = self._string_value(record.get("nidx"))

        return MediaItem(
            idx=self._string_value(record.get("idx")),
            nidx=nidx,
            mcode=self._string_value(record.get("mcode")),
            title=self._string_value(record.get("title")),
            chapter=self._string_value(record.get("chapter")),
            brand=self._string_value(self._first_value(record.get("brand"))),
            published_date=self._string_value(record.get("published_date")),
            registered_date=self._string_value(record.get("registrated_date")),
            country_code=country_code,
            category_code=category_code,
            category_name=self._string_value(record.get("category_code_name")),
            duration=self._float_value(record.get("duration")),
            play_url=urljoin(self.BASE_URL, f"/play/{nidx}") if nidx else "",
            source_page=urljoin(self.BASE_URL, f"/play/{nidx}") if nidx else "",
        )

    @staticmethod
    def _first_value(value: Any) -> Any:
        if isinstance(value, list):
            return value[0] if value else ""
        return value

    @staticmethod
    def _string_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    @staticmethod
    def _float_value(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _field(cls, block: str, name: str) -> str:
        pattern = re.compile(rf'\\"{re.escape(name)}\\":\\"((?:\\\\.|[^\\"])*)\\"')
        match = pattern.search(block)
        if not match:
            pattern = re.compile(rf'"{re.escape(name)}":"((?:\\.|[^"])*)"')
            match = pattern.search(block)
        if not match:
            return ""
        return cls._decode_js(match.group(1))

    @classmethod
    def _array_first(cls, block: str, name: str) -> str:
        match = re.search(rf'\\"{re.escape(name)}\\":\[(.*?)\]', block)
        if not match:
            match = re.search(rf'"{re.escape(name)}":\[(.*?)\]', block)
        if not match:
            return ""
        first = match.group(1).split(",", 1)[0].strip().strip('"\\')
        return first

    @classmethod
    def _first_array_text(cls, block: str, name: str) -> str:
        match = re.search(rf'\\"{re.escape(name)}\\":\[\\"((?:\\\\.|[^\\"])*)\\"', block)
        if not match:
            return ""
        return cls._decode_js(match.group(1))

    @staticmethod
    def _float_field(block: str, name: str) -> Optional[float]:
        match = re.search(rf'\\"{re.escape(name)}\\":([0-9.]+)', block)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _decode_js(value: str) -> str:
        try:
            return json.loads(f'"{value}"')
        except json.JSONDecodeError:
            return unescape(value.replace('\\"', '"').replace("\\/", "/"))


def filter_korean_ads(items: Iterable[MediaItem]) -> List[MediaItem]:
    return [
        item
        for item in items
        if item.country_code == "410" and (item.category_code == "1" or item.category_name == "광고")
    ]
