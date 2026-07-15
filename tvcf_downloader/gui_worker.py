from __future__ import annotations

import queue
import threading
from datetime import datetime

from .client import TVCFClient
from .downloader import DownloadCancelled, download_media, prepare_download_tools
from .history import flush_history
from .models import MediaItem


class DownloadWorkerMixin:
    def _worker_run(self, download: bool, options: dict[str, object]) -> None:
        client: TVCFClient | None = None
        try:
            client = TVCFClient()
            items = self._build_items(client, options)
            self.events.put(("items", items))
            self._checkpoint(f"대상 수집 완료: {self.run_summary} / 대상 {len(items)}개")

            if not download:
                self.events.put(("status", f"대상 확인 완료: {len(items)}개"))
                return

            if not items:
                self.events.put(("status", "대상 없음"))
                self.events.put(("log", "다운로드할 한국 광고가 없습니다. 날짜 기준과 시작일을 확인해주세요."))
                return

            self.events.put(("progress_max", max(1, len(items))))
            failed_count = 0
            parallel_count = self._normalize_parallel(options.get("parallel"))
            self.events.put(("log", "다운로드 도구 확인 중"))
            tools = prepare_download_tools(
                prefer_ytdlp=bool(options.get("prefer_ytdlp", True)),
                log=lambda msg: self.events.put(("log", msg)),
            )
            options = {**options, "tools": tools}
            failed_count += self._download_items_pipeline(client, items, parallel_count, options)
            if self.stop_event.is_set():
                self.events.put(("status", "중단됨"))
                self.events.put(("log", f"중단 지점: {self.last_checkpoint}"))
                return

            retry_failed_count = self._drain_retry_queue(client, options, wait_for_new=True)
            failed_count += retry_failed_count

            if failed_count:
                self.events.put(("status", f"다운로드 완료(오류 {failed_count}개)"))
            else:
                self.events.put(("status", "다운로드 완료"))
            self._checkpoint(f"작업 완료: {self.run_summary}")
        except Exception as exc:  # noqa: BLE001 - show GUI error.
            self._save_error_case("", None, "전체 작업", exc, options)
            self.events.put(("status", "오류"))
            self.events.put(("log", f"오류: {exc}"))
            self.events.put(("log", f"마지막 작업: {self.last_checkpoint}"))
        finally:
            if client:
                client.close()
            self._close_session_log()
            flush_history()

    def _download_items_pipeline(
        self,
        client: TVCFClient,
        items: list[MediaItem],
        parallel_count: int,
        options: dict[str, object],
    ) -> int:
        if parallel_count <= 1:
            return self._download_items_sequential_prefetch(client, items, options)
        return self._download_items_parallel_prefetch(items, parallel_count, options)

    def _download_items_sequential_prefetch(
        self,
        client: TVCFClient,
        items: list[MediaItem],
        options: dict[str, object],
    ) -> int:
        self.events.put(("log", "상세 확인 프리패치를 사용합니다."))
        sentinel = object()
        ready_queue: queue.Queue[object] = queue.Queue()

        def prefetch_loop() -> None:
            prefetch_client = TVCFClient()
            try:
                for index, item in enumerate(items, start=1):
                    if self.stop_event.is_set():
                        break
                    try:
                        detail = self._prefetch_item_detail(prefetch_client, index, len(items), item, options)
                        ready_queue.put((index, item, detail, None))
                    except DownloadCancelled:
                        self.stop_event.set()
                        break
                    except Exception as exc:  # noqa: BLE001 - hand detail errors to the normal item handler.
                        ready_queue.put((index, item, None, exc))
            finally:
                prefetch_client.close()
                ready_queue.put(sentinel)

        prefetch_thread = threading.Thread(target=prefetch_loop, daemon=True)
        prefetch_thread.start()

        failed_count = 0
        completed = 0
        try:
            while True:
                job = ready_queue.get()
                if job is sentinel:
                    break
                index, item, detail, detail_error = job
                if self.stop_event.is_set():
                    break
                failed_count += int(
                    self._download_one_item(
                        client,
                        index,
                        len(items),
                        item,
                        options,
                        prepared_detail=detail,
                        detail_error=detail_error,
                    )
                )
                completed += 1
                self.events.put(("progress", completed))
        finally:
            prefetch_thread.join()

        return failed_count

    def _download_items_parallel_prefetch(self, items: list[MediaItem], parallel_count: int, options: dict[str, object]) -> int:
        prefetch_count = min(max(2, parallel_count), 6, len(items))
        self.events.put(("log", f"병렬 다운로드 {parallel_count}개 / 상세 프리패치 {prefetch_count}개로 실행합니다."))
        failed_count = 0
        completed = 0
        item_queue: queue.Queue[object] = queue.Queue()
        ready_queue: queue.Queue[object] = queue.Queue()
        result_lock = threading.Lock()
        sentinel = object()

        for index, item in enumerate(items, start=1):
            item_queue.put((index, item))
        for _ in range(prefetch_count):
            item_queue.put(sentinel)

        def prefetch_loop() -> None:
            prefetch_client = TVCFClient()
            try:
                while not self.stop_event.is_set():
                    task = item_queue.get()
                    if task is sentinel:
                        return
                    index, item = task
                    try:
                        detail = self._prefetch_item_detail(prefetch_client, index, len(items), item, options)
                        ready_queue.put((index, item, detail, None))
                    except DownloadCancelled:
                        self.stop_event.set()
                        return
                    except Exception as exc:  # noqa: BLE001 - hand detail errors to the normal item handler.
                        ready_queue.put((index, item, None, exc))
            finally:
                prefetch_client.close()

        def download_loop() -> None:
            nonlocal completed, failed_count
            download_client = TVCFClient()
            try:
                while True:
                    job = ready_queue.get()
                    if job is sentinel:
                        return
                    item_index, item, detail, detail_error = job
                    if self.stop_event.is_set():
                        return
                    try:
                        failed = self._download_one_item(
                            download_client,
                            item_index,
                            len(items),
                            item,
                            options,
                            prepared_detail=detail,
                            detail_error=detail_error,
                        )
                    except DownloadCancelled:
                        self.stop_event.set()
                        self.events.put(("status", "중단됨"))
                        self.events.put(("log", f"중단 지점: {self.last_checkpoint}"))
                        return
                    except Exception as exc:  # noqa: BLE001 - keep other workers from disappearing silently.
                        row_id = item.nidx or item.idx or item.mcode
                        category = self._save_error_case(row_id, item, "병렬 작업", exc, options)
                        self.events.put(("item_status", row_id, category))
                        self.events.put(("log", f"병렬 작업 오류 - 건너뜀: {item.display_title} / {exc}"))
                        failed = True
                    with result_lock:
                        failed_count += int(failed)
                        completed += 1
                        self.events.put(("progress", completed))
            finally:
                download_client.close()

        prefetch_workers = [threading.Thread(target=prefetch_loop, daemon=True) for _ in range(prefetch_count)]
        download_workers = [
            threading.Thread(target=download_loop, daemon=True)
            for _ in range(min(parallel_count, len(items)))
        ]

        for worker in prefetch_workers + download_workers:
            worker.start()
        for worker in prefetch_workers:
            worker.join()
        for _ in download_workers:
            ready_queue.put(sentinel)
        for worker in download_workers:
            worker.join()

        return failed_count

    def _prefetch_item_detail(
        self,
        client: TVCFClient,
        index: int,
        total: int,
        item: MediaItem,
        options: dict[str, object],
    ) -> MediaItem:
        row_id = item.nidx or item.idx or item.mcode
        label = item.display_title
        date_basis = str(options.get("date_basis", "published"))
        position = self._item_position_text(index, total, item, date_basis)
        self._checkpoint(f"{self.run_summary} / {position} 상세 확인 중 / {label}")
        self.events.put(("item_status", row_id, "상세 확인"))
        self.events.put(("log", f"[{index}/{total}] {label}"))
        return self._get_media_detail(
            client,
            item,
            use_playwright=bool(options.get("use_playwright_fallback", True)),
        )

    def _download_one_item(
        self,
        client: TVCFClient,
        index: int,
        total: int,
        item: MediaItem,
        options: dict[str, object],
        prepared_detail: MediaItem | None = None,
        detail_error: BaseException | None = None,
    ) -> bool:
        row_id = item.nidx or item.idx or item.mcode
        if self.stop_event.is_set():
            raise DownloadCancelled("사용자 중단 요청")

        label = item.display_title
        date_basis = str(options.get("date_basis", "published"))
        position = self._item_position_text(index, total, item, date_basis)

        try:
            if detail_error:
                raise detail_error
            detail = prepared_detail or self._prefetch_item_detail(client, index, total, item, options)
        except Exception as exc:  # noqa: BLE001 - keep batch moving after one bad page.
            category = self._save_error_case(row_id, item, "상세 확인", exc, options)
            self.events.put(("item_status", row_id, category))
            self.events.put(("log", f"상세 확인 오류 - 건너뜀: {label} / {exc}"))
            return True

        if detail.country_code and detail.country_code != "410":
            self._record_session("한국 아님", "상세 확인", detail, "한국 광고가 아니어서 제외")
            self.events.put(("item_status", row_id, "한국 아님"))
            return False
        if detail.category_code and detail.category_code != "1":
            self._record_session("광고 아님", "상세 확인", detail, "광고 카테고리가 아니어서 제외")
            self.events.put(("item_status", row_id, "광고 아님"))
            return False

        merged = self._merge_item(item, detail)
        self._checkpoint(f"{self.run_summary} / {position} 다운로드 중 / {merged.display_title}")
        self.events.put(("item_status", row_id, "다운로드"))
        try:
            output = download_media(
                merged,
                str(options.get("download_dir", "")),
                str(options.get("quality", "가능한 최고화질")),
                str(options.get("date_basis", "published")),
                prefer_ytdlp=bool(options.get("prefer_ytdlp", True)),
                log=lambda msg: self.events.put(("log", msg)),
                should_stop=self.stop_event.is_set,
                tools=options.get("tools"),
                fast_verify=bool(options.get("fast_verify", True)),
            )
        except DownloadCancelled:
            self._record_session("중단됨", "다운로드", merged, "사용자 중단")
            self.events.put(("item_status", row_id, "중단됨"))
            raise
        except Exception as exc:  # noqa: BLE001 - skip failed item and continue.
            category = self._save_error_case(row_id, merged, "다운로드", exc, options)
            self.events.put(("item_status", row_id, category))
            self.events.put(("log", f"다운로드 오류 - 건너뜀: {merged.display_title} / {exc}"))
            return True

        result_status = "건너뜀" if output.skipped else "재다운완료" if output.repaired else "완료"
        self._record_session(result_status, "다운로드", merged, output_path=str(output.path))
        self.events.put(("output_path", row_id, str(output.path)))
        self.events.put(("item_status", row_id, result_status))
        self.events.put(("log", f"저장 완료: {output.path}"))
        return False

    def _build_items(self, client: TVCFClient, options: dict[str, object]) -> list[MediaItem]:
        start = datetime.strptime(str(options.get("date_from", "")).strip(), "%Y-%m-%d").date()
        end = datetime.strptime(str(options.get("date_to", "")).strip(), "%Y-%m-%d").date()
        if start > end:
            start, end = end, start
        date_basis = str(options.get("date_basis", "published"))
        items = client.collect_period(
            start,
            end,
            date_basis,
            0,
            log=lambda msg: self.events.put(("log", msg)),
            should_stop=self.stop_event.is_set,
        )
        return self._sort_items_by_file_date(items, date_basis)

    def _sort_items_by_file_date(self, items: list[MediaItem], basis: str) -> list[MediaItem]:
        indexed_items = list(enumerate(items))
        return [
            item
            for _, item in sorted(
                indexed_items,
                key=lambda pair: (
                    pair[1].date_value(basis) is None,
                    pair[1].date_value(basis),
                    pair[0],
                ),
            )
        ]
