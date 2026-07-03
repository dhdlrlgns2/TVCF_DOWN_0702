import json
import re
import time
from datetime import date
from http.client import IncompleteRead
from html import unescape
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.error import URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


from .models import MediaItem, parse_tvcf_date


LogCallback = Optional[Callable[[str], None]]
StopCallback = Optional[Callable[[], bool]]


class TVCFError(RuntimeError):
    pass


class TVCFClient:
    BASE_URL = "https://tvcf.co.kr"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    _record_re = re.compile(r'\{\\"idx\\":\\"(?P<idx>\d+)\\".*?\\"_score\\":(?:null|\[[^\]]*\])\}', re.S)
    _m3u8_re = re.compile(r"https?://[^\"\\]+?\.m3u8(?:\?[^\"\\]*)?")

    def __init__(self, timeout: int = 25, delay: float = 0.3) -> None:
        self.timeout = timeout
        self.delay = delay
        self.headers = {
            "User-Agent": self.USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.5",
        }

    def list_page(self, page: int, rows: int = 50, sort_by: str = "registrated_date") -> List[MediaItem]:
        params = {
            "country_code_value": "410",
            "lang": "ko",
            "mediaType_value": "1",
            "page": str(page),
            "rows": str(rows),
            "sort_by": sort_by,
        }
        html = self._get_text(f"{self.BASE_URL}/worked/video", params=params)
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

            page_items = self.list_page(page, sort_by=sort_by)
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
        errors: List[str] = []
        for url in self._candidate_urls(identifier):
            try:
                html = self._get_text(url)
                item = self.parse_play_page(html, url)
                if item.stream_urls:
                    return item

                if use_playwright_fallback:
                    streams = self.sniff_streams_with_playwright(url, log=log)
                    if streams:
                        item.stream_urls = streams
                        return item

                errors.append(f"{url}: 스트림 없음")
            except Exception as exc:  # noqa: BLE001 - keep trying candidate URLs.
                errors.append(f"{url}: {exc}")

        raise TVCFError("; ".join(errors) if errors else "미디어를 찾지 못했습니다.")

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

    def parse_play_page(self, html: str, page_url: str) -> MediaItem:
        initial = self._slice_after(html, r'\\"initialData\\":{', 40000)
        media = self._slice_after(html, r'\\"mediaData\\":{', 50000)
        scope = initial + media

        streams = self._extract_streams(scope or html)
        item = MediaItem(
            idx=self._field(initial, "idx") or self._field(media, "mIdx"),
            nidx=self._field(initial, "nidx") or self._slug_from_url(page_url),
            mcode=self._field(initial, "mcode") or self._field(media, "mCode"),
            title=self._field(initial, "title") or self._field(media, "title"),
            chapter=self._field(initial, "chapter") or self._field(media, "chapter"),
            brand=self._field(initial, "brand"),
            published_date=self._field(initial, "publishedDate"),
            registered_date=self._field(initial, "registratedDate"),
            country_code=self._field(initial, "countryCode") or self._field(initial, "country_code"),
            category_code=self._field(initial, "categoryCode") or self._array_first(initial, "category_code"),
            category_name=self._field(initial, "categoryCodeName") or self._field(initial, "category_code_name"),
            play_url=page_url,
            source_page=page_url,
            stream_urls=streams,
        )

        if item.country_code == "410" and not item.category_code and item.category_name == "광고":
            item.category_code = "1"

        if not item.title:
            item.title = self._meta_title(html)

        return item

    def sniff_streams_with_playwright(self, url: str, log: LogCallback = None) -> Dict[str, str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if log:
                log("Playwright가 설치되어 있지 않아 네트워크 감시 fallback을 건너뜁니다.")
            return {}

        found: Dict[str, str] = {}
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=self.USER_AGENT)

                def on_request(request) -> None:
                    req_url = request.url
                    if ".m3u8" in req_url:
                        key = "HD" if "720p" in req_url else "stream"
                        found.setdefault(key, req_url)

                page.on("request", on_request)
                page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
                page.wait_for_timeout(2500)
                browser.close()
        except Exception as exc:  # noqa: BLE001 - fallback should be quiet.
            if log:
                log(f"Playwright fallback 실패: {exc}")

        return found

    def _get_text(self, url: str, params: Optional[dict] = None) -> str:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"

        last_error: Exception | None = None
        for attempt in range(1, 4):
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
                    return raw.decode(encoding, errors="replace")
            except (IncompleteRead, TimeoutError, URLError, OSError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(0.8 * attempt)

        raise last_error or TVCFError(f"페이지를 읽지 못했습니다: {url}")

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

    def _extract_streams(self, text: str) -> Dict[str, str]:
        streams: Dict[str, str] = {}
        for quality in ("HD", "SD", "mobile", "urf", "url", "extSrc"):
            value = self._field(text, quality)
            if value and value.startswith("http"):
                clean_value = self._clean_url(value)
                key = "youtube" if "youtu.be/" in clean_value or "youtube.com/" in clean_value else quality
                streams[key] = clean_value

        for url in self._m3u8_re.findall(text):
            cleaned = self._clean_url(url)
            key = "HD" if "720p" in cleaned else "stream"
            streams.setdefault(key, cleaned)

        return streams

    def _candidate_urls(self, identifier: str) -> Iterable[str]:
        value = identifier.strip()
        if not value:
            return []

        if value.startswith("http://") or value.startswith("https://"):
            return [value]

        value = value.strip("/")
        if "/play/" in value:
            value = value.rsplit("/play/", 1)[-1]

        return [
            f"{self.BASE_URL}/play/{value}",
            f"https://play.tvcf.co.kr/{value}",
        ]

    @staticmethod
    def _slice_after(text: str, marker: str, length: int) -> str:
        match = re.search(marker, text)
        if not match:
            return ""
        start = match.start()
        return text[start : start + length]

    @staticmethod
    def _slug_from_url(url: str) -> str:
        return url.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _clean_url(value: str) -> str:
        return unescape(value).replace("\\u0026", "&")

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

    @staticmethod
    def _meta_title(html: str) -> str:
        match = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if not match:
            return ""
        return unescape(match.group(1)).replace("| TVCF", "").strip()


def filter_korean_ads(items: Iterable[MediaItem]) -> List[MediaItem]:
    return [
        item
        for item in items
        if item.country_code == "410" and (item.category_code == "1" or item.category_name == "광고")
    ]
