import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, Canvas, Frame, IntVar, PhotoImage, StringVar, Tk, filedialog
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .client import TVCFClient, TVCFError
from .config import PROJECT_ROOT, load_config, save_config
from .diagnostics import SessionLog, classify_error, save_error_case
from .downloader import DownloadCancelled, download_media, prepare_download_tools
from .history import flush_history
from .issue_reporter import report_error_cases
from .models import MediaItem
from .text_utils import decode_output


COLORS = {
    "bg": "#f6f8fb",
    "surface": "#ffffff",
    "surface_alt": "#f8fafc",
    "panel": "#eef6ff",
    "panel_border": "#b9d4ff",
    "border": "#dfe5ef",
    "text": "#24324b",
    "muted": "#667085",
    "accent": "#2367d7",
    "accent_dark": "#174ea6",
    "accent_soft": "#e8f1ff",
    "danger": "#c92a2a",
    "danger_soft": "#ffe4e6",
    "success": "#17803d",
    "success_soft": "#dcfce7",
    "warning": "#b45309",
    "warning_soft": "#ffedd5",
    "log_bg": "#111827",
    "log_fg": "#d9e2ef",
    "log_muted": "#94a3b8",
    "log_green": "#22c55e",
    "log_yellow": "#fbbf24",
    "log_red": "#f87171",
}

ERROR_STATUSES = {
    "오류",
    "상세 오류",
    "네트워크 오류",
    "스트림 없음",
    "yt-dlp 실패",
    "ffmpeg 실패",
    "파일 손상",
    "접근 거부",
    "요청 제한",
    "알 수 없는 오류",
}
DEFERRED_RETRY_STATUSES = ERROR_STATUSES | {"중단됨", "보류"}
DONE_STATUSES = {"완료", "재다운완료"}
ACTIVE_STATUSES = {"상세 확인", "다운로드", "재시도", "대기열에 다시 추가됨"}
QUALITY_OPTIONS = ("가능한 최고화질", "HD", "SD", "mobile")
DATE_BASIS_LABELS = {
    "published": "방영일",
    "registered": "TVCF 업로드 날짜",
}
DATE_BASIS_VALUES = {label: value for value, label in DATE_BASIS_LABELS.items()}
COMPLETION_ACTION_OPTIONS = {
    "notify": ("안띄우기", "띄우기"),
    "open_folder": ("안열기", "열기"),
    "shutdown": ("안끄기", "끄기"),
}
COMPLETION_ACTION_KEYS = {
    "notify": "completion_action_notify",
    "open_folder": "completion_action_open_folder",
    "shutdown": "completion_action_shutdown",
}
ICON_DIR = PROJECT_ROOT / "img"
ICON_SPECS = {
    "app_logo": ("app_logo.png", 52),
    "folder_open": ("folder_open.png", 30),
    "folder_select": ("folder_select.png", 30),
    "calendar": ("calendar.png", 30),
    "settings": ("settings.png", 28),
    "notification": ("notification.png", 28),
    "start": ("start.png", 26),
    "log_terminal": ("log_terminal.png", 28),
    "stop": ("stop.png", 26),
    "list": ("list.png", 28),
}
SECTION_ICON_ALIASES = {
    "folder": "folder_open",
    "target": "calendar",
    "gear": "settings",
    "complete": "notification",
    "info": "notification",
    "list": "list",
    "log": "log_terminal",
}
MAX_GUI_EVENTS_PER_TICK = 80
MAX_VISIBLE_LOG_LINES = 2000
LOG_TRIM_LINES = 400
MAX_LOG_MESSAGE_CHARS = 1200
MEDIA_CACHE_TTL_SECONDS = 1800


class DownloaderApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("TVCF 한국 광고 다운로더")
        self.root.geometry("1680x940")
        self.root.minsize(1280, 760)
        self.root.configure(bg=COLORS["bg"])
        self.root.option_add("*Font", "{Malgun Gothic} 10")

        self.config = load_config()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.items: list[MediaItem] = []
        self.item_by_id: dict[str, MediaItem] = {}
        self.row_records: dict[str, dict] = {}
        self.row_order: list[str] = []
        self.checked_rows: set[str] = set()
        self.retry_queue: deque[tuple[str, MediaItem]] = deque()
        self.queued_retry_rows: set[str] = set()
        self.retry_lock = threading.Lock()
        self.media_cache: dict[str, tuple[MediaItem, float]] = {}
        self.media_cache_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.run_summary = ""
        self.last_checkpoint = self.config.get("last_checkpoint", "작업 없음")
        self.error_case_by_id: dict[str, str] = {}
        self.session_log: SessionLog | None = None
        self.run_started_at: float = 0.0
        self.playwright_fallback_count = 0
        self.completion_actions_ran = False
        self.stop_requested_by_user = False
        self.last_metric_update = 0.0
        self.last_ffmpeg_size_bytes: float | None = None
        self.last_ffmpeg_size_at = 0.0
        self.log_line_count = 0
        self.last_checkpoint_save_at = 0.0

        today = datetime.now().date()
        default_from = today - timedelta(days=30)

        self.download_dir_var = StringVar(value=self.config.get("download_dir", ""))
        self.date_from_var = StringVar(value=self.config.get("date_from", default_from.strftime("%Y-%m-%d")))
        self.date_to_var = StringVar(value=self.config.get("date_to", today.strftime("%Y-%m-%d")))
        self.date_basis_label_var = StringVar(value=self._date_basis_label(self.config.get("date_basis", "published")))
        self.quality_var = StringVar(value=self._normalize_quality(self.config.get("quality", "가능한 최고화질")))
        self.max_pages_var = IntVar(value=0)
        self.parallel_var = IntVar(value=self._normalize_parallel(self.config.get("parallel_downloads", 3)))
        self.prefer_ytdlp_var = BooleanVar(value=True)
        self.playwright_var = BooleanVar(value=True)
        self.completion_notify_var = StringVar(value=self._completion_label("notify", self._completion_config_value("notify")))
        self.completion_open_folder_var = StringVar(
            value=self._completion_label("open_folder", self._completion_config_value("open_folder"))
        )
        self.completion_shutdown_var = StringVar(value=self._completion_label("shutdown", self._completion_config_value("shutdown")))
        self.status_filter_var = StringVar(value="전체")
        self.search_var = StringVar(value="")
        self.status_var = StringVar(value="대기 중")
        self.status_badge_var = StringVar(value="● 대기 중")
        self.current_task_var = StringVar(value=self.last_checkpoint)
        self.progress_text_var = StringVar(value="0 / 0 (0%)")
        self.speed_var = StringVar(value="속도: 계산 중")
        self.elapsed_time_var = StringVar(value="경과 시간: 0초")
        self.eta_var = StringVar(value="남은 시간: 계산 중")
        self.summary_stats_var = StringVar(value="완료 0 / 오류 0 / 건너뜀 0 / Playwright 0회")

        self._configure_style()
        self.icons = self._load_icons()
        if self.icons.get("app_logo"):
            self.root.iconphoto(True, self.icons["app_logo"])
        self._build_ui()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:  # noqa: BLE001 - Tcl theme availability can vary.
            pass

        base_font = ("Malgun Gothic", 10)
        medium_font = ("Malgun Gothic", 10, "bold")
        title_font = ("Malgun Gothic", 18, "bold")
        small_font = ("Malgun Gothic", 9)
        mono_font = ("Consolas", 10)

        self.style.configure(".", font=base_font, background=COLORS["bg"], foreground=COLORS["text"])
        self.style.configure("Main.TFrame", background=COLORS["bg"])
        self.style.configure("Surface.TFrame", background=COLORS["surface"])
        self.style.configure("Panel.TFrame", background=COLORS["panel"])
        self.style.configure("HeaderTitle.TLabel", font=title_font, background=COLORS["bg"], foreground="#101828")
        self.style.configure("HeaderSub.TLabel", font=small_font, background=COLORS["bg"], foreground=COLORS["muted"])
        self.style.configure("HeaderIcon.TLabel", background=COLORS["bg"])
        self.style.configure("Icon.TLabel", background=COLORS["surface"])
        self.style.configure("TLabel", background=COLORS["surface"], foreground=COLORS["text"])
        self.style.configure("Muted.TLabel", font=small_font, background=COLORS["bg"], foreground=COLORS["muted"])
        self.style.configure("SurfaceMuted.TLabel", font=small_font, background=COLORS["surface"], foreground=COLORS["muted"])
        self.style.configure("PanelText.TLabel", background=COLORS["panel"], foreground="#24466f")
        self.style.configure("SectionTitle.TLabel", font=medium_font, background=COLORS["surface"], foreground=COLORS["text"])
        self.style.configure("SectionIcon.TLabel", font=("Malgun Gothic", 12, "bold"), background=COLORS["surface"], foreground=COLORS["muted"])
        self.style.configure("ProgressTitle.TLabel", font=medium_font, background=COLORS["surface"], foreground=COLORS["text"])
        self.style.configure("ProgressCount.TLabel", font=medium_font, background=COLORS["surface"], foreground=COLORS["accent"])

        self.style.configure("TCheckbutton", background=COLORS["surface"], foreground=COLORS["text"])
        self.style.configure("TRadiobutton", background=COLORS["surface"], foreground=COLORS["text"])

        self.style.configure(
            "Input.TEntry",
            padding=(10, 6),
            fieldbackground="#ffffff",
            bordercolor="#ccd6e3",
            lightcolor="#ccd6e3",
            darkcolor="#ccd6e3",
            foreground=COLORS["text"],
        )
        self.style.configure(
            "Input.TCombobox",
            padding=(8, 5),
            fieldbackground="#ffffff",
            bordercolor="#ccd6e3",
            lightcolor="#ccd6e3",
            darkcolor="#ccd6e3",
            arrowcolor=COLORS["muted"],
            foreground=COLORS["text"],
        )
        self.style.map(
            "Input.TCombobox",
            fieldbackground=[("readonly", "#ffffff")],
            selectbackground=[("readonly", "#ffffff")],
            selectforeground=[("readonly", COLORS["text"])],
        )
        self.style.configure(
            "Input.TSpinbox",
            padding=(8, 5),
            fieldbackground="#ffffff",
            bordercolor="#ccd6e3",
            lightcolor="#ccd6e3",
            darkcolor="#ccd6e3",
            arrowcolor=COLORS["muted"],
            foreground=COLORS["text"],
        )

        self.style.configure("TButton", padding=(13, 7), background="#f8fafc", foreground=COLORS["text"], bordercolor="#ccd6e3")
        self.style.map("TButton", background=[("active", "#eef2f7")])
        self.style.configure("Accent.TButton", background="#ffffff", foreground=COLORS["accent"], bordercolor="#b8c9e8")
        self.style.map("Accent.TButton", background=[("active", "#f8fbff")], foreground=[("active", COLORS["accent_dark"])])
        self.style.configure("Danger.TButton", background="#ffffff", foreground=COLORS["danger"], bordercolor="#efb8b8")
        self.style.map("Danger.TButton", background=[("active", "#fff8f8")], foreground=[("active", COLORS["danger"])])
        self.style.configure("Text.TButton", padding=(8, 4), background=COLORS["surface"], foreground=COLORS["muted"], bordercolor=COLORS["surface"])
        self.style.map("Text.TButton", background=[("active", "#f2f5fa")], foreground=[("active", COLORS["text"])])
        self.style.configure("BlackText.TButton", padding=(8, 4), background=COLORS["surface"], foreground=COLORS["text"], bordercolor=COLORS["surface"])
        self.style.map("BlackText.TButton", background=[("active", "#f2f5fa")], foreground=[("active", COLORS["text"])])

        self.style.configure("Badge.Idle.TLabel", font=medium_font, background=COLORS["accent_soft"], foreground=COLORS["accent"], padding=(14, 6))
        self.style.configure("Badge.Working.TLabel", font=medium_font, background=COLORS["accent_soft"], foreground=COLORS["accent"], padding=(14, 6))
        self.style.configure("Badge.Done.TLabel", font=medium_font, background=COLORS["success_soft"], foreground=COLORS["success"], padding=(14, 6))
        self.style.configure("Badge.Error.TLabel", font=medium_font, background=COLORS["danger_soft"], foreground=COLORS["danger"], padding=(14, 6))

        self.style.configure(
            "Treeview",
            font=base_font,
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            rowheight=25,
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            relief="flat",
        )
        self.style.configure(
            "Treeview.Heading",
            font=medium_font,
            background="#edf2f7",
            foreground=COLORS["text"],
            relief="flat",
            padding=(8, 6),
        )
        self.style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", COLORS["text"])])

        self.style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#e6ebf2",
            background=COLORS["accent"],
            bordercolor="#e6ebf2",
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
        )
        self.log_font = mono_font

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(14, 12), style="Main.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        self._build_header(outer)

        content = ttk.Frame(outer, style="Main.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1, minsize=900)
        content.columnconfigure(1, weight=0, minsize=390)
        content.rowconfigure(0, weight=1)

        left = ttk.Frame(content, style="Main.TFrame")
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(6, weight=1, minsize=170)

        right = ttk.Frame(content, style="Main.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self._build_path_card(left)
        self._build_target_card(left)
        self._build_options_card(left)
        self._build_actions(left)
        self._build_checkpoint_card(left)
        self._build_list_card(left)
        self._build_progress_card(left)
        self._build_log_card(right, row=0)

        self.root.bind("<Configure>", self._sync_wraplength)

    def _build_header(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="Main.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(1, weight=1)

        logo_icon = self.icons.get("app_logo")
        if logo_icon:
            ttk.Label(header, image=logo_icon, style="HeaderIcon.TLabel").grid(row=0, column=0, rowspan=2, sticky="w", padx=(2, 14))
        else:
            logo = Canvas(header, width=34, height=34, bg=COLORS["bg"], highlightthickness=0)
            logo.grid(row=0, column=0, rowspan=2, sticky="w", padx=(2, 14))
            logo.create_polygon(5, 5, 17, 11, 17, 25, 5, 31, fill=COLORS["accent_dark"], outline="")
            logo.create_polygon(17, 4, 31, 12, 18, 19, fill="#2f8de4", outline="")
            logo.create_polygon(18, 20, 31, 27, 17, 32, fill="#52a6ff", outline="")
            logo.create_line(17, 10, 17, 27, fill="#ffffff", width=2)

        ttk.Label(header, text="TVCF 한국 광고 다운로더", style="HeaderTitle.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(
            header,
            text="기간으로 한국 광고 영상을 수집하고 다운로드합니다.",
            style="HeaderSub.TLabel",
        ).grid(row=1, column=1, sticky="w", pady=(3, 0))
        self.status_label = ttk.Label(header, textvariable=self.status_badge_var, style="Badge.Idle.TLabel", anchor="center")
        self.status_label.grid(row=0, column=2, rowspan=2, sticky="e", padx=(16, 4))

    def _build_path_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=1)
        card.columnconfigure(1, weight=1)

        self._section_title(card, "저장 위치", "folder").grid(row=0, column=0, sticky="w", padx=(18, 16), pady=14)
        ttk.Entry(card, textvariable=self.download_dir_var, style="Input.TEntry").grid(row=0, column=1, sticky="ew", pady=14)
        self._icon_button(card, "저장 폴더 열기", self._open_download_dir, "folder_open").grid(row=0, column=2, sticky="e", padx=(14, 8), pady=14)
        self._icon_button(card, "폴더 선택", self._choose_download_dir, "folder_select").grid(row=0, column=3, sticky="e", padx=(0, 18), pady=14)

    def _build_target_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=2)
        card.columnconfigure(0, weight=1)

        self._section_title(card, "다운로드 대상", "target").grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4))

        body = ttk.Frame(card, style="Surface.TFrame")
        body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        for idx in (2, 4, 6):
            body.columnconfigure(idx, weight=1)

        ttk.Label(body, text="기간").grid(row=0, column=0, sticky="w", padx=(0, 18), pady=6)
        ttk.Label(body, text="시작").grid(row=0, column=1, sticky="e", padx=(0, 8), pady=6)
        ttk.Entry(body, width=14, textvariable=self.date_from_var, style="Input.TEntry").grid(row=0, column=2, sticky="ew", padx=(0, 30), pady=6)
        ttk.Label(body, text="끝").grid(row=0, column=3, sticky="e", padx=(0, 8), pady=6)
        ttk.Entry(body, width=14, textvariable=self.date_to_var, style="Input.TEntry").grid(row=0, column=4, sticky="ew", padx=(0, 30), pady=6)
        ttk.Label(body, text="기준").grid(row=0, column=5, sticky="e", padx=(0, 8), pady=6)
        ttk.Combobox(
            body,
            width=13,
            state="readonly",
            textvariable=self.date_basis_label_var,
            values=tuple(DATE_BASIS_LABELS.values()),
            style="Input.TCombobox",
        ).grid(row=0, column=6, sticky="ew", pady=6)

    def _build_options_card(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent, style="Main.TFrame")
        row.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

        card = self._card(row, row=0, column=0, padx=(0, 5), pady=0)
        card.columnconfigure(0, weight=1)

        self._section_title(card, "다운로드 옵션", "gear").grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4))

        body = ttk.Frame(card, style="Surface.TFrame")
        body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        body.columnconfigure(3, weight=1)

        ttk.Label(body, text="화질").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Combobox(
            body,
            width=14,
            state="readonly",
            textvariable=self.quality_var,
            values=QUALITY_OPTIONS,
            style="Input.TCombobox",
        ).grid(row=0, column=1, sticky="w", padx=(0, 36))
        ttk.Label(body, text="병렬").grid(row=0, column=2, sticky="e", padx=(0, 8))
        ttk.Combobox(
            body,
            width=5,
            state="readonly",
            textvariable=self.parallel_var,
            values=(1, 2, 3, 4, 5, 6),
            style="Input.TCombobox",
        ).grid(row=0, column=3, sticky="w", padx=(0, 28))

        completion_card = self._card(row, row=0, column=1, padx=(5, 0), pady=0)
        completion_card.columnconfigure(0, weight=1)
        self._section_title(completion_card, "다운로드 완료 시 동작", "complete").grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4))
        completion = ttk.Frame(completion_card, style="Surface.TFrame")
        completion.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        for idx in (1, 3, 5):
            completion.columnconfigure(idx, weight=1)

        ttk.Label(completion, text="알림창").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Combobox(
            completion,
            width=12,
            state="readonly",
            textvariable=self.completion_notify_var,
            values=COMPLETION_ACTION_OPTIONS["notify"],
            style="Input.TCombobox",
        ).grid(row=0, column=1, sticky="w", padx=(0, 32), pady=4)
        ttk.Label(completion, text="폴더").grid(row=0, column=2, sticky="e", padx=(0, 8), pady=4)
        ttk.Combobox(
            completion,
            width=12,
            state="readonly",
            textvariable=self.completion_open_folder_var,
            values=COMPLETION_ACTION_OPTIONS["open_folder"],
            style="Input.TCombobox",
        ).grid(row=0, column=3, sticky="w", padx=(0, 32), pady=4)
        ttk.Label(completion, text="전원").grid(row=0, column=4, sticky="e", padx=(0, 8), pady=4)
        ttk.Combobox(
            completion,
            width=12,
            state="readonly",
            textvariable=self.completion_shutdown_var,
            values=COMPLETION_ACTION_OPTIONS["shutdown"],
            style="Input.TCombobox",
        ).grid(row=0, column=5, sticky="w", pady=4)

    def _build_actions(self, parent: ttk.Frame) -> None:
        action = ttk.Frame(parent, style="Main.TFrame")
        action.grid(row=4, column=0, sticky="ew", pady=(8, 10))
        action.columnconfigure(2, weight=1)

        self._icon_button(action, "다운로드 시작", self.download, "start", style="Accent.TButton").grid(row=0, column=0, padx=(0, 10))
        self._icon_button(action, "중지", self.stop, "stop", style="Danger.TButton").grid(row=0, column=1)
        ttk.Label(action, text="현재 상태", style="Muted.TLabel").grid(row=0, column=3, sticky="e", padx=(0, 10))
        ttk.Label(action, textvariable=self.status_var, style="Muted.TLabel").grid(row=0, column=4, sticky="e")

    def _build_checkpoint_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=5)
        card.columnconfigure(0, weight=1)

        self._section_title(card, "작업 요약 / 중단 지점", "info").grid(row=0, column=0, sticky="w", padx=18, pady=(12, 4))

        info = Frame(card, bg=COLORS["panel"], highlightbackground=COLORS["panel_border"], highlightthickness=1, bd=0)
        info.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))
        info.columnconfigure(0, weight=1)
        self.current_task_label = ttk.Label(
            info,
            textvariable=self.current_task_var,
            style="PanelText.TLabel",
            anchor="w",
            justify="left",
            wraplength=1120,
        )
        self.current_task_label.grid(row=0, column=0, sticky="ew", padx=14, pady=10)

    def _build_list_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=6)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1, minsize=120)

        header = ttk.Frame(card, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 6))
        header.columnconfigure(0, weight=1)
        self._section_title(header, "다운로드 목록", "list").grid(row=0, column=0, sticky="w")
        self._icon_button(header, "체크 재다운로드", self._retry_selected_item, "list", style="Text.TButton").grid(
            row=0,
            column=1,
            sticky="e",
            padx=(0, 8),
        )
        self._icon_button(header, "보류 일괄 재다운로드", self._retry_deferred_items, "list", style="Text.TButton").grid(
            row=0,
            column=2,
            sticky="e",
            padx=(0, 8),
        )
        self._icon_button(header, "기훈이한테 이르기", self._report_selected_errors, "notification", style="Text.TButton").grid(
            row=0,
            column=3,
            sticky="e",
            padx=(0, 8),
        )
        ttk.Button(header, text="지우기", command=self._clear_download_list, style="BlackText.TButton").grid(
            row=0,
            column=4,
            sticky="e",
        )

        filter_bar = ttk.Frame(card, style="Surface.TFrame")
        filter_bar.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
        filter_bar.columnconfigure(3, weight=1)
        ttk.Label(filter_bar, text="상태").grid(row=0, column=0, sticky="w", padx=(0, 8))
        status_filter = ttk.Combobox(
            filter_bar,
            width=12,
            state="readonly",
            textvariable=self.status_filter_var,
            values=("전체", "완료", "오류", "보류", "중단됨", "건너뜀", "다운로드 중"),
            style="Input.TCombobox",
        )
        status_filter.grid(row=0, column=1, sticky="w", padx=(0, 18))
        status_filter.bind("<<ComboboxSelected>>", lambda _event: self._render_tree())
        ttk.Label(filter_bar, text="검색").grid(row=0, column=2, sticky="w", padx=(0, 8))
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_var, style="Input.TEntry")
        search_entry.grid(row=0, column=3, sticky="ew")
        search_entry.bind("<KeyRelease>", lambda _event: self._render_tree())

        table_wrap = ttk.Frame(card, style="Surface.TFrame")
        table_wrap.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 12))
        table_wrap.columnconfigure(0, weight=1)
        table_wrap.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_wrap,
            columns=("check", "date", "title", "id", "status"),
            show="headings",
            height=6,
        )
        self.tree.heading("check", text="선택")
        self.tree.heading("date", text="날짜")
        self.tree.heading("title", text="제목")
        self.tree.heading("id", text="ID")
        self.tree.heading("status", text="상태")
        self.tree.column("check", width=60, anchor="center", stretch=False)
        self.tree.column("date", width=110, anchor="center", stretch=False)
        self.tree.column("title", width=430, anchor="w")
        self.tree.column("id", width=150, anchor="center", stretch=False)
        self.tree.column("status", width=130, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        self.tree.tag_configure("odd", background=COLORS["surface"])
        self.tree.tag_configure("even", background=COLORS["surface_alt"])
        self.tree.tag_configure("active", foreground=COLORS["accent_dark"])
        self.tree.tag_configure("done", foreground=COLORS["success"])
        self.tree.tag_configure("skip", foreground=COLORS["muted"])
        self.tree.tag_configure("warning", foreground=COLORS["warning"])
        self.tree.tag_configure("error", foreground=COLORS["danger"])

    def _build_log_card(self, parent: ttk.Frame, row: int = 7) -> None:
        card = self._card(parent, row=row, pady=0)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1, minsize=100)

        header = ttk.Frame(card, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 6))
        header.columnconfigure(0, weight=1)
        self._section_title(header, "실시간 로그", "log").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="로그 지우기", command=self._clear_log, style="Text.TButton").grid(row=0, column=1, sticky="e")

        log_wrap = ttk.Frame(card, style="Surface.TFrame")
        log_wrap.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 12))
        log_wrap.columnconfigure(0, weight=1)
        log_wrap.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(
            log_wrap,
            height=7,
            width=38,
            wrap="word",
            relief="flat",
            borderwidth=0,
            background=COLORS["log_bg"],
            foreground=COLORS["log_fg"],
            insertbackground="#ffffff",
            selectbackground="#334155",
            font=self.log_font,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("time", foreground=COLORS["log_muted"])
        self.log_text.tag_configure("info", foreground=COLORS["log_green"])
        self.log_text.tag_configure("warn", foreground=COLORS["log_yellow"])
        self.log_text.tag_configure("error", foreground=COLORS["log_red"])
        self.log_text.tag_configure("message", foreground=COLORS["log_fg"])

    def _build_progress_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=8)
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="진행률", style="ProgressTitle.TLabel").grid(row=0, column=0, sticky="w", padx=(18, 18), pady=13)
        self.progress = ttk.Progressbar(card, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="ew", pady=(13, 4))
        ttk.Label(card, textvariable=self.progress_text_var, style="ProgressCount.TLabel").grid(row=0, column=2, sticky="e", padx=(18, 18), pady=13)
        metrics = ttk.Frame(card, style="Surface.TFrame")
        metrics.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 18), pady=(0, 12))
        metrics.columnconfigure(3, weight=1)
        ttk.Label(metrics, textvariable=self.speed_var, style="SurfaceMuted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 18))
        ttk.Label(metrics, textvariable=self.elapsed_time_var, style="SurfaceMuted.TLabel").grid(row=0, column=1, sticky="w", padx=(0, 18))
        ttk.Label(metrics, textvariable=self.eta_var, style="SurfaceMuted.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 18))
        ttk.Label(metrics, textvariable=self.summary_stats_var, style="SurfaceMuted.TLabel").grid(row=0, column=3, sticky="e")

    def _card(
        self,
        parent: ttk.Frame,
        row: int,
        column: int = 0,
        padx: tuple[int, int] | int = 0,
        pady: tuple[int, int] | int = (0, 10),
    ) -> Frame:
        card = Frame(
            parent,
            bg=COLORS["surface"],
            bd=0,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"],
            highlightthickness=1,
        )
        card.grid(row=row, column=column, sticky="nsew", padx=padx, pady=pady)
        return card

    def _load_icons(self) -> dict[str, PhotoImage]:
        icons: dict[str, PhotoImage] = {}
        for name, (filename, target_size) in ICON_SPECS.items():
            path = ICON_DIR / filename
            if not path.exists():
                continue
            try:
                image = PhotoImage(file=str(path))
                scale = max(1, round(max(image.width(), image.height()) / target_size))
                icons[name] = image.subsample(scale, scale)
            except Exception:  # noqa: BLE001 - missing or unsupported images should not block startup.
                continue
        return icons

    def _icon_button(
        self,
        parent: object,
        text: str,
        command: object,
        icon: str,
        style: str | None = None,
    ) -> ttk.Button:
        options = {
            "text": text,
            "command": command,
        }
        if style:
            options["style"] = style
        image = self.icons.get(icon)
        if image:
            options["image"] = image
            options["compound"] = "left"
        return ttk.Button(parent, **options)

    def _section_title(self, parent: object, text: str, icon: str) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Surface.TFrame")
        icon_name = SECTION_ICON_ALIASES.get(icon, icon)
        icon_image = self.icons.get(icon_name)
        if icon_image:
            ttk.Label(frame, image=icon_image, style="Icon.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        else:
            icon_canvas = Canvas(frame, width=18, height=18, bg=COLORS["surface"], highlightthickness=0)
            icon_canvas.grid(row=0, column=0, sticky="w", padx=(0, 8))
            self._draw_section_icon(icon_canvas, icon)
        ttk.Label(frame, text=text, style="SectionTitle.TLabel").grid(row=0, column=1, sticky="w")
        return frame

    @staticmethod
    def _draw_section_icon(canvas: Canvas, icon: str) -> None:
        color = COLORS["muted"]
        accent = COLORS["accent"]
        if icon == "folder":
            canvas.create_line(2, 7, 7, 7, 8, 9, 16, 9, 16, 15, 2, 15, 2, 7, fill=color, width=1.8)
            canvas.create_line(2, 7, 2, 5, 7, 5, 8, 7, fill=color, width=1.8)
        elif icon == "target":
            canvas.create_oval(2, 2, 16, 16, outline=color, width=1.7)
            canvas.create_oval(6, 6, 12, 12, outline=color, width=1.5)
            canvas.create_oval(8, 8, 10, 10, fill=accent, outline=accent)
            canvas.create_line(12, 6, 16, 2, fill=color, width=1.4)
        elif icon == "gear":
            canvas.create_oval(5, 5, 13, 13, outline=color, width=1.6)
            canvas.create_oval(8, 8, 10, 10, fill=color, outline=color)
            for x1, y1, x2, y2 in ((9, 1, 9, 4), (9, 14, 9, 17), (1, 9, 4, 9), (14, 9, 17, 9), (4, 4, 6, 6), (12, 12, 14, 14), (14, 4, 12, 6), (6, 12, 4, 14)):
                canvas.create_line(x1, y1, x2, y2, fill=color, width=1.4)
        elif icon == "info":
            canvas.create_oval(2, 2, 16, 16, outline=color, width=1.6)
            canvas.create_text(9, 10, text="i", fill=color, font=("Segoe UI", 10, "bold"))
        elif icon == "list":
            for y in (5, 9, 13):
                canvas.create_oval(3, y - 1, 5, y + 1, fill=color, outline=color)
                canvas.create_line(8, y, 16, y, fill=color, width=1.5)
        elif icon == "log":
            canvas.create_line(3, 5, 7, 9, 3, 13, fill=color, width=1.6)
            canvas.create_line(9, 13, 16, 13, fill=color, width=1.6)
        else:
            canvas.create_oval(4, 4, 14, 14, outline=color, width=1.6)

    def _on_tree_click(self, event: object) -> None:
        region = self.tree.identify("region", event.x, event.y)
        column = self.tree.identify_column(event.x)
        item_id = self.tree.identify_row(event.y)
        if region == "cell" and column == "#1" and item_id:
            self._toggle_row_checked(item_id)

    def _toggle_row_checked(self, row_id: str) -> None:
        if row_id not in self.row_records:
            return
        if row_id in self.checked_rows:
            self.checked_rows.remove(row_id)
        else:
            self.checked_rows.add(row_id)
        self._render_tree()

    def _queue_checked_retries(self) -> None:
        row_ids = [row_id for row_id in self.row_order if row_id in self.checked_rows]
        self._queue_retry_rows(row_ids, "재다운로드할 항목을 체크해주세요.")

    def _retry_deferred_items(self) -> None:
        row_ids = [
            row_id
            for row_id in self.row_order
            if self._row_status(row_id) in DEFERRED_RETRY_STATUSES
        ]
        self._queue_retry_rows(row_ids, "재다운로드할 보류/오류 항목이 없습니다.")

    def _queue_retry_rows(self, row_ids: list[str], empty_message: str) -> None:
        if self.worker and self.worker.is_alive():
            worker_running = True
        else:
            worker_running = False

        if not row_ids:
            self._log(empty_message)
            return

        added = 0
        with self.retry_lock:
            for row_id in row_ids:
                item = self.item_by_id.get(row_id)
                if not item or row_id in self.queued_retry_rows:
                    continue

                self.retry_queue.append((row_id, item))
                self.queued_retry_rows.add(row_id)
                added += 1

        for row_id in row_ids:
            self._set_row_checked(row_id, False)
            if row_id in self.queued_retry_rows:
                if row_id in self.row_order:
                    self.row_order.remove(row_id)
                    self.row_order.append(row_id)
                self._set_item_status(row_id, "대기열에 다시 추가됨")

        if added <= 0:
            self._log("이미 재다운로드 대기열에 있는 항목입니다.")
            return

        if worker_running:
            self._log(f"재다운로드 대기열에 {added}개 항목을 추가했습니다.")
            self._extend_progress_total(added)
            self._set_status("재다운로드 대기열 추가됨")
            return

        self.stop_event.clear()
        self._save_current_config()
        self.run_summary = f"재다운로드 대기열 / {added}개"
        self._set_checkpoint(f"재다운로드 대기열 준비: {self.run_summary}")
        self.run_started_at = time.monotonic()
        self.progress.configure(value=0, maximum=max(1, added))
        self._set_progress_text(0, added)
        self._reset_speed_metrics()
        self.elapsed_time_var.set("경과 시간: 0초")
        self._log(f"재다운로드 대기열에 {added}개 항목을 추가했습니다.")
        self._set_status("재다운로드 대기열 실행 중")
        options = self._download_options_snapshot()
        self.worker = threading.Thread(target=self._retry_queue_worker_run, args=(options,), daemon=True)
        self.stop_requested_by_user = False
        self.completion_actions_ran = False
        self.worker.start()

    def _retry_selected_item(self) -> None:
        self._queue_checked_retries()

    def _report_selected_errors(self) -> None:
        row_ids = self._error_rows()
        paths = [Path(self.error_case_by_id[row_id]) for row_id in row_ids if row_id in self.error_case_by_id]
        if not paths:
            self._log("신고할 오류 로그가 없습니다. 목록에 오류 상태인 항목이 있는지 확인해주세요.")
            return

        self._log(f"기훈이한테 이를 오류 {len(paths)}건을 GitHub 이슈로 업로드합니다.")
        threading.Thread(target=self._issue_report_worker, args=(paths,), daemon=True).start()

    def _error_rows(self) -> list[str]:
        return [
            row_id
            for row_id in self.row_order
            if self._row_status(row_id) in ERROR_STATUSES
        ]

    def _issue_report_worker(self, paths: list[Path]) -> None:
        try:
            result = report_error_cases(paths, self.run_summary or self.last_checkpoint)
            self.events.put(("log", result.message))
            if result.url:
                self.events.put(("log", f"이슈 링크: {result.url}"))
            self.events.put(("status", "이슈 등록 완료" if result.created else "이슈 작성창 열림"))
        except Exception as exc:  # noqa: BLE001 - keep GUI alive if reporting fails.
            self.events.put(("status", "이슈 등록 오류"))
            self.events.put(("log", f"기훈이한테 이르기 실패: {exc}"))

    def _row_status(self, row_id: str) -> str:
        record = self.row_records.get(row_id)
        if record:
            return str(record.get("status", ""))
        if not self.tree.exists(row_id):
            return ""
        values = list(self.tree.item(row_id, "values"))
        return str(values[4]) if len(values) >= 5 else ""

    def _set_row_checked(self, row_id: str, checked: bool) -> None:
        if row_id not in self.row_records:
            return
        if checked:
            self.checked_rows.add(row_id)
        else:
            self.checked_rows.discard(row_id)
        self._render_tree()

    def _extend_progress_total(self, amount: int) -> None:
        if not hasattr(self, "progress") or amount <= 0:
            return
        try:
            current = int(float(self.progress.cget("value")))
            maximum = int(float(self.progress.cget("maximum")))
        except (TypeError, ValueError):
            current = 0
            maximum = 0
        new_maximum = max(current, maximum) + amount
        self.progress.configure(maximum=max(1, new_maximum))
        self._set_progress_text(current, new_maximum)

    def _retry_queue_worker_run(self, options: dict[str, object]) -> None:
        client: TVCFClient | None = None
        try:
            client = TVCFClient()
            self.events.put(("log", "다운로드 도구 확인 중"))
            tools = prepare_download_tools(
                prefer_ytdlp=bool(options.get("prefer_ytdlp", True)),
                log=lambda msg: self.events.put(("log", msg)),
            )
            options = {**options, "tools": tools}
            failed_count = self._drain_retry_queue(client, options)
            if failed_count:
                self.events.put(("status", f"재다운로드 완료(오류 {failed_count}개)"))
            else:
                self.events.put(("status", "재다운로드 완료"))
            self._checkpoint(f"재다운로드 대기열 완료: {self.run_summary}")
        except DownloadCancelled:
            self.events.put(("status", "중단됨"))
            self.events.put(("log", f"중단 지점: {self.last_checkpoint}"))
        except Exception as exc:  # noqa: BLE001 - show retry failure without killing GUI.
            self.events.put(("status", "재다운로드 오류"))
            self.events.put(("log", f"재다운로드 대기열 실패: {exc}"))
        finally:
            if client:
                client.close()
            flush_history()

    def _drain_retry_queue(self, client: TVCFClient, options: dict[str, object]) -> int:
        failed_count = 0
        while True:
            with self.retry_lock:
                if not self.retry_queue:
                    break
                row_id, item = self.retry_queue.popleft()

            if self.stop_event.is_set():
                raise DownloadCancelled("사용자 중단 요청으로 재다운로드 대기열을 중단했습니다.")

            try:
                self._download_retry_item(client, row_id, item, options)
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - keep draining retry queue.
                failed_count += 1
                category = self._save_error_case(row_id, item, "재다운로드", exc, options)
                self.events.put(("item_status", row_id, category))
                self.events.put(("log", f"재다운로드 실패 - 건너뜀: {item.display_title} / {exc}"))
            finally:
                with self.retry_lock:
                    self.queued_retry_rows.discard(row_id)
                self.events.put(("progress_step", 1))

        return failed_count

    def _download_retry_item(
        self,
        client: TVCFClient,
        row_id: str,
        item: MediaItem,
        options: dict[str, object],
    ) -> None:
        label = item.display_title
        date_basis = str(options.get("date_basis", "published"))
        position = self._item_position_text(1, 1, item, date_basis)

        self._checkpoint(f"{self.run_summary} / {position} 상세 확인 중 / {label}")
        self.events.put(("item_status", row_id, "상세 확인"))
        detail = self._get_media_for_retry(client, item, use_playwright=bool(options.get("use_playwright_fallback", True)))

        if detail.country_code and detail.country_code != "410":
            self._record_session("한국 아님", "재다운로드", detail, "한국 광고가 아니어서 제외")
            self.events.put(("item_status", row_id, "한국 아님"))
            self.events.put(("log", f"재다운로드 제외: 한국 광고가 아닙니다 / {label}"))
            return
        if detail.category_code and detail.category_code != "1":
            self._record_session("광고 아님", "재다운로드", detail, "광고 카테고리가 아니어서 제외")
            self.events.put(("item_status", row_id, "광고 아님"))
            self.events.put(("log", f"재다운로드 제외: 광고 카테고리가 아닙니다 / {label}"))
            return

        merged = self._merge_item(item, detail)
        self._checkpoint(f"{self.run_summary} / {position} 다운로드 중 / {merged.display_title}")
        self.events.put(("item_status", row_id, "다운로드"))

        try:
            output = download_media(
                merged,
                str(options.get("download_dir", "")),
                str(options.get("quality", "가능한 최고화질")),
                date_basis,
                prefer_ytdlp=bool(options.get("prefer_ytdlp", True)),
                log=lambda msg: self.events.put(("log", msg)),
                should_stop=self.stop_event.is_set,
                force=True,
                tools=options.get("tools"),
                fast_verify=bool(options.get("fast_verify", True)),
            )
        except DownloadCancelled:
            raise
        except Exception as first_exc:  # noqa: BLE001 - one alternate recovery pass.
            self.events.put(("log", f"기본 재다운로드 실패: {first_exc}"))
            self.events.put(("log", "대체 방식으로 재시도합니다: 상세 정보 재조회 + ffmpeg 우선"))
            detail = self._get_media_for_retry(client, merged, force_playwright=True, use_playwright=True)
            merged = self._merge_item(merged, detail)
            output = download_media(
                merged,
                str(options.get("download_dir", "")),
                str(options.get("quality", "가능한 최고화질")),
                date_basis,
                prefer_ytdlp=False,
                log=lambda msg: self.events.put(("log", msg)),
                should_stop=self.stop_event.is_set,
                force=True,
                tools=options.get("tools"),
                fast_verify=bool(options.get("fast_verify", True)),
            )

        result_status = "재다운완료" if output.repaired else "완료"
        self._record_session(result_status, "재다운로드", merged, output_path=str(output.path))
        self.events.put(("output_path", row_id, str(output.path)))
        self.events.put(("item_status", row_id, result_status))
        self.events.put(("log", f"재다운로드 저장 완료: {output.path}"))

    def _get_media_for_retry(
        self,
        client: TVCFClient,
        item: MediaItem,
        force_playwright: bool = False,
        use_playwright: bool = True,
    ) -> MediaItem:
        if not force_playwright:
            cached = self._cached_media(item)
            if cached:
                return cached

        identifiers = [
            item.nidx or item.play_url or item.idx,
            item.play_url,
            item.source_page,
            item.nidx,
            item.idx,
            item.mcode,
        ]
        errors: list[str] = []
        seen: set[str] = set()
        for identifier in identifiers:
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            if not force_playwright:
                cached = self._cached_media(identifier)
                if cached:
                    return cached
            try:
                detail = client.get_media(
                    identifier,
                    use_playwright_fallback=force_playwright or use_playwright,
                    log=lambda msg: self.events.put(("log", msg)),
                )
                self._remember_media_detail(item, detail, identifier)
                return detail
            except Exception as exc:  # noqa: BLE001 - try another identifier.
                errors.append(f"{identifier}: {exc}")

        raise TVCFError("; ".join(errors) if errors else "상세 정보를 찾지 못했습니다.")

    def _get_media_detail(
        self,
        client: TVCFClient,
        item: MediaItem,
        use_playwright: bool,
    ) -> MediaItem:
        cached = self._cached_media(item)
        if cached:
            return cached

        identifier = item.nidx or item.play_url or item.idx
        detail = client.get_media(
            identifier,
            use_playwright_fallback=use_playwright,
            log=lambda msg: self.events.put(("log", msg)),
        )
        self._remember_media_detail(item, detail, identifier)
        return detail

    def _cached_media(self, value: MediaItem | str) -> MediaItem | None:
        now = time.monotonic()
        keys = self._media_cache_keys(value)
        expired: list[str] = []
        with self.media_cache_lock:
            for key in keys:
                cached = self.media_cache.get(key)
                if not cached:
                    continue
                item, cached_at = cached
                if now - cached_at <= MEDIA_CACHE_TTL_SECONDS:
                    return item
                expired.append(key)
            for key in expired:
                self.media_cache.pop(key, None)
        return None

    def _remember_media_detail(self, base: MediaItem | None, detail: MediaItem, *extra_keys: str) -> None:
        now = time.monotonic()
        keys = [
            *self._media_cache_keys(base),
            *self._media_cache_keys(detail),
            *self._media_cache_keys(*extra_keys),
        ]
        with self.media_cache_lock:
            for key in keys:
                self.media_cache[key] = (detail, now)

    def _media_cache_keys(self, *values: MediaItem | str | None) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            candidates: list[str]
            if isinstance(value, MediaItem):
                candidates = [
                    value.nidx,
                    value.idx,
                    value.mcode,
                    value.play_url,
                    value.source_page,
                ]
            else:
                candidates = [str(value)]
            for candidate in candidates:
                key = candidate.strip()
                if key and key not in seen:
                    keys.append(key)
                    seen.add(key)
        return keys

    def _save_error_case(
        self,
        row_id: str,
        item: MediaItem | None,
        stage: str,
        exc: BaseException | str,
        options: dict[str, object] | None = None,
    ) -> str:
        category = classify_error(stage, exc)
        if options:
            download_dir = str(options.get("download_dir", ""))
            quality = str(options.get("quality", "가능한 최고화질"))
            date_basis = str(options.get("date_basis", "published"))
        else:
            download_dir = self.download_dir_var.get()
            quality = self.quality_var.get()
            date_basis = self._date_basis_value()
        context = {
            "run_summary": self.run_summary,
            "last_checkpoint": self.last_checkpoint,
            "download_dir": download_dir,
            "quality": quality,
            "date_basis": date_basis,
            "file_date": item.date_label(date_basis) if item else "",
            "playwright_fallback_count": self.playwright_fallback_count,
            "retry_policy": "network/timeout/HTTP 5xx up to 3, HTTP 429 delayed retry, HTTP 403 one fallback, HTTP 404 no retry",
        }
        try:
            error_path = save_error_case(item, stage, exc, context)
            if row_id:
                self.events.put(("error_case", row_id, str(error_path)))
            self.events.put(("log", f"오류 로그 저장: {error_path}"))
            self._record_session(category, stage, item, str(exc), error_path=str(error_path))
        except Exception as save_exc:  # noqa: BLE001 - logging failure must not stop downloads.
            self.events.put(("log", f"오류 로그 저장 실패: {save_exc}"))
            self._record_session(category, stage, item, str(exc))
        return category

    def _record_session(
        self,
        status: str,
        stage: str,
        item: MediaItem | None = None,
        message: str = "",
        output_path: str = "",
        error_path: str = "",
    ) -> None:
        if not self.session_log:
            return
        try:
            with self.session_lock:
                self.session_log.add(
                    status=status,
                    stage=stage,
                    item=item,
                    message=message,
                    output_path=output_path,
                    error_path=error_path,
                )
        except OSError:
            pass

    def download(self) -> None:
        self._start_worker(download=True)

    def stop(self) -> None:
        self.stop_requested_by_user = True
        self.stop_event.set()
        self._log(f"중지 요청: {self.last_checkpoint}")

    def _start_worker(self, download: bool) -> None:
        if self.worker and self.worker.is_alive():
            self._log("이미 작업이 진행 중입니다.")
            return

        self.stop_event.clear()
        self.stop_requested_by_user = False
        self._save_current_config()
        self.run_summary = self._build_run_summary()
        self.session_log = SessionLog(self.run_summary)
        self.run_started_at = time.monotonic()
        self.playwright_fallback_count = 0
        self.completion_actions_ran = False
        self._reset_speed_metrics()
        self.elapsed_time_var.set("경과 시간: 0초")
        self.eta_var.set("남은 시간: 계산 중")
        self.summary_stats_var.set("완료 0 / 오류 0 / 건너뜀 0 / Playwright 0회")
        self._set_checkpoint(f"작업 준비: {self.run_summary}")
        self._clear_items()
        self.progress.configure(value=0, maximum=1)
        self._set_progress_text(0, 0)
        self.elapsed_time_var.set("경과 시간: 0초")
        self._set_status("작업 준비 중")

        options = self._download_options_snapshot()
        self.worker = threading.Thread(target=self._worker_run, args=(download, options), daemon=True)
        self.worker.start()

    def _download_options_snapshot(self) -> dict[str, object]:
        return {
            "date_from": self.date_from_var.get().strip(),
            "date_to": self.date_to_var.get().strip(),
            "date_basis": self._date_basis_value(),
            "download_dir": self.download_dir_var.get(),
            "quality": self.quality_var.get(),
            "parallel": self._normalize_parallel(self.parallel_var.get()),
            "prefer_ytdlp": True,
            "use_playwright_fallback": True,
            "fast_verify": True,
        }

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

            retry_failed_count = self._drain_retry_queue(client, options)
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

    @staticmethod
    def _merge_item(base: MediaItem, detail: MediaItem) -> MediaItem:
        return MediaItem(
            idx=detail.idx or base.idx,
            nidx=detail.nidx or base.nidx,
            mcode=detail.mcode or base.mcode,
            title=detail.title or base.title,
            chapter=detail.chapter or base.chapter,
            brand=detail.brand or base.brand,
            published_date=detail.published_date or base.published_date,
            registered_date=detail.registered_date or base.registered_date,
            country_code=detail.country_code or base.country_code,
            category_code=detail.category_code or base.category_code,
            category_name=detail.category_name or base.category_name,
            duration=detail.duration or base.duration,
            play_url=detail.play_url or base.play_url,
            source_page=detail.source_page or base.source_page,
            stream_urls=detail.stream_urls or base.stream_urls,
        )

    def _poll_events(self) -> None:
        processed = 0
        while processed < MAX_GUI_EVENTS_PER_TICK:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
            processed += 1

        self._update_elapsed_time()
        delay = 20 if processed >= MAX_GUI_EVENTS_PER_TICK else 120
        self.root.after(delay, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self._log(event[1])
        elif kind == "status":
            self._set_status(event[1])
            self._log(event[1])
            if self._is_terminal_status(event[1]):
                self._update_elapsed_time(force=True)
            if self._is_completion_status(event[1]):
                self.root.after(250, lambda status=event[1]: self._maybe_run_completion_actions(status))
        elif kind == "items":
            self.items = event[1]
            self._show_items(self.items)
            self._set_status(f"대상 {len(self.items)}개")
            self._set_progress_text(0, len(self.items))
        elif kind == "item_status":
            self._set_item_status(event[1], event[2])
        elif kind == "progress_max":
            self.progress.configure(maximum=event[1], value=0)
            self._set_progress_text(0, event[1])
        elif kind == "progress":
            self.progress.configure(value=event[1])
            self._set_progress_text(event[1], int(float(self.progress.cget("maximum"))))
        elif kind == "progress_step":
            current = int(float(self.progress.cget("value"))) + int(event[1])
            maximum = int(float(self.progress.cget("maximum")))
            self.progress.configure(value=min(current, maximum))
            self._set_progress_text(current, maximum)
        elif kind == "checkpoint":
            self._set_checkpoint(event[1])
        elif kind == "error_case":
            self.error_case_by_id[event[1]] = event[2]
        elif kind == "output_path":
            self._set_row_output_path(event[1], event[2])

    def _show_items(self, items: list[MediaItem]) -> None:
        self.item_by_id = {}
        self.row_records = {}
        self.row_order = []
        for row_index, item in enumerate(items):
            item_id = item.nidx or item.idx or item.mcode
            if not item_id:
                continue
            self.item_by_id[item_id] = item
            self.row_order.append(item_id)
            self.row_records[item_id] = {
                "item": item,
                "date": item.date_label(self._date_basis_value()),
                "title": item.display_title,
                "id": item_id,
                "status": "대기",
                "saved_path": "",
                "row_index": row_index,
            }
        self._render_tree()

    def _render_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        self._clear_tree()
        visible_index = 0
        for row_id in self.row_order:
            record = self.row_records.get(row_id)
            if not record or not self._record_matches_filters(record):
                continue
            status = record.get("status", "대기")
            self.tree.insert(
                "",
                "end",
                iid=row_id,
                values=(
                    "☑" if row_id in self.checked_rows else "☐",
                    record.get("date", ""),
                    record.get("title", ""),
                    record.get("id", row_id),
                    status,
                ),
                tags=self._row_tags(status, visible_index),
            )
            visible_index += 1
        self._update_summary_stats()

    def _filters_active(self) -> bool:
        return self.status_filter_var.get() != "전체" or bool(self.search_var.get().strip())

    def _record_matches_filters(self, record: dict) -> bool:
        status_filter = self.status_filter_var.get()
        status = str(record.get("status", ""))
        if status_filter != "전체":
            if status_filter == "완료" and status not in DONE_STATUSES:
                return False
            if status_filter == "오류" and status not in ERROR_STATUSES:
                return False
            if status_filter == "다운로드 중" and status not in ACTIVE_STATUSES:
                return False
            if status_filter in {"보류", "중단됨", "건너뜀"} and status != status_filter:
                return False

        keyword = self.search_var.get().strip().casefold()
        if not keyword:
            return True
        haystack = " ".join(
            str(record.get(key, ""))
            for key in ("date", "title", "id", "status", "saved_path")
        ).casefold()
        return keyword in haystack

    def _row_tags(self, status: str, row_index: int) -> tuple[str, ...]:
        base = "even" if row_index % 2 else "odd"
        status_tag = self._status_tag(status)
        return (base, status_tag) if status_tag else (base,)

    def _set_item_status(self, item_id: str, status: str) -> None:
        if not item_id:
            return
        record = self.row_records.get(item_id)
        if record:
            record["status"] = status
        if self._filters_active():
            self._render_tree()
            return
        if self.tree.exists(item_id):
            values = list(self.tree.item(item_id, "values"))
            if len(values) >= 5:
                values[4] = status
                self.tree.item(item_id, values=values, tags=self._row_tags(status, self.tree.index(item_id)))
        self._update_summary_stats()

    def _refresh_row_stripes(self) -> None:
        self._render_tree()

    def _clear_items(self, clear_log: bool = True) -> None:
        self.items = []
        self.item_by_id = {}
        self.row_records = {}
        self.row_order = []
        self.checked_rows.clear()
        self.error_case_by_id.clear()
        with self.retry_lock:
            self.retry_queue.clear()
            self.queued_retry_rows.clear()
        self._clear_tree()
        if clear_log:
            self._clear_log()

    def _clear_download_list(self) -> None:
        if self.worker and self.worker.is_alive():
            self._log("작업 중에는 다운로드 목록을 지울 수 없습니다.")
            return

        self._clear_items(clear_log=False)
        self.search_var.set("")
        self.status_filter_var.set("전체")
        self.progress.configure(value=0, maximum=1)
        self._set_progress_text(0, 0)
        self._reset_speed_metrics()
        self.elapsed_time_var.set("경과 시간: 0초")
        self.eta_var.set("남은 시간: 계산 중")
        self.summary_stats_var.set("완료 0 / 오류 0 / 건너뜀 0 / Playwright 0회")
        self._set_checkpoint("다운로드 목록을 지웠습니다.")
        self._log("다운로드 목록을 지웠습니다.")

    def _clear_tree(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

    def _clear_log(self) -> None:
        if hasattr(self, "log_text"):
            self.log_text.delete("1.0", "end")
        self.log_line_count = 0

    def _log(self, message: str) -> None:
        message = decode_output(message)
        if len(message) > MAX_LOG_MESSAGE_CHARS:
            message = f"{message[:900]} ... [긴 로그 생략] ... {message[-200:]}"
        self._parse_download_metrics(message)
        if "Playwright fallback 사용" in message:
            self.playwright_fallback_count += 1
            self._update_summary_stats()
        timestamp = datetime.now().strftime("%H:%M:%S")
        level = self._log_level(message)
        self.log_text.insert("end", f"[{timestamp}]  ", "time")
        self.log_text.insert("end", f"[{level}]  ", level)
        self.log_text.insert("end", f"{message}\n", "message")
        self.log_line_count += 1
        if self.log_line_count > MAX_VISIBLE_LOG_LINES:
            self.log_text.delete("1.0", f"{LOG_TRIM_LINES + 1}.0")
            self.log_line_count -= LOG_TRIM_LINES
        self.log_text.see("end")

    @staticmethod
    def _log_level(message: str) -> str:
        if any(word in message for word in ("오류", "실패", "중단", "Error", "ERROR")):
            return "error"
        if any(word in message for word in ("제외", "없습니다", "손상", "삭제", "경고")):
            return "warn"
        return "info"

    def _parse_download_metrics(self, message: str) -> None:
        now = time.monotonic()
        can_update = now - self.last_metric_update >= 0.35

        yt_match = re.search(
            r"\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%.*?(?:at\s+(?P<speed>\S+/s))?.*?(?:ETA\s+(?P<eta>\S+))?",
            message,
        )
        if yt_match:
            if can_update:
                if yt_match.group("speed"):
                    self.speed_var.set(f"속도: {yt_match.group('speed')}")
                if yt_match.group("eta"):
                    self.eta_var.set(f"남은 시간: {yt_match.group('eta')}")
                self.last_metric_update = now
            return

        ffmpeg_size = re.search(r"\bL?size=\s*(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>[KMG]?i?B|[KMG]?B)", message)
        if not ffmpeg_size:
            return

        size_bytes = self._parse_size_bytes(ffmpeg_size.group("size"), ffmpeg_size.group("unit"))
        if size_bytes is None:
            return

        last_size = self.last_ffmpeg_size_bytes
        last_at = self.last_ffmpeg_size_at
        self.last_ffmpeg_size_bytes = size_bytes
        self.last_ffmpeg_size_at = now

        if last_size is None or last_at <= 0 or size_bytes <= last_size:
            return

        bytes_per_second = (size_bytes - last_size) / max(0.001, now - last_at)
        if can_update and bytes_per_second > 0:
            self.speed_var.set(f"속도: {self._format_bytes_per_second(bytes_per_second)}")
            self.last_metric_update = now

    def _reset_speed_metrics(self) -> None:
        self.last_metric_update = 0.0
        self.last_ffmpeg_size_bytes = None
        self.last_ffmpeg_size_at = 0.0
        self.speed_var.set("속도: 계산 중")

    @staticmethod
    def _parse_size_bytes(value: str, unit: str) -> float | None:
        try:
            number = float(value)
        except ValueError:
            return None
        multipliers = {
            "B": 1,
            "KB": 1000,
            "MB": 1000**2,
            "GB": 1000**3,
            "KiB": 1024,
            "MiB": 1024**2,
            "GiB": 1024**3,
        }
        multiplier = multipliers.get(unit)
        return number * multiplier if multiplier else None

    @staticmethod
    def _format_bytes_per_second(bytes_per_second: float) -> str:
        if bytes_per_second >= 1024**2:
            return f"{bytes_per_second / 1024**2:.2f} MiB/s"
        if bytes_per_second >= 1024:
            return f"{bytes_per_second / 1024:.1f} KiB/s"
        return f"{bytes_per_second:.0f} B/s"

    def _set_row_output_path(self, row_id: str, path: str) -> None:
        record = self.row_records.get(row_id)
        if record:
            record["saved_path"] = path
        if self._filters_active():
            self._render_tree()

    def _clear_search(self) -> None:
        self.search_var.set("")
        self._render_tree()

    def _open_download_dir(self) -> None:
        path = Path(self.download_dir_var.get().strip())
        if not path.exists() or not path.is_dir():
            messagebox.showwarning("저장 폴더 열기", "저장 경로가 없거나 존재하지 않습니다.")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("저장 폴더 열기", f"저장 폴더를 열 수 없습니다.\n{exc}")

    def _update_summary_stats(self) -> None:
        statuses = [str(record.get("status", "")) for record in self.row_records.values()]
        done = sum(1 for status in statuses if status in DONE_STATUSES)
        errors = sum(1 for status in statuses if status in ERROR_STATUSES)
        skipped = sum(1 for status in statuses if status == "건너뜀")
        total = len(statuses)
        processed = done + errors + skipped + sum(1 for status in statuses if status in {"한국 아님", "광고 아님"})
        self.summary_stats_var.set(
            f"완료 {done} / 오류 {errors} / 건너뜀 {skipped} / Playwright {self.playwright_fallback_count}회"
        )
        if total > 0 and processed > 0 and self.run_started_at:
            elapsed = max(1.0, time.monotonic() - self.run_started_at)
            remaining = max(0, total - processed)
            seconds = int(elapsed / processed * remaining)
            self.eta_var.set(f"남은 시간: {self._format_seconds(seconds)}")

    def _update_elapsed_time(self, force: bool = False) -> None:
        if not self.run_started_at:
            self.elapsed_time_var.set("경과 시간: 0초")
            return
        if not force and not (self.worker and self.worker.is_alive()):
            return
        elapsed = max(0, int(time.monotonic() - self.run_started_at))
        self.elapsed_time_var.set(f"경과 시간: {self._format_seconds(elapsed)}")

    @staticmethod
    def _format_seconds(seconds: int) -> str:
        if seconds <= 0:
            return "0초"
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        if hours:
            return f"{hours}시간 {minute}분"
        if minute:
            return f"{minute}분 {sec}초"
        return f"{sec}초"

    @staticmethod
    def _is_completion_status(status: str) -> bool:
        return status.startswith("다운로드 완료") or status.startswith("재다운로드 완료")

    @staticmethod
    def _is_terminal_status(status: str) -> bool:
        return (
            status.startswith("다운로드 완료")
            or status.startswith("재다운로드 완료")
            or status.startswith("대상 확인 완료")
            or status in {"대상 없음", "중단됨", "오류", "재다운로드 오류"}
        )

    def _maybe_run_completion_actions(self, status: str) -> None:
        if self.completion_actions_ran or not self._is_completion_status(status):
            return
        if self.stop_requested_by_user or self.stop_event.is_set():
            return
        self.completion_actions_ran = True

        done, errors, skipped = self._completion_counts()
        if self._completion_action_on("notify"):
            messagebox.showinfo(
                "작업 완료",
                f"작업이 완료되었습니다.\n\n완료: {done}개\n오류: {errors}개\n건너뜀: {skipped}개",
            )
        if self._completion_action_on("open_folder"):
            self._open_download_dir_from_completion()
        if self._completion_action_on("shutdown"):
            self._schedule_shutdown()

    def _completion_counts(self) -> tuple[int, int, int]:
        statuses = [str(record.get("status", "")) for record in self.row_records.values()]
        done = sum(1 for value in statuses if value in DONE_STATUSES)
        errors = sum(1 for value in statuses if value in ERROR_STATUSES)
        skipped = sum(1 for value in statuses if value == "건너뜀")
        return done, errors, skipped

    def _open_download_dir_from_completion(self) -> None:
        path = Path(self.download_dir_var.get().strip()).expanduser()
        if not path.exists() or not path.is_dir():
            self._log(f"경고: 저장 폴더를 열 수 없습니다. 경로가 없거나 접근할 수 없습니다: {path}")
            return
        if os.name != "nt":
            self._log("경고: 저장 폴더 열기는 Windows에서만 동작합니다.")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            self._log(f"경고: 저장 폴더를 열 수 없습니다: {exc}")

    def _schedule_shutdown(self) -> None:
        if os.name != "nt":
            self._log("경고: 전원 끄기 옵션은 Windows에서만 동작합니다.")
            return
        self._log("60초 후 전원이 꺼집니다. 취소하려면 Windows에서 shutdown /a를 실행하세요.")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.Popen(
                [
                    "shutdown",
                    "/s",
                    "/t",
                    "60",
                    "/c",
                    "TVCF Downloader completed. The PC will shut down in 60 seconds.",
                ],
                creationflags=creationflags,
            )
        except OSError as exc:
            self._log(f"경고: 전원 끄기 예약 실패: {exc}")

    def _checkpoint(self, message: str) -> None:
        self.last_checkpoint = message
        self.events.put(("checkpoint", message))

    def _set_checkpoint(self, message: str) -> None:
        self.last_checkpoint = message
        self.current_task_var.set(message)
        self.config["last_checkpoint"] = message
        now = time.monotonic()
        force_save = message.startswith(("작업 준비", "작업 완료", "재다운로드 대기열 완료"))
        if force_save or now - self.last_checkpoint_save_at >= 2.0:
            try:
                save_config(self.config)
                self.last_checkpoint_save_at = now
            except OSError:
                pass

    def _set_progress_text(self, value: int, maximum: int) -> None:
        if maximum <= 0:
            self.progress_text_var.set("0 / 0 (0%)")
            return
        current = min(max(0, value), maximum)
        percent = current / maximum * 100
        self.progress_text_var.set(f"{current} / {maximum} ({percent:.1f}%)")

    def _set_status(self, status: str) -> None:
        self.status_var.set(status)
        self.status_badge_var.set(f"● {status}")
        if hasattr(self, "status_label"):
            self.status_label.configure(style=self._status_style(status))

    @staticmethod
    def _status_style(status: str) -> str:
        if any(word in status for word in ("오류", "중단")):
            return "Badge.Error.TLabel"
        if any(word in status for word in ("완료", "대상 없음")):
            return "Badge.Done.TLabel"
        if any(word in status for word in ("준비", "확인", "다운로드", "대상", "재시도", "대기열")):
            return "Badge.Working.TLabel"
        return "Badge.Idle.TLabel"

    @staticmethod
    def _status_tag(status: str) -> str:
        if status in {"완료", "재다운완료"}:
            return "done"
        if status in {"건너뜀", "대기"}:
            return "skip"
        if status in {"상세 확인", "다운로드", "재시도"}:
            return "active"
        if status in {"한국 아님", "광고 아님", "대기열에 다시 추가됨"}:
            return "warning"
        if status in ERROR_STATUSES or status == "중단됨":
            return "error"
        return ""

    @staticmethod
    def _normalize_quality(value: object) -> str:
        text = str(value or "").strip()
        return text if text in QUALITY_OPTIONS else "가능한 최고화질"

    def _date_basis_value(self) -> str:
        return self._normalize_date_basis(self.date_basis_label_var.get())

    @staticmethod
    def _date_basis_label(value: object) -> str:
        return DATE_BASIS_LABELS[DownloaderApp._normalize_date_basis(value)]

    @staticmethod
    def _normalize_date_basis(value: object) -> str:
        text = str(value or "").strip()
        if text in DATE_BASIS_LABELS:
            return text
        if text in DATE_BASIS_VALUES:
            return DATE_BASIS_VALUES[text]
        return "published"

    @staticmethod
    def _normalize_parallel(value: object) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 3
        return min(6, max(1, number))

    def _completion_config_value(self, action: str) -> str:
        saved_actions = self.config.get("completion_actions")
        if isinstance(saved_actions, dict):
            return self._normalize_completion_action(saved_actions.get(action))
        legacy_key = COMPLETION_ACTION_KEYS.get(action, "")
        if legacy_key:
            return self._normalize_completion_action(self.config.get(legacy_key))
        return "off"

    @staticmethod
    def _normalize_completion_action(value: object) -> str:
        text = str(value or "").strip().casefold()
        return "on" if text in {"on", "true", "1", "yes", "y"} else "off"

    @staticmethod
    def _completion_label(action: str, value: str) -> str:
        options = COMPLETION_ACTION_OPTIONS[action]
        return options[1] if value == "on" else options[0]

    @staticmethod
    def _completion_value_from_label(action: str, label: str) -> str:
        options = COMPLETION_ACTION_OPTIONS[action]
        return "on" if label == options[1] else "off"

    def _completion_action_on(self, action: str) -> bool:
        if action == "notify":
            label = self.completion_notify_var.get()
        elif action == "open_folder":
            label = self.completion_open_folder_var.get()
        else:
            label = self.completion_shutdown_var.get()
        return self._completion_value_from_label(action, label) == "on"

    def _sync_wraplength(self, event: object | None = None) -> None:
        if not hasattr(self, "current_task_label"):
            return
        try:
            if not self.current_task_label.winfo_exists():
                return
            container_width = self.current_task_label.master.winfo_width()
        except Exception:  # noqa: BLE001 - resize callbacks can fire while the window is closing.
            return
        if container_width <= 1:
            container_width = self.root.winfo_width() - 420
        width = max(360, container_width - 32)
        self.current_task_label.configure(wraplength=width)

    def _build_run_summary(self) -> str:
        return (
            f"{self.date_from_var.get()}~{self.date_to_var.get()} / "
            f"기준 {self.date_basis_label_var.get()} / "
            f"화질 {self.quality_var.get()} / 병렬 {self._normalize_parallel(self.parallel_var.get())} / "
            f"저장 {self.download_dir_var.get()}"
        )

    def _item_position_text(self, index: int, total: int, item: MediaItem, date_basis: str | None = None) -> str:
        item_date = item.date_value(date_basis or self._date_basis_value())
        if item_date:
            return f"{item_date}, {index}/{total}번째"

        return f"{index}/{total}번째"

    def _choose_download_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.download_dir_var.get() or str(Path.home()))
        if selected:
            self.download_dir_var.set(selected)
            self._save_current_config()

    def _save_current_config(self) -> None:
        self.config.update(
            {
                "download_dir": self.download_dir_var.get(),
                "date_from": self.date_from_var.get(),
                "date_to": self.date_to_var.get(),
                "date_basis": self._date_basis_value(),
                "quality": self.quality_var.get(),
                "max_pages": 0,
                "parallel_downloads": self._normalize_parallel(self.parallel_var.get()),
                "completion_actions": {
                    "notify": self._completion_value_from_label("notify", self.completion_notify_var.get()),
                    "open_folder": self._completion_value_from_label("open_folder", self.completion_open_folder_var.get()),
                    "shutdown": self._completion_value_from_label("shutdown", self.completion_shutdown_var.get()),
                },
                "prefer_ytdlp": True,
                "use_playwright_fallback": True,
                "fast_verify": True,
                "last_checkpoint": self.last_checkpoint,
            }
        )
        save_config(self.config)

    def _on_close(self) -> None:
        self._save_current_config()
        flush_history()
        self.stop_event.set()
        self.root.destroy()


def run_app() -> None:
    root = Tk()
    DownloaderApp(root)
    root.mainloop()
