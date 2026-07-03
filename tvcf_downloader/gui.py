import queue
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, Canvas, Frame, IntVar, StringVar, Tk, filedialog
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .client import TVCFClient, TVCFError
from .config import load_config, save_config
from .downloader import DownloadCancelled, download_media
from .models import MediaItem


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

DEFERRED_RETRY_STATUSES = {"오류", "중단됨", "보류"}


class DownloaderApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("TVCF 한국 광고 다운로더")
        self.root.geometry("1280x900")
        self.root.minsize(1080, 760)
        self.root.configure(bg=COLORS["bg"])
        self.root.option_add("*Font", "{Malgun Gothic} 10")

        self.config = load_config()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.items: list[MediaItem] = []
        self.item_by_id: dict[str, MediaItem] = {}
        self.checked_rows: set[str] = set()
        self.retry_queue: deque[tuple[str, MediaItem]] = deque()
        self.queued_retry_rows: set[str] = set()
        self.retry_lock = threading.Lock()
        self.run_summary = ""
        self.last_checkpoint = self.config.get("last_checkpoint", "작업 없음")

        today = datetime.now().date()
        default_from = today - timedelta(days=30)

        self.mode_var = StringVar(value="period")
        self.download_dir_var = StringVar(value=self.config.get("download_dir", ""))
        self.date_from_var = StringVar(value=self.config.get("date_from", default_from.strftime("%Y-%m-%d")))
        self.date_to_var = StringVar(value=self.config.get("date_to", today.strftime("%Y-%m-%d")))
        self.date_basis_var = StringVar(value=self.config.get("date_basis", "published"))
        self.id_start_var = StringVar(value=self.config.get("id_start", ""))
        self.id_end_var = StringVar(value=self.config.get("id_end", ""))
        self.quality_var = StringVar(value=self.config.get("quality", "HD"))
        self.max_pages_var = IntVar(value=int(self.config.get("max_pages", 0)))
        self.prefer_ytdlp_var = BooleanVar(value=bool(self.config.get("prefer_ytdlp", True)))
        self.playwright_var = BooleanVar(value=bool(self.config.get("use_playwright_fallback", True)))
        self.status_var = StringVar(value="대기 중")
        self.status_badge_var = StringVar(value="● 대기 중")
        self.current_task_var = StringVar(value=self.last_checkpoint)
        self.progress_text_var = StringVar(value="0 / 0")

        self._configure_style()
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
        self.style.configure("Accent.TButton", background=COLORS["accent"], foreground="#ffffff", bordercolor=COLORS["accent"])
        self.style.map("Accent.TButton", background=[("active", COLORS["accent_dark"])], foreground=[("active", "#ffffff")])
        self.style.configure("Danger.TButton", background=COLORS["danger_soft"], foreground=COLORS["danger"], bordercolor="#f5b4b4")
        self.style.map("Danger.TButton", background=[("active", "#ffd2d2")])
        self.style.configure("Text.TButton", padding=(8, 4), background=COLORS["surface"], foreground=COLORS["muted"], bordercolor=COLORS["surface"])
        self.style.map("Text.TButton", background=[("active", "#f2f5fa")], foreground=[("active", COLORS["text"])])

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
        outer.rowconfigure(6, weight=3, minsize=150)
        outer.rowconfigure(7, weight=2, minsize=130)

        self._build_header(outer)
        self._build_path_card(outer)
        self._build_target_card(outer)
        self._build_options_card(outer)
        self._build_actions(outer)
        self._build_checkpoint_card(outer)
        self._build_list_card(outer)
        self._build_log_card(outer)
        self._build_progress_card(outer)

        self.root.bind("<Configure>", self._sync_wraplength)

    def _build_header(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="Main.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(1, weight=1)

        logo = Canvas(header, width=34, height=34, bg=COLORS["bg"], highlightthickness=0)
        logo.grid(row=0, column=0, rowspan=2, sticky="w", padx=(2, 14))
        logo.create_polygon(5, 5, 17, 11, 17, 25, 5, 31, fill=COLORS["accent_dark"], outline="")
        logo.create_polygon(17, 4, 31, 12, 18, 19, fill="#2f8de4", outline="")
        logo.create_polygon(18, 20, 31, 27, 17, 32, fill="#52a6ff", outline="")
        logo.create_line(17, 10, 17, 27, fill="#ffffff", width=2)

        ttk.Label(header, text="TVCF 한국 광고 다운로더", style="HeaderTitle.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(
            header,
            text="기간 또는 ID 범위로 한국 광고 영상을 수집하고 다운로드합니다.",
            style="HeaderSub.TLabel",
        ).grid(row=1, column=1, sticky="w", pady=(3, 0))
        self.status_label = ttk.Label(header, textvariable=self.status_badge_var, style="Badge.Idle.TLabel", anchor="center")
        self.status_label.grid(row=0, column=2, rowspan=2, sticky="e", padx=(16, 4))

    def _build_path_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=1)
        card.columnconfigure(1, weight=1)

        self._section_title(card, "저장 위치", "folder").grid(row=0, column=0, sticky="w", padx=(18, 16), pady=14)
        ttk.Entry(card, textvariable=self.download_dir_var, style="Input.TEntry").grid(row=0, column=1, sticky="ew", pady=14)
        ttk.Button(card, text="폴더 선택", command=self._choose_download_dir).grid(row=0, column=2, sticky="e", padx=(14, 18), pady=14)

    def _build_target_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=2)
        card.columnconfigure(0, weight=1)

        self._section_title(card, "다운로드 대상", "target").grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4))

        body = ttk.Frame(card, style="Surface.TFrame")
        body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        for idx in (2, 4, 6, 8):
            body.columnconfigure(idx, weight=1)

        ttk.Radiobutton(body, text="기간", value="period", variable=self.mode_var).grid(row=0, column=0, sticky="w", padx=(0, 18), pady=6)
        ttk.Label(body, text="시작").grid(row=0, column=1, sticky="e", padx=(0, 8), pady=6)
        ttk.Entry(body, width=14, textvariable=self.date_from_var, style="Input.TEntry").grid(row=0, column=2, sticky="ew", padx=(0, 30), pady=6)
        ttk.Label(body, text="끝").grid(row=0, column=3, sticky="e", padx=(0, 8), pady=6)
        ttk.Entry(body, width=14, textvariable=self.date_to_var, style="Input.TEntry").grid(row=0, column=4, sticky="ew", padx=(0, 30), pady=6)
        ttk.Label(body, text="기준").grid(row=0, column=5, sticky="e", padx=(0, 8), pady=6)
        ttk.Combobox(
            body,
            width=13,
            state="readonly",
            textvariable=self.date_basis_var,
            values=("published", "registered"),
            style="Input.TCombobox",
        ).grid(row=0, column=6, sticky="ew", padx=(0, 30), pady=6)
        ttk.Label(body, text="최대 페이지(0=자동)").grid(row=0, column=7, sticky="e", padx=(0, 8), pady=6)
        ttk.Spinbox(body, from_=0, to=500, width=7, textvariable=self.max_pages_var, style="Input.TSpinbox").grid(
            row=0,
            column=8,
            sticky="ew",
            pady=6,
        )

        ttk.Radiobutton(body, text="ID 범위", value="id", variable=self.mode_var).grid(row=1, column=0, sticky="w", padx=(0, 18), pady=6)
        ttk.Label(body, text="시작 ID").grid(row=1, column=1, sticky="e", padx=(0, 8), pady=6)
        ttk.Entry(body, width=16, textvariable=self.id_start_var, style="Input.TEntry").grid(row=1, column=2, sticky="ew", padx=(0, 30), pady=6)
        ttk.Label(body, text="끝 ID").grid(row=1, column=3, sticky="e", padx=(0, 8), pady=6)
        ttk.Entry(body, width=16, textvariable=self.id_end_var, style="Input.TEntry").grid(row=1, column=4, sticky="ew", padx=(0, 30), pady=6)

    def _build_options_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=3)
        card.columnconfigure(0, weight=1)

        self._section_title(card, "다운로드 옵션", "gear").grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4))

        body = ttk.Frame(card, style="Surface.TFrame")
        body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        body.columnconfigure(5, weight=1)

        ttk.Label(body, text="화질").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Combobox(
            body,
            width=14,
            state="readonly",
            textvariable=self.quality_var,
            values=("HD", "SD", "mobile"),
            style="Input.TCombobox",
        ).grid(row=0, column=1, sticky="w", padx=(0, 36))
        ttk.Checkbutton(body, text="yt-dlp 우선", variable=self.prefer_ytdlp_var).grid(row=0, column=2, sticky="w", padx=(0, 28))
        ttk.Checkbutton(body, text="Playwright fallback", variable=self.playwright_var).grid(row=0, column=3, sticky="w", padx=(0, 28))

    def _build_actions(self, parent: ttk.Frame) -> None:
        action = ttk.Frame(parent, style="Main.TFrame")
        action.grid(row=4, column=0, sticky="ew", pady=(8, 10))
        action.columnconfigure(3, weight=1)

        ttk.Button(action, text="미리보기", command=self.preview).grid(row=0, column=0, padx=(0, 10))
        ttk.Button(action, text="다운로드 시작", command=self.download, style="Accent.TButton").grid(row=0, column=1, padx=(0, 10))
        ttk.Button(action, text="중지", command=self.stop, style="Danger.TButton").grid(row=0, column=2)
        ttk.Label(action, text="현재 상태", style="Muted.TLabel").grid(row=0, column=4, sticky="e", padx=(0, 10))
        ttk.Label(action, textvariable=self.status_var, style="Muted.TLabel").grid(row=0, column=5, sticky="e")

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
        card.rowconfigure(1, weight=1, minsize=120)

        header = ttk.Frame(card, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 6))
        header.columnconfigure(0, weight=1)
        self._section_title(header, "다운로드 목록", "list").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="체크 재다운로드", command=self._retry_selected_item, style="Text.TButton").grid(row=0, column=1, sticky="e", padx=(0, 8))
        ttk.Button(header, text="보류 일괄 재다운로드", command=self._retry_deferred_items, style="Text.TButton").grid(row=0, column=2, sticky="e")

        table_wrap = ttk.Frame(card, style="Surface.TFrame")
        table_wrap.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 12))
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
        self.tree.column("title", width=670, anchor="w")
        self.tree.column("id", width=170, anchor="center", stretch=False)
        self.tree.column("status", width=170, anchor="center", stretch=False)
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

    def _build_log_card(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=7)
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
        self.progress.grid(row=0, column=1, sticky="ew", pady=13)
        ttk.Label(card, textvariable=self.progress_text_var, style="ProgressCount.TLabel").grid(row=0, column=2, sticky="e", padx=(18, 18), pady=13)

    def _card(self, parent: ttk.Frame, row: int) -> Frame:
        card = Frame(
            parent,
            bg=COLORS["surface"],
            bd=0,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"],
            highlightthickness=1,
        )
        card.grid(row=row, column=0, sticky="nsew", pady=(0, 10))
        return card

    def _section_title(self, parent: object, text: str, icon: str) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Surface.TFrame")
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
        if not self.tree.exists(row_id):
            return
        values = list(self.tree.item(row_id, "values"))
        if not values:
            return
        if row_id in self.checked_rows:
            self.checked_rows.remove(row_id)
            values[0] = "☐"
        else:
            self.checked_rows.add(row_id)
            values[0] = "☑"
        self.tree.item(row_id, values=values)

    def _queue_checked_retries(self) -> None:
        row_ids = [row_id for row_id in self.tree.get_children() if row_id in self.checked_rows]
        self._queue_retry_rows(row_ids, "재다운로드할 항목을 체크해주세요.")

    def _retry_deferred_items(self) -> None:
        row_ids = [
            row_id
            for row_id in self.tree.get_children()
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
                self.tree.move(row_id, "", "end")
                self._refresh_row_stripes()
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
        self.progress.configure(value=0, maximum=max(1, added))
        self.progress_text_var.set(f"0 / {added}")
        self._log(f"재다운로드 대기열에 {added}개 항목을 추가했습니다.")
        self._set_status("재다운로드 대기열 실행 중")
        self.worker = threading.Thread(target=self._retry_queue_worker_run, daemon=True)
        self.worker.start()

    def _retry_selected_item(self) -> None:
        self._queue_checked_retries()

    def _row_status(self, row_id: str) -> str:
        if not self.tree.exists(row_id):
            return ""
        values = list(self.tree.item(row_id, "values"))
        return str(values[4]) if len(values) >= 5 else ""

    def _set_row_checked(self, row_id: str, checked: bool) -> None:
        if not self.tree.exists(row_id):
            return
        values = list(self.tree.item(row_id, "values"))
        if not values:
            return
        if checked:
            self.checked_rows.add(row_id)
            values[0] = "☑"
        else:
            self.checked_rows.discard(row_id)
            values[0] = "☐"
        self.tree.item(row_id, values=values)

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

    def _retry_queue_worker_run(self) -> None:
        try:
            client = TVCFClient()
            failed_count = self._drain_retry_queue(client)
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

    def _drain_retry_queue(self, client: TVCFClient) -> int:
        failed_count = 0
        while True:
            with self.retry_lock:
                if not self.retry_queue:
                    break
                row_id, item = self.retry_queue.popleft()

            if self.stop_event.is_set():
                raise DownloadCancelled("사용자 중단 요청으로 재다운로드 대기열을 중단했습니다.")

            try:
                self._download_retry_item(client, row_id, item)
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - keep draining retry queue.
                failed_count += 1
                self.events.put(("item_status", row_id, "오류"))
                self.events.put(("log", f"재다운로드 실패 - 건너뜀: {item.display_title} / {exc}"))
            finally:
                with self.retry_lock:
                    self.queued_retry_rows.discard(row_id)
                self.events.put(("progress_step", 1))

        return failed_count

    def _download_retry_item(self, client: TVCFClient, row_id: str, item: MediaItem) -> None:
        label = item.display_title
        position = self._item_position_text(1, 1, item)

        self._checkpoint(f"{self.run_summary} / {position} 상세 확인 중 / {label}")
        self.events.put(("item_status", row_id, "상세 확인"))
        detail = self._get_media_for_retry(client, item)

        if detail.country_code and detail.country_code != "410":
            self.events.put(("item_status", row_id, "한국 아님"))
            self.events.put(("log", f"재다운로드 제외: 한국 광고가 아닙니다 / {label}"))
            return
        if detail.category_code and detail.category_code != "1":
            self.events.put(("item_status", row_id, "광고 아님"))
            self.events.put(("log", f"재다운로드 제외: 광고 카테고리가 아닙니다 / {label}"))
            return

        merged = self._merge_item(item, detail)
        self._checkpoint(f"{self.run_summary} / {position} 다운로드 중 / {merged.display_title}")
        self.events.put(("item_status", row_id, "다운로드"))

        try:
            output = download_media(
                merged,
                self.download_dir_var.get(),
                self.quality_var.get(),
                self.date_basis_var.get(),
                prefer_ytdlp=self.prefer_ytdlp_var.get(),
                log=lambda msg: self.events.put(("log", msg)),
                should_stop=self.stop_event.is_set,
                force=True,
            )
        except DownloadCancelled:
            raise
        except Exception as first_exc:  # noqa: BLE001 - one alternate recovery pass.
            self.events.put(("log", f"기본 재다운로드 실패: {first_exc}"))
            self.events.put(("log", "대체 방식으로 재시도합니다: 상세 정보 재조회 + ffmpeg 우선"))
            detail = self._get_media_for_retry(client, merged, force_playwright=True)
            merged = self._merge_item(merged, detail)
            output = download_media(
                merged,
                self.download_dir_var.get(),
                self.quality_var.get(),
                self.date_basis_var.get(),
                prefer_ytdlp=False,
                log=lambda msg: self.events.put(("log", msg)),
                should_stop=self.stop_event.is_set,
                force=True,
            )

        result_status = "재다운완료" if output.repaired else "완료"
        self.events.put(("item_status", row_id, result_status))
        self.events.put(("log", f"재다운로드 저장 완료: {output.path}"))

    def _get_media_for_retry(self, client: TVCFClient, item: MediaItem, force_playwright: bool = False) -> MediaItem:
        identifiers = [
            item.nidx or item.play_url or item.idx,
            item.play_url,
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
            try:
                return client.get_media(
                    identifier,
                    use_playwright_fallback=force_playwright or self.playwright_var.get(),
                    log=lambda msg: self.events.put(("log", msg)),
                )
            except Exception as exc:  # noqa: BLE001 - try another identifier.
                errors.append(f"{identifier}: {exc}")

        raise TVCFError("; ".join(errors) if errors else "상세 정보를 찾지 못했습니다.")

    def preview(self) -> None:
        self._start_worker(download=False)

    def download(self) -> None:
        self._start_worker(download=True)

    def stop(self) -> None:
        self.stop_event.set()
        self._log(f"중지 요청: {self.last_checkpoint}")

    def _start_worker(self, download: bool) -> None:
        if self.worker and self.worker.is_alive():
            self._log("이미 작업이 진행 중입니다.")
            return

        self.stop_event.clear()
        self._save_current_config()
        self.run_summary = self._build_run_summary()
        self._set_checkpoint(f"작업 준비: {self.run_summary}")
        self._clear_items()
        self.progress.configure(value=0, maximum=1)
        self.progress_text_var.set("0 / 0")
        self._set_status("작업 준비 중")

        self.worker = threading.Thread(target=self._worker_run, args=(download,), daemon=True)
        self.worker.start()

    def _worker_run(self, download: bool) -> None:
        try:
            client = TVCFClient()
            items = self._build_items(client)
            self.events.put(("items", items))
            self._checkpoint(f"대상 수집 완료: {self.run_summary} / 대상 {len(items)}개")

            if not download:
                self.events.put(("status", f"미리보기 완료: {len(items)}개"))
                return

            if not items:
                self.events.put(("status", "대상 없음"))
                self.events.put(("log", "다운로드할 한국 광고가 없습니다. 날짜 기준과 시작일을 확인해주세요."))
                return

            self.events.put(("progress_max", max(1, len(items))))
            failed_count = 0
            for index, item in enumerate(items, start=1):
                if self.stop_event.is_set():
                    self.events.put(("status", "중단됨"))
                    self.events.put(("log", f"중단 지점: {self.last_checkpoint}"))
                    return

                label = item.display_title
                position = self._item_position_text(index, len(items), item)
                self._checkpoint(f"{self.run_summary} / {position} 상세 확인 중 / {label}")
                self.events.put(("item_status", item.nidx or item.idx, "상세 확인"))
                self.events.put(("log", f"[{index}/{len(items)}] {label}"))

                try:
                    detail = client.get_media(
                        item.nidx or item.play_url or item.idx,
                        use_playwright_fallback=self.playwright_var.get(),
                        log=lambda msg: self.events.put(("log", msg)),
                    )
                except Exception as exc:  # noqa: BLE001 - keep batch moving after one bad page.
                    failed_count += 1
                    self.events.put(("item_status", item.nidx or item.idx, "오류"))
                    self.events.put(("log", f"상세 확인 오류 - 건너뜀: {label} / {exc}"))
                    self.events.put(("progress", index))
                    continue

                if detail.country_code and detail.country_code != "410":
                    self.events.put(("item_status", item.nidx or item.idx, "한국 아님"))
                    self.events.put(("progress", index))
                    continue
                if detail.category_code and detail.category_code != "1":
                    self.events.put(("item_status", item.nidx or item.idx, "광고 아님"))
                    self.events.put(("progress", index))
                    continue

                merged = self._merge_item(item, detail)
                self._checkpoint(f"{self.run_summary} / {position} 다운로드 중 / {merged.display_title}")
                self.events.put(("item_status", item.nidx or item.idx, "다운로드"))
                try:
                    output = download_media(
                        merged,
                        self.download_dir_var.get(),
                        self.quality_var.get(),
                        self.date_basis_var.get(),
                        prefer_ytdlp=self.prefer_ytdlp_var.get(),
                        log=lambda msg: self.events.put(("log", msg)),
                        should_stop=self.stop_event.is_set,
                    )
                except DownloadCancelled:
                    self.events.put(("item_status", item.nidx or item.idx, "중단됨"))
                    self.events.put(("status", "중단됨"))
                    self.events.put(("log", f"중단 지점: {self.last_checkpoint}"))
                    return
                except Exception as exc:  # noqa: BLE001 - skip failed item and continue.
                    failed_count += 1
                    self.events.put(("item_status", item.nidx or item.idx, "오류"))
                    self.events.put(("log", f"다운로드 오류 - 건너뜀: {merged.display_title} / {exc}"))
                    self.events.put(("progress", index))
                    continue

                result_status = "건너뜀" if output.skipped else "재다운완료" if output.repaired else "완료"
                self.events.put(("item_status", item.nidx or item.idx, result_status))
                self.events.put(("log", f"저장 완료: {output.path}"))
                self.events.put(("progress", index))

            retry_failed_count = self._drain_retry_queue(client)
            failed_count += retry_failed_count

            if failed_count:
                self.events.put(("status", f"다운로드 완료(오류 {failed_count}개)"))
            else:
                self.events.put(("status", "다운로드 완료"))
            self._checkpoint(f"작업 완료: {self.run_summary}")
        except Exception as exc:  # noqa: BLE001 - show GUI error.
            self.events.put(("status", "오류"))
            self.events.put(("log", f"오류: {exc}"))
            self.events.put(("log", f"마지막 작업: {self.last_checkpoint}"))

    def _build_items(self, client: TVCFClient) -> list[MediaItem]:
        if self.mode_var.get() == "period":
            start = datetime.strptime(self.date_from_var.get().strip(), "%Y-%m-%d").date()
            end = datetime.strptime(self.date_to_var.get().strip(), "%Y-%m-%d").date()
            if start > end:
                start, end = end, start
            items = client.collect_period(
                start,
                end,
                self.date_basis_var.get(),
                self.max_pages_var.get(),
                log=lambda msg: self.events.put(("log", msg)),
                should_stop=self.stop_event.is_set,
            )
            return self._sort_items_by_file_date(items)

        start_id = self.id_start_var.get().strip()
        end_id = self.id_end_var.get().strip() or start_id
        if start_id.isdigit() and end_id.isdigit():
            left, right = sorted((int(start_id), int(end_id)))
            identifiers = [str(value) for value in range(left, right + 1)]
        else:
            identifiers = [start_id]

        items: list[MediaItem] = []
        for identifier in identifiers:
            if self.stop_event.is_set():
                break
            try:
                item = client.get_media(
                    identifier,
                    use_playwright_fallback=self.playwright_var.get(),
                    log=lambda msg: self.events.put(("log", msg)),
                )
                if item.country_code == "410" and (item.category_code == "1" or item.category_name == "광고"):
                    items.append(item)
                else:
                    self.events.put(("log", f"{identifier}: 한국 광고가 아니어서 제외"))
            except TVCFError as exc:
                self.events.put(("log", f"{identifier}: {exc}"))
        return items

    def _sort_items_by_file_date(self, items: list[MediaItem]) -> list[MediaItem]:
        basis = self.date_basis_var.get()
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
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(120, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self._log(event[1])
        elif kind == "status":
            self._set_status(event[1])
            self._log(event[1])
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

    def _show_items(self, items: list[MediaItem]) -> None:
        self._clear_tree()
        self.item_by_id = {}
        for row_index, item in enumerate(items):
            item_id = item.nidx or item.idx
            self.item_by_id[item_id] = item
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    "☐",
                    item.date_label(self.date_basis_var.get()),
                    item.display_title,
                    item_id,
                    "대기",
                ),
                tags=("even" if row_index % 2 else "odd", "skip"),
            )

    def _set_item_status(self, item_id: str, status: str) -> None:
        if not item_id or not self.tree.exists(item_id):
            return
        values = list(self.tree.item(item_id, "values"))
        if len(values) >= 5:
            values[4] = status
            base_tags = [tag for tag in self.tree.item(item_id, "tags") if tag in ("odd", "even")]
            status_tag = self._status_tag(status)
            tags = tuple(base_tags + ([status_tag] if status_tag else []))
            self.tree.item(item_id, values=values, tags=tags)

    def _refresh_row_stripes(self) -> None:
        for row_index, row_id in enumerate(self.tree.get_children()):
            tags = [tag for tag in self.tree.item(row_id, "tags") if tag not in ("odd", "even")]
            tags.insert(0, "even" if row_index % 2 else "odd")
            self.tree.item(row_id, tags=tuple(tags))

    def _clear_items(self) -> None:
        self.items = []
        self.item_by_id = {}
        self.checked_rows.clear()
        with self.retry_lock:
            self.retry_queue.clear()
            self.queued_retry_rows.clear()
        self._clear_tree()
        self._clear_log()

    def _clear_tree(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

    def _clear_log(self) -> None:
        if hasattr(self, "log_text"):
            self.log_text.delete("1.0", "end")

    def _log(self, message: str) -> None:
        message = str(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        level = self._log_level(message)
        self.log_text.insert("end", f"[{timestamp}]  ", "time")
        self.log_text.insert("end", f"[{level}]  ", level)
        self.log_text.insert("end", f"{message}\n", "message")
        self.log_text.see("end")

    @staticmethod
    def _log_level(message: str) -> str:
        if any(word in message for word in ("오류", "실패", "중단", "Error", "ERROR")):
            return "error"
        if any(word in message for word in ("제외", "없습니다", "손상", "삭제", "경고")):
            return "warn"
        return "info"

    def _checkpoint(self, message: str) -> None:
        self.last_checkpoint = message
        self.events.put(("checkpoint", message))

    def _set_checkpoint(self, message: str) -> None:
        self.last_checkpoint = message
        self.current_task_var.set(message)
        self.config["last_checkpoint"] = message
        try:
            save_config(self.config)
        except OSError:
            pass

    def _set_progress_text(self, value: int, maximum: int) -> None:
        if maximum <= 0:
            self.progress_text_var.set("0 / 0")
            return
        self.progress_text_var.set(f"{min(value, maximum)} / {maximum}")

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
        if status in {"중단됨", "오류"}:
            return "error"
        return ""

    def _sync_wraplength(self, event: object | None = None) -> None:
        if not hasattr(self, "current_task_label"):
            return
        width = max(480, self.root.winfo_width() - 140)
        self.current_task_label.configure(wraplength=width)

    def _build_run_summary(self) -> str:
        max_pages = self.max_pages_var.get()
        max_pages_label = "자동" if max_pages <= 0 else f"{max_pages}페이지"
        if self.mode_var.get() == "period":
            return (
                f"{self.date_from_var.get()}~{self.date_to_var.get()} / "
                f"기준 {self.date_basis_var.get()} / 화질 {self.quality_var.get()} / "
                f"페이지 {max_pages_label} / 저장 {self.download_dir_var.get()}"
            )

        return (
            f"ID {self.id_start_var.get()}~{self.id_end_var.get() or self.id_start_var.get()} / "
            f"화질 {self.quality_var.get()} / 저장 {self.download_dir_var.get()}"
        )

    def _item_position_text(self, index: int, total: int, item: MediaItem) -> str:
        item_date = item.date_value(self.date_basis_var.get())
        if self.mode_var.get() == "period" and item_date:
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
                "date_basis": self.date_basis_var.get(),
                "id_start": self.id_start_var.get(),
                "id_end": self.id_end_var.get(),
                "quality": self.quality_var.get(),
                "max_pages": self.max_pages_var.get(),
                "prefer_ytdlp": self.prefer_ytdlp_var.get(),
                "use_playwright_fallback": self.playwright_var.get(),
                "last_checkpoint": self.last_checkpoint,
            }
        )
        save_config(self.config)

    def _on_close(self) -> None:
        self._save_current_config()
        self.stop_event.set()
        self.root.destroy()


def run_app() -> None:
    root = Tk()
    DownloaderApp(root)
    root.mainloop()
