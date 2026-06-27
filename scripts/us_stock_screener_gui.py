#!/usr/bin/env python3
"""Beginner-friendly GUI for the US stock screener."""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
import queue
import threading
import time
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from fetch_yfinance_snapshot import (  # noqa: E402
    DEFAULT_SNAPSHOT_PATH,
    DEFAULT_WATCHLIST_PATH,
    YFinanceUnavailableError,
    fetch_snapshot,
    load_snapshot,
    load_watchlist,
    save_snapshot,
)
from us_stock_screener import (  # noqa: E402
    ScreenConfig,
    _format_sector_relative_peer_source,
    _format_sector_relative_preview,
    build_report,
    load_records,
)


class ScreenerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("美股自動選股助手")
        self.geometry("1280x820")
        self.minsize(1080, 720)

        self.config = ScreenConfig()
        self.palette = {
            "page": "#f3f6fb",
            "paper": "#ffffff",
            "paper_alt": "#f8fafc",
            "ink": "#0f172a",
            "soft_ink": "#64748b",
            "muted_ink": "#94a3b8",
            "line": "#dbe3ef",
            "line_strong": "#c7d2e2",
            "accent": "#2563eb",
            "accent_soft": "#dbeafe",
            "accent_warm": "#f59e0b",
            "accent_green": "#16a34a",
            "accent_red": "#dc2626",
            "accent_lavender": "#7c3aed",
            "button_secondary": "#eef2f7",
            "button_secondary_hover": "#e2e8f0",
            "success_soft": "#dcfce7",
            "warning_soft": "#fef3c7",
            "danger_soft": "#fee2e2",
        }
        self.font_family = self._pick_font_family(
            "SF Pro Display",
            "Avenir Next",
            "Helvetica Neue",
            "Arial",
        )
        self.body_family = self._pick_font_family("SF Pro Text", "Helvetica Neue", "Avenir Next", "Arial")
        self.mono_family = self._pick_font_family("JetBrains Mono", "Menlo", "Monaco", "Courier New")
        self.font_display_large = (self.font_family, 24, "bold")
        self.font_display = (self.font_family, 17, "bold")
        self.font_body = (self.body_family, 11)
        self.font_body_bold = (self.body_family, 11, "bold")
        self.font_small = (self.body_family, 10)
        self.font_tiny = (self.body_family, 9)
        self.font_mono = (self.mono_family, 11)
        self.selected_path: Path | None = None
        self.current_mode = "watchlist"
        self.snapshot_bundle = None
        self.current_report = None
        self.current_snapshot_path = DEFAULT_SNAPSHOT_PATH
        self.candidate_lookup: dict[str, object] = {}
        self.selected_candidate_ticker: str | None = None
        self.strategy_var = tk.StringVar(value="Hybrid")
        self.force_rebalance_var = tk.BooleanVar(value=False)
        self.action_buttons: list[tk.Button] = []
        self.busy_var = tk.StringVar(value="就緒")
        self.is_busy = False
        self.busy_started_at: float | None = None
        self.busy_timer_after_id: str | None = None
        self.worker_queue: queue.Queue = queue.Queue()
        self.worker_poll_after_id: str | None = None

        self._build_theme()
        self._build_ui()
        self._show_empty_state()
        self.after_idle(self._focus_main_window)

    def _pick_font_family(self, *candidates: str) -> str:
        available = set(tkfont.families(self))
        for candidate in candidates:
            if candidate in available:
                return candidate
        return candidates[-1]

    def _build_theme(self) -> None:
        self.configure(bg=self.palette["page"])
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Screener.TNotebook", background=self.palette["page"], borderwidth=0)
        style.configure(
            "Screener.TNotebook.Tab",
            padding=(18, 10),
            background=self.palette["paper_alt"],
            foreground=self.palette["ink"],
            font=self.font_body_bold,
        )
        style.map(
            "Screener.TNotebook.Tab",
            background=[("selected", self.palette["paper"])],
            foreground=[("selected", self.palette["accent"])],
        )
        style.configure(
            "Screener.Treeview",
            rowheight=34,
            font=self.font_body,
            background=self.palette["paper"],
            fieldbackground=self.palette["paper"],
            foreground=self.palette["ink"],
            borderwidth=0,
        )
        style.configure(
            "Screener.Treeview.Heading",
            font=self.font_body_bold,
            background=self.palette["paper_alt"],
            foreground=self.palette["ink"],
            relief="flat",
            padding=(8, 8),
        )
        style.map(
            "Screener.Treeview",
            background=[("selected", self.palette["accent_soft"])],
            foreground=[("selected", self.palette["ink"])],
        )
        style.configure(
            "Screener.Horizontal.TProgressbar",
            troughcolor=self.palette["paper_alt"],
            background=self.palette["accent"],
            bordercolor=self.palette["line"],
            lightcolor=self.palette["accent"],
            darkcolor=self.palette["accent"],
        )
        style.configure(
            "Screener.TCombobox",
            fieldbackground=self.palette["paper"],
            background=self.palette["paper"],
            foreground=self.palette["ink"],
            arrowcolor=self.palette["accent"],
            padding=(8, 6),
        )
        style.configure(
            "Primary.TButton",
            font=self.font_body_bold,
            padding=(14, 12),
            background=self.palette["accent"],
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=2,
            focuscolor=self.palette["accent"],
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", self.palette["line"]),
                ("pressed", "#1e40af"),
                ("active", "#1d4ed8"),
            ],
            foreground=[("disabled", self.palette["muted_ink"]), ("!disabled", "#ffffff")],
        )
        style.configure(
            "Secondary.TButton",
            font=self.font_body_bold,
            padding=(14, 10),
            background=self.palette["button_secondary"],
            foreground=self.palette["ink"],
            borderwidth=0,
            focusthickness=2,
            focuscolor=self.palette["accent"],
        )
        style.map(
            "Secondary.TButton",
            background=[
                ("disabled", self.palette["line"]),
                ("pressed", self.palette["line_strong"]),
                ("active", self.palette["button_secondary_hover"]),
            ],
            foreground=[("disabled", self.palette["muted_ink"]), ("!disabled", self.palette["ink"])],
        )

    def _build_ui(self) -> None:
        root = tk.Frame(self, bg=self.palette["page"])
        root.pack(fill="both", expand=True)

        hero = tk.Frame(root, bg=self.palette["paper"], highlightthickness=1, highlightbackground=self.palette["line"])
        hero.pack(fill="x", padx=16, pady=(16, 10))

        top_strip = tk.Frame(hero, bg=self.palette["paper"])
        top_strip.pack(fill="x", padx=24, pady=(18, 0))
        tk.Label(
            top_strip,
            text="US STOCK SCREENER",
            font=self.font_body_bold,
            fg=self.palette["accent"],
            bg=self.palette["paper"],
        ).pack(side="left")
        tk.Label(
            top_strip,
            text="Hybrid / Stop Checking Price",
            font=self.font_small,
            fg=self.palette["soft_ink"],
            bg=self.palette["paper"],
        ).pack(side="right")

        tk.Label(
            hero,
            text="美股自動選股助手",
            font=self.font_display_large,
            fg=self.palette["ink"],
            bg=self.palette["paper"],
        ).pack(anchor="w", padx=24, pady=(8, 2))
        tk.Label(
            hero,
            text="選擇 ticker 清單後，一鍵更新 yfinance snapshot 並產生候選排名、理由與風險提示。",
            font=self.font_body,
            fg=self.palette["soft_ink"],
            bg=self.palette["paper"],
        ).pack(anchor="w", padx=24, pady=(0, 14))

        steps = tk.Frame(hero, bg=self.palette["paper"])
        steps.pack(fill="x", padx=24, pady=(0, 20))
        self._step_card(steps, "1", "選清單", "先選 sample-watchlist.csv 或你自己的 ticker 檔").pack(side="left", padx=(0, 12))
        self._step_card(steps, "2", "更新並篩選", "按一下就用 yfinance 抓最新 snapshot 並重算排名").pack(side="left", padx=(0, 12))
        self._step_card(steps, "3", "看結果", "直接看候選表格、理由與風險").pack(side="left")

        body = tk.Frame(root, bg=self.palette["page"])
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left_shell = tk.Frame(body, bg=self.palette["paper"], highlightthickness=1, highlightbackground=self.palette["line"], width=330)
        left_shell.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        left_shell.grid_propagate(False)
        left_shell.grid_rowconfigure(0, weight=1)
        left_shell.grid_columnconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_shell, bg=self.palette["paper"], highlightthickness=0, bd=0, width=330)
        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scroll = ttk.Scrollbar(left_shell, orient="vertical", command=left_canvas.yview)
        left_scroll.grid(row=0, column=1, sticky="ns")
        left_canvas.configure(yscrollcommand=left_scroll.set)

        left = tk.Frame(left_canvas, bg=self.palette["paper"])
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")

        def _sync_left_scrollregion(_event=None) -> None:
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def _sync_left_width(event) -> None:
            left_canvas.itemconfigure(left_window, width=event.width)

        def _scroll_left_units(delta: int) -> None:
            left_canvas.yview_scroll(delta, "units")

        def _on_left_mousewheel(event) -> str:
            if not str(left_canvas.cget("scrollregion")):
                return "break"
            if sys.platform == "darwin":
                delta = int(-1 * event.delta)
            else:
                delta = int(-1 * (event.delta / 120))
            if delta == 0:
                delta = -1 if event.delta > 0 else 1
            _scroll_left_units(delta)
            return "break"

        def _on_left_linux_scroll_up(_event) -> str:
            _scroll_left_units(-1)
            return "break"

        def _on_left_linux_scroll_down(_event) -> str:
            _scroll_left_units(1)
            return "break"

        def _bind_left_mousewheel(_event=None) -> None:
            left_canvas.bind_all("<MouseWheel>", _on_left_mousewheel)
            left_canvas.bind_all("<Button-4>", _on_left_linux_scroll_up)
            left_canvas.bind_all("<Button-5>", _on_left_linux_scroll_down)

        def _unbind_left_mousewheel(_event=None) -> None:
            left_canvas.unbind_all("<MouseWheel>")
            left_canvas.unbind_all("<Button-4>")
            left_canvas.unbind_all("<Button-5>")

        left.bind("<Configure>", _sync_left_scrollregion)
        left_canvas.bind("<Configure>", _sync_left_width)
        for widget in (left_canvas, left):
            widget.bind("<Enter>", _bind_left_mousewheel)
            widget.bind("<Leave>", _unbind_left_mousewheel)

        right = tk.Frame(body, bg=self.palette["paper"], highlightthickness=1, highlightbackground=self.palette["line"])
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        left_note = tk.Frame(left, bg=self.palette["paper_alt"], highlightthickness=1, highlightbackground=self.palette["line"])
        left_note.pack(fill="x", padx=14, pady=(14, 0))
        tk.Label(
            left_note,
            text="操作面板",
            font=self.font_body_bold,
            fg=self.palette["ink"],
            bg=self.palette["paper_alt"],
        ).pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(
            left_note,
            text="選清單、選策略、按更新並篩選。",
            font=self.font_small,
            fg=self.palette["soft_ink"],
            bg=self.palette["paper_alt"],
        ).pack(anchor="w", padx=12, pady=(0, 8))

        busy_box = tk.Frame(left, bg=self.palette["accent_soft"], highlightthickness=1, highlightbackground=self.palette["line"])
        busy_box.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(
            busy_box,
            text="執行狀態",
            anchor="w",
            font=self.font_body_bold,
            fg=self.palette["ink"],
            bg=self.palette["accent_soft"],
        ).pack(fill="x", padx=12, pady=(8, 0))
        self.busy_label = tk.Label(
            busy_box,
            textvariable=self.busy_var,
            anchor="w",
            font=self.font_small,
            fg=self.palette["soft_ink"],
            bg=self.palette["accent_soft"],
        )
        self.busy_label.pack(fill="x", padx=12, pady=(4, 6))
        self.busy_bar = ttk.Progressbar(busy_box, mode="indeterminate", style="Screener.Horizontal.TProgressbar")
        self.busy_bar.pack(fill="x", padx=12, pady=(0, 10))

        actions_box = tk.Frame(left, bg=self.palette["paper"])
        actions_box.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(
            actions_box,
            text="核心操作",
            anchor="w",
            font=self.font_body_bold,
            fg=self.palette["ink"],
            bg=self.palette["paper"],
        ).pack(fill="x", padx=4, pady=(0, 6))

        self.file_label = tk.Label(
            left,
            text="目前：未選資料",
            anchor="w",
            justify="left",
            font=self.font_body_bold,
            fg=self.palette["ink"],
            bg=self.palette["paper"],
            wraplength=280,
        )
        self.file_label.pack(fill="x", padx=18, pady=(14, 8))

        self.snapshot_label = tk.Label(
            left,
            text="快照：尚未更新",
            anchor="w",
            justify="left",
            font=self.font_small,
            fg=self.palette["soft_ink"],
            bg=self.palette["paper"],
            wraplength=280,
        )
        self.snapshot_label.pack(fill="x", padx=18, pady=(0, 10))

        self.hint_title = tk.Label(
            left,
            text="你只要做這三件事",
            anchor="w",
            font=self.font_body_bold,
            fg=self.palette["accent"],
            bg=self.palette["paper"],
        )
        self.hint_title.pack(fill="x", padx=18, pady=(8, 6))

        hints = [
            "1. 先點「選擇 ticker 清單」。",
            "2. 再按「更新並篩選」，它會自動抓資料並重算排名。",
        ]
        for hint in hints:
            tk.Label(
                left,
                text=hint,
                anchor="w",
                justify="left",
                font=self.font_small,
                fg=self.palette["soft_ink"],
                bg=self.palette["paper"],
                wraplength=284,
            ).pack(fill="x", padx=18, pady=2)

        options_box = tk.Frame(left, bg=self.palette["paper_alt"], highlightthickness=1, highlightbackground=self.palette["line"])
        options_box.pack(fill="x", padx=18, pady=(10, 8))
        tk.Label(
            options_box,
            text="策略模式",
            anchor="w",
            font=self.font_body_bold,
            fg=self.palette["ink"],
            bg=self.palette["paper_alt"],
        ).pack(fill="x", padx=12, pady=(10, 4))
        strategy_choice_box = tk.Frame(options_box, bg=self.palette["paper_alt"])
        strategy_choice_box.pack(fill="x", padx=10, pady=(0, 8))
        self.strategy_menu = tk.OptionMenu(
            strategy_choice_box,
            self.strategy_var,
            "Hybrid",
            "Stop Checking Price",
            command=self._on_strategy_change,
        )
        self.strategy_menu.configure(
            bg=self.palette["paper"],
            fg=self.palette["ink"],
            activebackground=self.palette["accent_soft"],
            activeforeground=self.palette["ink"],
            highlightthickness=1,
            highlightbackground=self.palette["line"],
            relief="flat",
            anchor="w",
            font=self.font_body_bold,
            padx=10,
            pady=8,
            cursor="hand2",
        )
        self.strategy_menu["menu"].configure(
            bg=self.palette["paper"],
            fg=self.palette["ink"],
            activebackground=self.palette["accent_soft"],
            activeforeground=self.palette["ink"],
            font=self.font_body,
        )
        self.strategy_menu.pack(fill="x", pady=(0, 6))
        tk.Label(
            strategy_choice_box,
            text="Hybrid：日常混合評分；Stop Checking Price：長期品質模式。",
            anchor="w",
            justify="left",
            font=self.font_small,
            fg=self.palette["soft_ink"],
            bg=self.palette["paper_alt"],
            wraplength=280,
        ).pack(fill="x")
        self.force_rebalance_check = tk.Checkbutton(
            options_box,
            text="強制季度檢查",
            variable=self.force_rebalance_var,
            onvalue=True,
            offvalue=False,
            fg=self.palette["ink"],
            bg=self.palette["paper_alt"],
            activebackground=self.palette["paper_alt"],
            activeforeground=self.palette["ink"],
            selectcolor=self.palette["paper_alt"],
            anchor="w",
            font=self.font_small,
            padx=8,
            pady=6,
            cursor="hand2",
        )
        self.force_rebalance_check.pack(fill="x", padx=10, pady=(0, 10))

        button_specs = [
            ("選擇 ticker 清單", self.choose_file, self.palette["button_secondary"]),
            ("更新並篩選", self.refresh_data, self.palette["accent"]),
            ("儲存結果", self.save_report, self.palette["button_secondary"]),
        ]
        for text, command, color in button_specs:
            btn = self._button(actions_box, text, command, color)
            btn.pack(fill="x", padx=4, pady=(4 if text == "更新並篩選" else 3))
            self.action_buttons.append(btn)

        self.status = tk.Label(
            left,
            text="還沒開始。",
            anchor="w",
            justify="left",
            font=self.font_small,
            fg=self.palette["ink"],
            bg=self.palette["paper_alt"],
            wraplength=280,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.palette["line"],
            padx=10,
            pady=10,
        )
        self.status.pack(fill="x", padx=18, pady=(4, 18))

        self.summary_stats = tk.Frame(right, bg=self.palette["paper"])
        self.summary_stats.grid(row=0, column=0, sticky="ew")
        for idx in range(4):
            self.summary_stats.columnconfigure(idx, weight=1)

        self.metric_universe = self._metric_card(self.summary_stats, "輸入", "0")
        self.metric_candidates = self._metric_card(self.summary_stats, "候選顯示", "0")
        self.metric_excluded = self._metric_card(self.summary_stats, "硬性剔除", "0")
        self.metric_snapshot = self._metric_card(self.summary_stats, "快照", "尚未更新")
        self.metric_universe.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        self.metric_candidates.grid(row=0, column=1, sticky="ew", padx=12, pady=12)
        self.metric_excluded.grid(row=0, column=2, sticky="ew", padx=12, pady=12)
        self.metric_snapshot.grid(row=0, column=3, sticky="ew", padx=12, pady=12)

        notebook = ttk.Notebook(right, style="Screener.TNotebook")
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        summary_tab = tk.Frame(notebook, bg=self.palette["paper"])
        detail_tab = tk.Frame(notebook, bg=self.palette["paper"])
        notebook.add(summary_tab, text="候選清單")
        notebook.add(detail_tab, text="完整輸出")

        summary_tab.rowconfigure(0, weight=1)
        summary_tab.rowconfigure(1, weight=0)
        summary_tab.columnconfigure(0, weight=1)

        table_frame = tk.Frame(summary_tab, bg=self.palette["paper"])
        table_frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 8))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = ("rank", "ticker", "score", "sector_preview", "reasons", "risk")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            style="Screener.Treeview",
            selectmode="browse",
        )
        self.tree.heading("rank", text="排名")
        self.tree.heading("ticker", text="Ticker")
        self.tree.heading("score", text="總分")
        self.tree.heading("sector_preview", text="Sector Preview")
        self.tree.heading("reasons", text="入選理由")
        self.tree.heading("risk", text="風險提醒")
        self.tree.column("rank", width=70, anchor="center")
        self.tree.column("ticker", width=90, anchor="center")
        self.tree.column("score", width=90, anchor="center")
        self.tree.column("sector_preview", width=190, anchor="center")
        self.tree.column("reasons", width=440, anchor="w")
        self.tree.column("risk", width=260, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        tree_x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        tree_x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=tree_x_scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        lower = tk.Frame(summary_tab, bg=self.palette["paper"])
        lower.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        lower.columnconfigure(0, weight=2)
        lower.columnconfigure(1, weight=1)

        detail_box = self._panel(lower, "選到一檔後會顯示完整理由", self.palette["accent_soft"])
        detail_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        detail_box.rowconfigure(1, weight=1)
        detail_box.columnconfigure(0, weight=1)
        self.detail_text = self._scrollable_text(detail_box, height=12)

        exclusion_box = self._panel(lower, "被排除的股票", self.palette["paper_alt"])
        exclusion_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        exclusion_box.rowconfigure(1, weight=1)
        exclusion_box.columnconfigure(0, weight=1)
        self.exclusion_text = self._scrollable_text(exclusion_box, height=12)

        detail_tab.rowconfigure(0, weight=1)
        detail_tab.columnconfigure(0, weight=1)
        self.output = self._scrollable_text(detail_tab, height=30, font=self.font_mono, padx=12, pady=12)

    def _step_card(self, parent: tk.Widget, number: str, title: str, body: str) -> tk.Frame:
        card = tk.Frame(parent, bg=self.palette["paper_alt"], highlightthickness=1, highlightbackground=self.palette["line"])
        card.configure(width=292, height=78)
        card.grid_propagate(False)
        tk.Label(
            card,
            text=number,
            width=2,
            font=self.font_display,
            fg=self.palette["accent"],
            bg=self.palette["paper_alt"],
        ).grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=10, sticky="nw")
        tk.Label(card, text=title, font=self.font_body_bold, fg=self.palette["ink"], bg=self.palette["paper_alt"]).grid(
            row=0, column=1, sticky="w", pady=(12, 0)
        )
        tk.Label(
            card,
            text=body,
            font=self.font_small,
            fg=self.palette["soft_ink"],
            bg=self.palette["paper_alt"],
            wraplength=220,
            justify="left",
        ).grid(row=1, column=1, sticky="w", padx=(0, 12), pady=(0, 10))
        return card

    def _metric_card(self, parent: tk.Widget, label: str, value: str) -> tk.Frame:
        card = tk.Frame(parent, bg=self.palette["paper"], highlightthickness=1, highlightbackground=self.palette["line"])
        card.configure(height=70)
        card.grid_propagate(False)
        tk.Label(card, text=label, font=self.font_small, fg=self.palette["soft_ink"], bg=self.palette["paper"]).pack(
            anchor="w", padx=12, pady=(10, 2)
        )
        text = tk.Label(card, text=value, font=self.font_display, fg=self.palette["ink"], bg=self.palette["paper"])
        text.pack(anchor="w", padx=12)
        card.value_label = text  # type: ignore[attr-defined]
        return card

    def _panel(self, parent: tk.Widget, title: str, accent: str) -> tk.Frame:
        panel = tk.Frame(parent, bg=accent, highlightthickness=1, highlightbackground=self.palette["line"])
        title_bar = tk.Frame(panel, bg=accent)
        title_bar.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(title_bar, text=title, font=self.font_body_bold, fg=self.palette["ink"], bg=accent).pack(
            anchor="w"
        )
        return panel

    def _text_widget(self, parent: tk.Widget, *, height: int, font=("Helvetica Neue", 11)) -> tk.Text:
        text = tk.Text(
            parent,
            wrap="word",
            height=height,
            bg=self.palette["paper"],
            fg=self.palette["ink"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.palette["line"],
            highlightcolor=self.palette["accent"],
            font=font,
            padx=10,
            pady=10,
            insertbackground=self.palette["ink"],
            selectbackground=self.palette["accent_soft"],
            selectforeground=self.palette["ink"],
        )
        text.configure(state="normal")
        return text

    def _scrollable_text(
        self,
        parent: tk.Widget,
        *,
        height: int,
        font=("Helvetica Neue", 11),
        padx: int = 10,
        pady: int = 10,
    ) -> tk.Text:
        container = tk.Frame(parent, bg=parent.cget("bg"))
        container.pack(fill="both", expand=True, padx=padx, pady=(0, pady))
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        text = self._text_widget(container, height=height, font=font)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(container, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        return text

    def _button(self, parent: tk.Widget, text: str, command, color: str) -> tk.Button:
        is_primary = text == "更新並篩選"
        default_bg = self.palette["accent"] if is_primary else self.palette["button_secondary"]
        hover_bg = "#1d4ed8" if is_primary else self.palette["button_secondary_hover"]
        pressed_bg = "#1e40af" if is_primary else self.palette["line_strong"]
        text_fg = "#ffffff" if is_primary else self.palette["ink"]
        disabled_fg = self.palette["muted_ink"]

        button = tk.Button(
            parent,
            text=text,
            bg=default_bg,
            fg=text_fg,
            activebackground=hover_bg,
            activeforeground=text_fg,
            disabledforeground=disabled_fg,
            font=self.font_body_bold,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.palette["line"],
            highlightcolor=self.palette["accent"],
            cursor="hand2",
            takefocus=1,
            pady=12 if is_primary else 10,
            padx=12,
        )

        button.default_bg = default_bg  # type: ignore[attr-defined]
        button.hover_bg = hover_bg  # type: ignore[attr-defined]
        button.pressed_bg = pressed_bg  # type: ignore[attr-defined]
        button.text_fg = text_fg  # type: ignore[attr-defined]

        def _enabled() -> bool:
            return str(button.cget("state")) != tk.DISABLED

        def _paint(bg: str) -> None:
            if _enabled():
                button.config(bg=bg, activebackground=bg)

        def _run_action() -> None:
            if not _enabled():
                self._set_status("目前正在執行，請等它完成。")
                return
            self._set_status(f"已收到操作：{text}")
            command()

        def _on_enter(_event=None) -> None:
            _paint(button.hover_bg)  # type: ignore[attr-defined]

        def _on_leave(_event=None) -> None:
            _paint(button.default_bg)  # type: ignore[attr-defined]

        def _on_press(_event=None) -> None:
            _paint(button.pressed_bg)  # type: ignore[attr-defined]

        def _on_release(_event=None) -> None:
            _paint(button.hover_bg)  # type: ignore[attr-defined]

        button.configure(command=_run_action)
        button.bind("<Enter>", _on_enter)
        button.bind("<Leave>", _on_leave)
        button.bind("<ButtonPress-1>", _on_press)
        button.bind("<ButtonRelease-1>", _on_release)
        return button

    def _set_action_enabled(self, widget: tk.Button, enabled: bool) -> None:
        bg = widget.default_bg if enabled else self.palette["line"]  # type: ignore[attr-defined]
        fg = widget.text_fg if enabled else self.palette["muted_ink"]  # type: ignore[attr-defined]
        cursor = "hand2" if enabled else "arrow"
        state = tk.NORMAL if enabled else tk.DISABLED
        widget.config(bg=bg, fg=fg, activebackground=bg, cursor=cursor, state=state)

    def _focus_main_window(self) -> None:
        try:
            self.focus_set()
        except tk.TclError:
            pass

    def _nudge_window_for_macos_tk_click_bug(self) -> None:
        # Kept as an emergency manual hook, but no longer scheduled at startup.
        # The geometry nudge caused a short period where macOS/Tk missed clicks.
        if sys.platform != "darwin":
            return
        try:
            self.update_idletasks()
            width = self.winfo_width()
            height = self.winfo_height()
            x = self.winfo_x()
            y = self.winfo_y()
            if width <= 1 or height <= 1:
                return
            self.geometry(f"{width}x{height}+{x + 1}+{y}")
            self.after(60, lambda: self.geometry(f"{width}x{height}+{x}+{y}"))
        except tk.TclError:
            pass

    def _on_strategy_change(self, value: str) -> None:
        self.strategy_var.set(value)
        self._set_status(f"策略模式已切換：{value}")

    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    def _set_busy(self, active: bool, message: str | None = None) -> None:
        if self.busy_timer_after_id is not None:
            try:
                self.after_cancel(self.busy_timer_after_id)
            except tk.TclError:
                pass
            self.busy_timer_after_id = None
        self.is_busy = active
        if message is not None:
            self.busy_var.set(message)
        if active:
            self.busy_started_at = time.monotonic()
            for button in self.action_buttons:
                self._set_action_enabled(button, False)
            self.busy_bar.start(12)
            self._update_busy_timer()
        else:
            self.busy_started_at = None
            self.busy_bar.stop()
            for button in self.action_buttons:
                self._set_action_enabled(button, True)
            if message is not None:
                self.busy_var.set(message)

    def _update_busy_timer(self) -> None:
        if self.busy_started_at is None:
            return
        elapsed = max(0, int(time.monotonic() - self.busy_started_at))
        base = self.busy_var.get()
        if "更新中" in base:
            label = "更新中"
        elif "篩選中" in base:
            label = "篩選中"
        else:
            label = "執行中"
        self.busy_var.set(f"{label}：已運行 {elapsed} 秒")
        self.busy_timer_after_id = self.after(1000, self._update_busy_timer)

    def _start_worker_polling(self) -> None:
        if self.worker_poll_after_id is None:
            self.worker_poll_after_id = self.after(80, self._poll_worker_queue)

    def _poll_worker_queue(self) -> None:
        processed = 0
        max_events_per_tick = 12
        try:
            while processed < max_events_per_tick:
                kind, payload = self.worker_queue.get_nowait()
                processed += 1
                if kind == "status":
                    self._set_status(payload)
                elif kind == "refresh_failed":
                    self._finish_refresh_failed(*payload)
                elif kind == "refresh_done":
                    self._finish_refresh_data(*payload)
                elif kind == "refresh_error":
                    self._finish_refresh_error(payload)
                elif kind == "render_done":
                    self._finish_render_payload(payload)
                elif kind == "render_error":
                    self._finish_render_error(payload)
        except queue.Empty:
            pass

        if self.is_busy or not self.worker_queue.empty():
            self.worker_poll_after_id = self.after(80, self._poll_worker_queue)
        else:
            self.worker_poll_after_id = None

    def _selected_strategy_mode(self) -> str:
        return "stop_checking_price" if self.strategy_var.get() == "Stop Checking Price" else "hybrid"

    def _selected_force_rebalance(self) -> bool:
        return bool(self.force_rebalance_var.get())

    def _show_empty_state(self) -> None:
        self.file_label.config(text="目前：尚未選擇 ticker 清單")
        self.snapshot_label.config(text="快照：尚未更新")
        self.metric_universe.value_label.config(text="0")
        self.metric_candidates.value_label.config(text="0")
        self.metric_excluded.value_label.config(text="0")
        self.metric_snapshot.value_label.config(text="尚未更新")
        self.summary_stats.update_idletasks()
        self._set_status("等待操作：可先選清單，或直接按「更新並篩選」使用預設清單。")
        self.busy_var.set("就緒")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(
            tk.END,
            "操作方式：\n\n"
            "1. 點「選擇 ticker 清單」載入自己的股票清單，或直接使用預設清單。\n"
            "2. 選擇策略模式。\n"
            "3. 按「更新並篩選」，系統會自動抓資料並排序。\n",
        )
        self.exclusion_text.delete("1.0", tk.END)
        self.exclusion_text.insert(tk.END, "目前還沒有被排除的股票。")
        self.output.delete("1.0", tk.END)
        self.output.insert(tk.END, "這裡會顯示完整報告。")
        rows = self.tree.get_children()
        if rows:
            self.tree.delete(*rows)

    def choose_file(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self,
            title="選擇 ticker 清單或 snapshot",
            filetypes=[
                ("支援的資料檔", "*.json *.jsonl *.ndjson *.csv"),
                ("JSON", "*.json"),
                ("JSONL", "*.jsonl *.ndjson"),
                ("CSV", "*.csv"),
                ("所有檔案", "*.*"),
            ],
        )
        if not file_path:
            self.after_idle(self._focus_main_window)
            return
        self.selected_path = Path(file_path)
        self.current_mode = "watchlist"
        self.file_label.config(text=f"目前：{self.selected_path.name}")
        self._set_status("已選擇清單。下一步按「更新並篩選」。")
        self.busy_var.set("就緒")
        self.after_idle(self._focus_main_window)

    def _load_watchlist_tickers(self, selected_path: Path | None = None) -> list[str]:
        selected_path = selected_path if selected_path is not None else self.selected_path
        if selected_path is not None:
            return load_watchlist(selected_path)
        if DEFAULT_WATCHLIST_PATH.exists():
            return load_watchlist(DEFAULT_WATCHLIST_PATH)
        return ["AAPL", "MSFT", "NVDA"]

    def refresh_data(self) -> None:
        if self.is_busy:
            self._set_status("目前正在執行，請等它完成。")
            return
        selected_path = self.selected_path
        strategy_mode = self._selected_strategy_mode()
        force_rebalance = self._selected_force_rebalance()
        self._set_busy(True, "更新中：正在抓取 snapshot...")
        self._set_status("資料更新中，請稍候。抓取完成後會自動計算排名。")
        self._start_worker_polling()
        self.after(20, lambda: self._refresh_data_async(selected_path, strategy_mode, force_rebalance))

    def _refresh_data_async(self, selected_path: Path | None, strategy_mode: str, force_rebalance: bool) -> None:
        def worker() -> None:
            try:
                tickers = self._load_watchlist_tickers(selected_path)
                if not tickers:
                    raise ValueError("找不到任何 ticker，請先選一份清單")
                source_text = selected_path.name if selected_path is not None else DEFAULT_WATCHLIST_PATH.name
                bundle = fetch_snapshot(tickers)
                if bundle.status == "failed":
                    self.worker_queue.put(("refresh_failed", (tickers, bundle, source_text)))
                    return
                self.worker_queue.put(("status", "資料已抓到，正在儲存 snapshot 並計算排名。"))
                snapshot_path = save_snapshot(bundle, DEFAULT_SNAPSHOT_PATH)
                payload = self._build_render_payload(
                    bundle.records,
                    f"yfinance snapshot ({source_text})",
                    bundle=bundle,
                    strategy_mode=strategy_mode,
                    force_rebalance=force_rebalance,
                )
                self.worker_queue.put(("refresh_done", (tickers, bundle, snapshot_path, payload, source_text)))
            except Exception as exc:
                self.worker_queue.put(("refresh_error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_refresh_failed(self, tickers, bundle, source_text: str) -> None:
        self.file_label.config(text=f"目前：{source_text}")
        self.snapshot_bundle = None
        self.current_mode = "watchlist"
        self.snapshot_label.config(text="快照：抓取失敗")
        self.metric_universe.value_label.config(text=str(len(tickers)))
        self.metric_candidates.value_label.config(text="0")
        self.metric_excluded.value_label.config(text="0")
        self.metric_snapshot.value_label.config(text="抓取失敗")
        rows = self.tree.get_children()
        if rows:
            self.tree.delete(*rows)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(
            tk.END,
            "這次沒有抓到任何可用市場資料，所以沒有產生候選名單。\n\n"
            "通常是網路連不上 Yahoo Finance，或 Yahoo 暫時擋住了這個資料來源。\n"
            "你可以稍後再試，或先看空白狀態確認版面。",
        )
        self.exclusion_text.delete("1.0", tk.END)
        self.exclusion_text.insert(tk.END, "這次沒有可評分的資料，因此不顯示硬篩排除名單。")
        self.output.delete("1.0", tk.END)
        self.output.insert(
            tk.END,
            "yfinance 抓取失敗，沒有產生有效 snapshot。\n\n"
            + "\n".join(f"- {warning}" for warning in bundle.warnings[:10]),
        )
        self._set_status("抓取失敗：請檢查網路或稍後再試。")
        self._set_busy(False, "抓取失敗")
        messagebox.showwarning("抓取失敗", "這次沒有抓到可用市場資料，所以沒有產生候選名單。", parent=self)

    def _finish_refresh_data(self, tickers, bundle, snapshot_path: Path, payload, source_text: str) -> None:
        self.file_label.config(text=f"目前：{source_text}")
        self.snapshot_bundle = bundle
        self.current_snapshot_path = snapshot_path
        self.current_mode = "snapshot"
        self.snapshot_label.config(text=f"快照：{self.current_snapshot_path.name} | {bundle.as_of} | {bundle.fetched_at}")
        self._apply_render_payload(payload)
        self._set_status("資料已更新完成。")
        self._set_busy(False, "就緒")

    def _finish_refresh_error(self, exc: Exception) -> None:
        if isinstance(exc, YFinanceUnavailableError):
            messagebox.showerror("缺少 yfinance", f"{exc}\n\n請先執行：python3 -m pip install yfinance", parent=self)
            self._set_status("缺少 yfinance，無法更新資料。")
            self._set_busy(False, "缺少 yfinance")
            return
        messagebox.showerror("無法更新資料", str(exc), parent=self)
        self._set_status("更新失敗，請確認清單檔案格式。")
        self._set_busy(False, "更新失敗")

    def run_screener(self) -> None:
        if self.is_busy:
            self._set_status("目前正在執行，請等它完成。")
            return
        current_mode = self.current_mode
        snapshot_bundle = self.snapshot_bundle
        current_snapshot_path = self.current_snapshot_path
        selected_path = self.selected_path
        strategy_mode = self._selected_strategy_mode()
        force_rebalance = self._selected_force_rebalance()
        self._set_busy(True, "篩選中：正在計算分數...")
        self._set_status("候選計算中，請稍候。")
        self._start_worker_polling()
        self.after(
            20,
            lambda: self._run_screener_async(
                current_mode,
                snapshot_bundle,
                current_snapshot_path,
                selected_path,
                strategy_mode,
                force_rebalance,
            ),
        )

    def _run_screener_async(
        self,
        current_mode: str,
        snapshot_bundle,
        current_snapshot_path: Path,
        selected_path: Path | None,
        strategy_mode: str,
        force_rebalance: bool,
    ) -> None:
        def worker() -> None:
            try:
                if current_mode == "snapshot" and snapshot_bundle is not None:
                    self.worker_queue.put(("status", "正在用目前 snapshot 重新計算排名。"))
                    payload = self._build_render_payload(
                        snapshot_bundle.records,
                        f"yfinance snapshot ({current_snapshot_path.name})",
                        bundle=snapshot_bundle,
                        strategy_mode=strategy_mode,
                        force_rebalance=force_rebalance,
                    )
                    self.worker_queue.put(("render_done", payload))
                    return
                if selected_path is None:
                    self.worker_queue.put(("render_error", ValueError("請先按「選擇 ticker 清單」，再按「更新並篩選」。")))
                    return
                self.worker_queue.put(("status", "正在讀取資料檔並計算排名。"))
                records = load_records(selected_path)
                payload = self._build_render_payload(
                    records,
                    selected_path.name,
                    strategy_mode=strategy_mode,
                    force_rebalance=force_rebalance,
                )
                self.worker_queue.put(("render_done", payload))
            except Exception as exc:
                self.worker_queue.put(("render_error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_render_payload(self, payload) -> None:
        self._apply_render_payload(payload)
        self._set_busy(False, "就緒")

    def _finish_render_error(self, exc: Exception) -> None:
        messagebox.showinfo(
            "先更新並篩選",
            "如果你選的是 ticker 清單，請先按「更新並篩選」。\n"
            "如果你選的是完整資料檔，按它也可以直接產生結果。\n\n"
            f"錯誤：{exc}",
            parent=self,
        )
        self._set_status("篩選未完成，請確認資料檔或先更新資料。")
        self._set_busy(False, "就緒")

    def _build_render_payload(
        self,
        records,
        source_name: str,
        *,
        bundle=None,
        strategy_mode: str,
        force_rebalance: bool,
    ):
        as_of = None
        if bundle is not None and getattr(bundle, "as_of", None):
            try:
                as_of = date.fromisoformat(str(bundle.as_of))
            except ValueError:
                as_of = None
        report = build_report(
            records,
            self.config,
            strategy_mode=strategy_mode,
            as_of=as_of,
            force_rebalance=force_rebalance,
        )
        table_rows = []
        for index, item in enumerate(report.candidates, start=1):
            if report.strategy_mode == "stop_checking_price":
                reasons = f"{item.suggested_action or 'WATCHLIST'}｜{'；'.join(item.reasons[:1]) if item.reasons else '等待審核'}"
            else:
                reasons = "；".join(item.reasons[:2]) if item.reasons else "等待審核"
            risk = "；".join(item.risk_warnings[:2]) if item.risk_warnings else "無明顯風險"
            table_rows.append(
                (
                    item.ticker,
                    (
                        index,
                        item.ticker,
                        item.total_score if item.total_score is not None else "",
                        _format_sector_relative_preview(item),
                        reasons,
                        risk,
                    ),
                )
            )
        return {
            "report": report,
            "bundle": bundle,
            "table_rows": table_rows,
            "exclusion_text": self._build_exclusion_text(report),
            "output_text": report_to_text(report, source_name, bundle=bundle),
        }

    def _render_records(self, records, source_name: str, bundle=None) -> None:
        payload = self._build_render_payload(
            records,
            source_name,
            bundle=bundle,
            strategy_mode=self._selected_strategy_mode(),
            force_rebalance=self._selected_force_rebalance(),
        )
        self._apply_render_payload(payload)

    def _apply_render_payload(self, payload) -> None:
        report = payload["report"]
        bundle = payload.get("bundle")
        self.current_report = report
        self.candidate_lookup = {item.ticker: item for item in report.candidates}
        self.selected_candidate_ticker = None

        self.metric_universe.value_label.config(text=str(report.universe_size))
        self.metric_candidates.value_label.config(text=str(len(report.candidates)))
        self.metric_excluded.value_label.config(text=str(len(report.hard_excluded)))
        if bundle is not None:
            self.metric_snapshot.value_label.config(text=bundle.as_of)
            self.snapshot_label.config(text=f"快照：{bundle.as_of} | {bundle.fetched_at}")
        else:
            self.metric_snapshot.value_label.config(text="手動資料")
        review_text = "是" if report.review_mode == "quarterly_rebalance" else "否"
        self._set_status(
            f"完成。策略模式：{report.strategy_mode}；季度檢查：{review_text}；硬篩通過 {report.hard_pass_count} 檔；目前只顯示前 {len(report.candidates)} 名。"
        )

        rows = self.tree.get_children()
        if rows:
            self.tree.delete(*rows)

        for ticker, values in payload["table_rows"]:
            self.tree.insert(
                "",
                "end",
                iid=ticker,
                values=values,
            )

        if report.candidates:
            self.tree.selection_set(report.candidates[0].ticker)
            self.tree.focus(report.candidates[0].ticker)
            self._show_candidate_details(report.candidates[0].ticker)
        else:
            self.detail_text.delete("1.0", tk.END)
            self.detail_text.insert(tk.END, "沒有通過硬篩條件的股票。")

        self.exclusion_text.delete("1.0", tk.END)
        self.exclusion_text.insert(tk.END, payload["exclusion_text"])
        self.output.delete("1.0", tk.END)
        self.output.insert(tk.END, payload["output_text"])

    def _build_exclusion_text(self, report) -> str:
        lines = ["硬性剔除："]
        if report.hard_excluded:
            for item in report.hard_excluded:
                detail = item.exclusion_details[0] if item.exclusion_details else {}
                lines.append(
                    f"- {item.ticker}：{item.excluded_reason}"
                    f"｜類別 {detail.get('category', 'N/A')}"
                    f"｜原始值 {detail.get('raw_value', 'N/A')}"
                    f"｜正規化 {detail.get('normalized_value', 'N/A')}"
                    f"｜門檻 {detail.get('threshold', 'N/A')}"
                )
        else:
            lines.append("沒有硬性剔除的股票。")
        lines.extend(["", "扣分標記："])
        if report.soft_penalties:
            for item in report.soft_penalties:
                reasons = "；".join(item.get("reasons", [])) or "有扣分"
                lines.append(f"- {item['ticker']}：扣分 {item.get('penalty_score')} 分，{reasons}")
        else:
            lines.append("沒有扣分標記。")
        lines.extend(["", "資料缺口提示："])
        if report.missing_data_warnings:
            for item in report.missing_data_warnings:
                missing_fields = "、".join(item.get("missing_fields", [])) or "部分欄位缺失"
                lines.append(f"- {item['ticker']}：{missing_fields}")
        else:
            lines.append("沒有資料缺口提示。")
        return "\n".join(lines)

    def _on_tree_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self._show_candidate_details(selection[0])

    def _show_candidate_details(self, ticker: str) -> None:
        item = self.candidate_lookup.get(ticker)
        if item is None:
            return
        lines = [
            f"{item.ticker}",
            f"總分：{item.total_score}",
            f"Legacy score：{item.legacy_total_score if item.legacy_total_score is not None else 'N/A'}",
            f"原始分：{item.raw_score}",
            f"扣分：{item.penalty_score}",
            f"信心倍率：{item.confidence_multiplier}",
            f"資料品質：{item.data_quality_score if item.data_quality_score is not None else 'N/A'}",
            f"動作限制：{item.action_cap_reason or '無'}",
            f"最終分：{item.final_score}",
            f"Sector-aware preview：{_format_sector_relative_preview(item) or 'N/A'}",
            f"Peer source：{_format_sector_relative_peer_source(item) or 'N/A'}",
            f"Peer count：{item.sector_relative_peer_count if item.sector_relative_peer_count is not None else 'N/A'}",
            f"基本面：{item.factor_scores.get('fundamental')}",
            f"動量：{item.factor_scores.get('momentum')}",
            f"風險安全：{item.factor_scores.get('risk_safety')}",
            "",
            "入選理由：",
        ]
        if item.suggested_action:
            lines.insert(2, f"動作：{item.suggested_action}")
        for reason in item.reasons:
            lines.append(f"- {reason}")
        if item.risk_warnings:
            lines.append("")
            lines.append("風險提醒：")
            for warning in item.risk_warnings:
                lines.append(f"- {warning}")
        if item.confidence_notes:
            lines.append("")
            lines.append("資料提醒：")
            for note in item.confidence_notes:
                lines.append(f"- {note}")
        if item.data_quality_flags:
            lines.append("")
            lines.append("資料品質旗標：")
            for flag in item.data_quality_flags:
                lines.append(f"- {flag}")
        if item.normalization_notes:
            lines.append("")
            lines.append("正規化備註：")
            for note in item.normalization_notes:
                lines.append(f"- {note}")
        if item.sector_relative_factor_scores:
            lines.append("")
            lines.append("Sector-aware factor preview：")
            for key, value in item.sector_relative_factor_scores.items():
                lines.append(f"- {key}: {value}")
        if item.sector_relative_notes:
            lines.append("")
            lines.append("Sector-aware notes：")
            for note in item.sector_relative_notes:
                lines.append(f"- {note}")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(tk.END, "\n".join(lines))

    def save_report(self) -> None:
        if self.current_report is None:
            messagebox.showinfo("還沒有結果", "先按一次「選擇 ticker 清單」或「更新並篩選」。", parent=self)
            return
        output_path = filedialog.asksaveasfilename(
            parent=self,
            title="儲存報告",
            defaultextension=".txt",
            filetypes=[("文字檔", "*.txt"), ("所有檔案", "*.*")],
        )
        if not output_path:
            self.after_idle(self._focus_main_window)
            return
        Path(output_path).write_text(self.output.get("1.0", tk.END), encoding="utf-8")
        self._set_status(f"已儲存到 {output_path}")
        self.after_idle(self._focus_main_window)


def report_to_text(report, source_name: str, bundle=None) -> str:
    lines = [
        f"來源：{source_name}",
        f"策略模式：{report.strategy_mode}",
        f"季度檢查：{'是' if report.review_mode == 'quarterly_rebalance' else '否'}",
        f"輸入 {report.universe_size} 檔",
        f"min_score：{report.min_score if report.min_score is not None else '未設定'}",
        f"effective_min_score_source：{report.effective_min_score_source}",
        f"top_n：{report.top_n}",
        f"dedupe_company：{'啟用' if report.dedupe_company else '未啟用'}",
        f"硬篩通過 {report.hard_pass_count} 檔",
        f"顯示前 {len(report.candidates)} 名",
        f"hard_exclusion count：{len(report.hard_excluded)}",
        f"soft_penalty count：{len(report.soft_penalties)}",
        f"missing_data count：{len(report.missing_data_warnings)}",
        f"retry_failed_count：{report.retry_failed_count}",
        f"fetch_failed_count：{report.fetch_failed_count}",
        f"dedupe_removed_count：{report.dedupe_removed_count}",
        f"ranking_style：{report.ranking_style}",
        f"top_n_average_total_score：{report.top_n_average_total_score}",
        f"top_n_average_fundamental_score：{report.top_n_average_fundamental_score}",
        f"top_n_average_momentum_score：{report.top_n_average_momentum_score}",
        f"top_n_average_risk_safety_score：{report.top_n_average_risk_safety_score}",
        f"high_risk_candidate_count：{report.high_risk_candidate_count}",
        f"expensive_candidate_count：{report.expensive_candidate_count}",
        f"high_volatility_candidate_count：{report.high_volatility_candidate_count}",
        f"deep_drawdown_candidate_count：{report.deep_drawdown_candidate_count}",
        f"missing_data_candidate_count：{report.missing_data_candidate_count}",
        f"sector_aware_official_scoring：{'啟用' if report.sector_aware_official_scoring else '未啟用'}",
        f"sector_aware_shadow_mode：{'啟用' if report.sector_aware_shadow_mode else '未啟用'}",
        f"sector_aware_preview_available_count：{report.sector_aware_preview_available_count}",
        f"sector_aware_preview_missing_count：{report.sector_aware_preview_missing_count}",
        f"sector_aware_average_score_delta：{report.sector_aware_average_score_delta}",
        f"sector_aware_rank_changed_count：{report.sector_aware_rank_changed_count}",
        f"sector_aware_preview_coverage：{report.sector_aware_preview_coverage}",
        f"sector_aware_score_correlation_with_current：{report.sector_aware_score_correlation_with_current}",
        f"sector_aware_top_10_overlap：{report.sector_aware_top_10_overlap} / {report.sector_aware_top_10_overlap_total}",
        f"sector_aware_sector_peer_used_count：{report.sector_aware_sector_peer_used_count}",
        f"sector_aware_universe_fallback_count：{report.sector_aware_universe_fallback_count}",
        f"sector_aware_missing_sector_count：{report.sector_aware_missing_sector_count}",
        f"sector_aware_average_peer_count：{report.sector_aware_average_peer_count}",
        f"sector_aware_min_peer_count：{report.sector_aware_min_peer_count}",
        f"sector_aware_max_peer_count：{report.sector_aware_max_peer_count}",
        f"sector_aware_large_rank_change_count：{report.sector_aware_large_rank_change_count}（threshold {report.sector_aware_large_rank_change_threshold}）",
    ]
    if report.sector_aware_top_movers_up:
        movers = "；".join(
            f"{item['ticker']} rank_delta {item.get('rank_delta')} score_delta {item.get('score_delta')}"
            for item in report.sector_aware_top_movers_up
        )
        lines.append(f"sector_aware_top_movers_up：{movers}")
    if report.sector_aware_top_movers_down:
        movers = "；".join(
            f"{item['ticker']} rank_delta {item.get('rank_delta')} score_delta {item.get('score_delta')}"
            for item in report.sector_aware_top_movers_down
        )
        lines.append(f"sector_aware_top_movers_down：{movers}")
    if report.sector_aware_largest_movers:
        movers = "；".join(
            f"{item['ticker']} rank_delta {item.get('rank_delta')} score_delta {item.get('score_delta')}"
            for item in report.sector_aware_largest_movers[:5]
        )
        lines.append(f"sector_aware_largest_movers：{movers}")
    if report.strategy_mode == "hybrid" and report.ranking_style == "momentum_driven":
        lines.append("診斷提醒：本次 hybrid 排名偏動量導向，適合作為候選初篩，不代表低風險或長期品質排序。")
    if bundle is not None:
        lines.append(f"快照日期：{bundle.as_of}")
        lines.append(f"抓取時間：{bundle.fetched_at}")
        if bundle.warnings:
            lines.append("")
            lines.append("抓取提醒")
            for warning in bundle.warnings:
                lines.append(f"- {warning}")

    lines.extend(["", "候選名單", ""])
    for index, item in enumerate(report.candidates, start=1):
        lines.append(f"{index}. {item.ticker}")
        lines.append(f"   總分：{item.total_score}")
        lines.append(f"   Legacy score：{item.legacy_total_score if item.legacy_total_score is not None else 'N/A'}")
        lines.append(f"   Sector-aware preview：{_format_sector_relative_preview(item) or 'N/A'}")
        lines.append(f"   Peer source：{_format_sector_relative_peer_source(item) or 'N/A'}")
        lines.append(f"   Peer count：{item.sector_relative_peer_count if item.sector_relative_peer_count is not None else 'N/A'}")
        if item.suggested_action:
            lines.append(f"   動作：{item.suggested_action}")
        if item.confidence_score is not None:
            lines.append(f"   信心：{item.confidence_label} ({item.confidence_score})")
        if item.data_quality_score is not None:
            lines.append(f"   資料品質：{item.data_quality_score}")
        if item.action_cap_reason:
            lines.append(f"   動作限制：{item.action_cap_reason}")
        lines.append(f"   基本面：{item.factor_scores.get('fundamental')}")
        lines.append(f"   動量：{item.factor_scores.get('momentum')}")
        lines.append(f"   風險安全：{item.factor_scores.get('risk_safety')}")
        if item.reasons:
            lines.append(f"   理由：{'；'.join(item.reasons)}")
        if item.risk_warnings:
            lines.append(f"   風險：{'；'.join(item.risk_warnings)}")
        if item.confidence_notes:
            lines.append(f"   提醒：{'；'.join(item.confidence_notes)}")
        if item.data_quality_flags:
            lines.append(f"   資料品質旗標：{'；'.join(item.data_quality_flags)}")
        if item.normalization_notes:
            lines.append(f"   正規化備註：{'；'.join(item.normalization_notes)}")
        if item.sector_relative_factor_scores:
            preview_parts = [
                f"{key}={value}"
                for key, value in item.sector_relative_factor_scores.items()
            ]
            lines.append(f"   Sector-aware factor preview：{'；'.join(preview_parts)}")
        if item.sector_relative_notes:
            lines.append(f"   Sector-aware notes：{'；'.join(item.sector_relative_notes)}")
        if item.penalties:
            penalty_text = "；".join(
                f"{penalty.get('reason')} -{penalty.get('points')}分"
                for penalty in item.penalties
            )
            lines.append(f"   扣分：{item.penalty_score}（{penalty_text}）")
        lines.append("")

    if report.hard_excluded:
        lines.append("硬性剔除")
        lines.append("")
        for item in report.hard_excluded:
            detail = item.exclusion_details[0] if item.exclusion_details else {}
            lines.append(
                f"- {item.ticker}：{item.excluded_reason}"
                f"｜類別 {detail.get('category', 'N/A')}"
                f"｜嚴重度 {detail.get('severity', 'normal')}"
                f"｜原始值 {detail.get('raw_value', 'N/A')}"
                f"｜正規化 {detail.get('normalized_value', 'N/A')}"
                f"｜門檻 {detail.get('threshold', 'N/A')}"
            )
            if item.confidence_notes:
                lines.append(f"  提醒：{'；'.join(item.confidence_notes)}")
    if report.soft_penalties:
        lines.append("")
        lines.append("扣分標記")
        lines.append("")
        for item in report.soft_penalties:
            reasons = "；".join(item.get("reasons", [])) or "有扣分"
            lines.append(f"- {item['ticker']}：扣分 {item.get('penalty_score')} 分，{reasons}")
    if report.missing_data_warnings:
        lines.append("")
        lines.append("資料缺口提示")
        lines.append("")
        for item in report.missing_data_warnings:
            missing_fields = "、".join(item.get("missing_fields", [])) or "部分欄位缺失"
            cap = f"｜動作限制：{item.get('action_cap_reason')}" if item.get("action_cap_reason") else ""
            lines.append(f"- {item['ticker']}：{missing_fields}{cap}")
    return "\n".join(lines)


def main() -> int:
    app = ScreenerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
