from __future__ import annotations

import json
import queue
import socket
import sys
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .auction import AuctionEngine
from .backtest import AuctionBacktester
from .big_movement import BigMovementMonitor, MovementCandidate
from .candidate_sync import ThsWatchlistController, calculate_candidate_zones, zone_summary
from .database import Database
from .mobile_notifications import AlertEvent, NotificationJob
from .mobile_web import MobileWebServer
from .models import Analysis, Position
from .monitor import MonitorEngine
from .security import hash_password, new_access_token, protect_secret, unprotect_secret
from .startup import set_startup
from .ths_window import capture_window, find_ths_windows
from .tray import TrayIcon


RISK_TEXT = {1: "1级 正常", 2: "2级 轻微注意", 3: "3级 短线风险增加", 4: "4级 明显风险", 5: "5级 强风险"}
RISK_COLORS = {1: "#16875b", 2: "#777b22", 3: "#c47a13", 4: "#d54c32", 5: "#b42332"}


class StockMonitorApp:
    def __init__(self, root: tk.Tk, database: Database, base_dir: Path, background: bool = False):
        self.root = root
        self.db = database
        self.base_dir = base_dir
        self.events: queue.Queue[Any] = queue.Queue()
        self.monitor = MonitorEngine(database, self.events)
        self.auction = AuctionEngine(database, self.events)
        self.movement = BigMovementMonitor(self.events)
        self.backtester = AuctionBacktester(database)
        self.ths_watchlist = ThsWatchlistController()
        self.web = MobileWebServer(base_dir / "stock_monitor" / "web", self.db.get_settings, self._web_payload)
        self.tray = TrayIcon(self.events)
        self._shutting_down = False
        self.selected_code: str | None = None
        self._build_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.tray.start()
        self.refresh_positions()
        self.auction.start()
        self._sync_web_server(show_errors=False)
        self.root.after(250, self._drain_events)
        if background:
            self.root.withdraw()
        settings = self.db.get_settings()
        if background or settings.get("monitor_enabled") == "1":
            self.monitor.start()

    def _build_style(self) -> None:
        self.root.title("股票持仓监控助手")
        self.root.geometry("1160x760")
        self.root.minsize(940, 620)
        self.root.configure(bg="#f4f6f8")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f4f6f8", foreground="#20262e")
        style.configure("TButton", padding=(12, 7), background="#ffffff", bordercolor="#c9d1d9")
        style.map("TButton", background=[("active", "#edf2f5")])
        style.configure("Accent.TButton", background="#176b57", foreground="white", bordercolor="#176b57")
        style.map("Accent.TButton", background=[("active", "#125746")])
        style.configure("Treeview", rowheight=34, fieldbackground="#ffffff", background="#ffffff", borderwidth=0)
        style.configure("Treeview.Heading", padding=(8, 9), background="#e8edf0", foreground="#36414c", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#dcebe7")], foreground=[("selected", "#1d2a2d")])
        style.configure("TNotebook", background="#f4f6f8", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), background="#e7ecef")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", "#176b57")])

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#ffffff", height=72, highlightbackground="#dfe5e8", highlightthickness=1)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_box = tk.Frame(header, bg="#ffffff")
        title_box.pack(side="left", padx=22, pady=12)
        tk.Label(title_box, text="股票持仓监控助手", bg="#ffffff", fg="#17212b", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        tk.Label(title_box, text="只做监控、分析与风险提醒，不执行交易", bg="#ffffff", fg="#69747e", font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        controls = tk.Frame(header, bg="#ffffff")
        controls.pack(side="right", padx=18, pady=15)
        ttk.Button(controls, text="添加持仓", style="Accent.TButton", command=self.show_add_dialog).pack(side="left", padx=4)
        ttk.Button(controls, text="刷新行情", command=self.monitor.refresh_now).pack(side="left", padx=4)
        self.monitor_button = ttk.Button(controls, text="开始监控", command=self.toggle_monitor)
        self.monitor_button.pack(side="left", padx=4)
        ttk.Button(controls, text="读取当前同花顺", command=self.read_ths).pack(side="left", padx=4)

        body = tk.Frame(self.root, bg="#f4f6f8")
        body.pack(fill="both", expand=True, padx=18, pady=(14, 10))
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        self.dashboard_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.auction_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.movement_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.backtest_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.detail_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.alert_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.notification_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.settings_tab = tk.Frame(self.notebook, bg="#ffffff")
        self.notebook.add(self.dashboard_tab, text="持仓总览")
        self.notebook.add(self.auction_tab, text="9:25竞价候选")
        self.notebook.add(self.movement_tab, text="盘中异动")
        self.notebook.add(self.backtest_tab, text="历史回测")
        self.notebook.add(self.detail_tab, text="个股详情")
        self.notebook.add(self.alert_tab, text="提醒记录")
        self.notebook.add(self.notification_tab, text="通知设置")
        self.notebook.add(self.settings_tab, text="设置")
        self._build_dashboard()
        self._build_auction()
        self._build_movement()
        self._build_backtest()
        self._build_detail()
        self._build_alerts()
        self._build_notification_settings()
        self._build_settings()
        self.status_var = tk.StringVar(value="准备就绪")
        status = tk.Label(self.root, textvariable=self.status_var, anchor="w", bg="#e9eef0", fg="#55616b", padx=18, pady=7, font=("Microsoft YaHei UI", 9))
        status.pack(fill="x")

    def _build_auction(self) -> None:
        settings = self.db.get_settings()
        toolbar = tk.Frame(self.auction_tab, bg="#ffffff")
        toolbar.pack(fill="x", padx=16, pady=(14, 8))
        title_box = tk.Frame(toolbar, bg="#ffffff")
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text="9:25锁单决策", bg="#ffffff", fg="#17212b", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        self.auction_status_var = tk.StringVar(value="等待竞价时段；9:25后不会用盘中数据回填")
        tk.Label(title_box, textvariable=self.auction_status_var, bg="#ffffff", fg="#69747e", anchor="w").pack(anchor="w", pady=(2, 0))
        self.auction_performance_var = tk.StringVar(value="保守试验版：未通过正式回测审核")
        tk.Label(title_box, textvariable=self.auction_performance_var, bg="#ffffff", fg="#0c8279", anchor="w").pack(anchor="w", pady=(2, 0))
        auction_enabled = settings.get("auction_enabled", "1") == "1"
        self.auction_toggle_button = ttk.Button(toolbar, text="暂停自动竞价" if auction_enabled else "启动自动竞价", command=self.toggle_auction)
        self.auction_toggle_button.pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="立即采样", command=self.auction.capture_now).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="测试Infoway", command=self.auction.test_infoway_source).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="读取今日结果", command=self.refresh_auction_results).pack(side="right")

        config = tk.Frame(self.auction_tab, bg="#f8fafb", highlightbackground="#e1e7ea", highlightthickness=1)
        config.pack(fill="x", padx=16, pady=(0, 8))
        self.auction_setting_vars: dict[str, tk.StringVar] = {
            "infoway_request_interval": tk.StringVar(value=settings.get("infoway_request_interval", "1.05")),
            "auction_min_amount": tk.StringVar(value=f"{float(settings.get('auction_min_amount', '20000000')) / 10000:g}"),
            "auction_high_price": tk.StringVar(value=settings.get("auction_high_price", "100")),
            "auction_min_retention": tk.StringVar(value=f"{float(settings.get('auction_min_retention', '0.70')) * 100:g}"),
            "auction_late_drop": tk.StringVar(value=settings.get("auction_late_drop", "0.50")),
            "auction_max_drawdown": tk.StringVar(value=settings.get("auction_max_drawdown", "2.00")),
            "auction_min_float_amount_ratio": tk.StringVar(value=f"{float(settings.get('auction_min_float_amount_ratio', '0.0005')) * 100:g}"),
            "auction_min_market_positive_ratio": tk.StringVar(value=f"{float(settings.get('auction_min_market_positive_ratio', '0.35')) * 100:g}"),
            "auction_max_orderable": tk.StringVar(value=settings.get("auction_max_orderable", "1")),
            "auction_pool_max_size": tk.StringVar(value=settings.get("auction_pool_max_size", "500")),
            "auction_pool_max_per_sector": tk.StringVar(value=settings.get("auction_pool_max_per_sector", "5")),
            "auction_pool_min_amount": tk.StringVar(value=f"{float(settings.get('auction_pool_min_amount', '5000000')) / 10000:g}"),
        }
        self.auction_source_var = tk.StringVar(value=settings.get("auction_data_source", "eastmoney"))
        self.infoway_key_var = tk.StringVar(value=unprotect_secret(settings.get("secret_infoway_key", "")))
        source_box = tk.Frame(config, bg="#f8fafb")
        source_box.grid(row=0, column=5, rowspan=4, sticky="nw", padx=(12, 8), pady=6)
        tk.Label(source_box, text="竞价数据源", bg="#f8fafb", fg="#56636d", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        ttk.Combobox(source_box, textvariable=self.auction_source_var, values=("eastmoney", "infoway"), state="readonly", width=12).pack(anchor="w", pady=(2, 6))
        tk.Label(source_box, text="Infoway API Key", bg="#f8fafb", fg="#56636d", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        ttk.Entry(source_box, textvariable=self.infoway_key_var, width=34, show="*").pack(anchor="w", pady=(2, 6))
        tk.Label(source_box, text="请求间隔(秒)", bg="#f8fafb", fg="#56636d", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        ttk.Entry(source_box, textvariable=self.auction_setting_vars["infoway_request_interval"], width=13).pack(anchor="w", pady=(2, 0))
        fields = (
            ("auction_min_amount", "最低竞价额(万元)"), ("auction_high_price", "高价股阈值(元)"),
            ("auction_min_retention", "最低留存率(%)"), ("auction_late_drop", "末段下降(百分点)"),
            ("auction_max_drawdown", "最大回撤(百分点)"),
            ("auction_min_float_amount_ratio", "竞价额/流通市值(%)"),
            ("auction_min_market_positive_ratio", "市场正竞价最低(%)"),
            ("auction_max_orderable", "每日可挂入上限"),
            ("auction_pool_max_size", "9:20后跟踪上限"),
            ("auction_pool_max_per_sector", "每板块入池上限"),
            ("auction_pool_min_amount", "入池竞价额(万元)"),
        )
        for col, (key, label) in enumerate(fields):
            box = tk.Frame(config, bg="#f8fafb")
            box.grid(row=col // 5, column=col % 5, padx=(10 if col % 5 == 0 else 5, 5), pady=6, sticky="w")
            tk.Label(box, text=label, bg="#f8fafb", fg="#56636d", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            ttk.Entry(box, textvariable=self.auction_setting_vars[key], width=13).pack(anchor="w", pady=(2, 0))
        self.auction_option_vars: dict[str, tk.BooleanVar] = {}
        options = (
            ("auction_include_chinext", "创业板"), ("auction_include_star", "科创板"),
            ("auction_include_bse", "北交所"), ("auction_include_st", "ST/退市"),
            ("auction_include_high_price", "高价股"),
        )
        option_box = tk.Frame(config, bg="#f8fafb")
        option_box.grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))
        tk.Label(option_box, text="手动放开：", bg="#f8fafb", fg="#56636d").pack(side="left")
        for key, label in options:
            variable = tk.BooleanVar(value=settings.get(key, "0") == "1")
            self.auction_option_vars[key] = variable
            ttk.Checkbutton(option_box, text=label, variable=variable).pack(side="left", padx=(0, 10))
        ttk.Button(config, text="保存竞价设置", style="Accent.TButton", command=self.save_auction_settings).grid(row=3, column=4, sticky="e", padx=8, pady=(0, 8))

        filter_bar = tk.Frame(self.auction_tab, bg="#ffffff")
        filter_bar.pack(fill="x", padx=16, pady=(0, 8))
        self.auction_summary_var = tk.StringVar(value="尚未生成今日锁单结果")
        tk.Label(filter_bar, textvariable=self.auction_summary_var, bg="#ffffff", fg="#35424b").pack(side="left")
        ttk.Button(filter_bar, text="加入同花顺自选", style="Accent.TButton", command=self.sync_candidates_to_ths).pack(side="right", padx=(8, 0))
        ttk.Button(filter_bar, text="计算三个区间", command=self.calculate_selected_candidate_zones).pack(side="right", padx=(8, 0))
        self.auction_filter_var = tk.StringVar(value="全部")
        for label in ("全部", "竞价小仓", "试验观察", "等待9:35确认", "放弃"):
            ttk.Radiobutton(filter_bar, text=label, value=label, variable=self.auction_filter_var, command=self._render_auction_rows).pack(side="right", padx=4)

        table_frame = tk.Frame(self.auction_tab, bg="#ffffff")
        table_frame.pack(fill="both", expand=True, padx=16)
        columns = ("rank", "quality", "code", "name", "industry", "lock", "peak", "drawdown", "retention", "amount", "sector", "false", "pullback", "action", "score", "fill", "price", "no_buy")
        self.auction_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=11)
        headings = {
            "rank": "排名", "quality": "数据", "code": "代码", "name": "名称", "industry": "板块", "lock": "9:25涨幅",
            "peak": "最高涨幅", "drawdown": "回撤", "retention": "留存率", "amount": "竞价额",
            "sector": "板块共振", "false": "虚假风险", "pullback": "冲高回落", "action": "建议动作",
            "score": "等级", "fill": "规则估计", "price": "建议挂单价", "no_buy": "不买条件",
        }
        widths = {"rank": 48, "quality": 66, "code": 72, "name": 96, "industry": 96, "lock": 78, "peak": 78, "drawdown": 68, "retention": 68, "amount": 82, "sector": 96, "false": 70, "pullback": 78, "action": 74, "score": 62, "fill": 72, "price": 112, "no_buy": 310}
        for col in columns:
            self.auction_tree.heading(col, text=headings[col])
            self.auction_tree.column(col, width=widths[col], minwidth=widths[col], anchor="w" if col in ("name", "industry", "price", "no_buy") else "center", stretch=col == "no_buy")
        self.auction_tree.tag_configure("竞价小仓", foreground="#087a58")
        self.auction_tree.tag_configure("试验观察", foreground="#6c5a14")
        self.auction_tree.tag_configure("等待9:35确认", foreground="#a36800")
        self.auction_tree.tag_configure("放弃", foreground="#b42332")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.auction_tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.auction_tree.xview)
        self.auction_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.auction_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1); table_frame.columnconfigure(0, weight=1)
        self.auction_tree.bind("<<TreeviewSelect>>", self._on_auction_select)

        detail = tk.Frame(self.auction_tab, bg="#f8fafb", height=112, highlightbackground="#e1e7ea", highlightthickness=1)
        detail.pack(fill="x", padx=16, pady=(8, 12))
        detail.pack_propagate(False)
        self.auction_detail_var = tk.StringVar(value="选择一只股票查看 9:20-9:25 路径、板块前排和淘汰原因。")
        tk.Label(detail, textvariable=self.auction_detail_var, bg="#f8fafb", fg="#35424b", justify="left", anchor="nw", wraplength=1080).pack(fill="both", expand=True, padx=12, pady=9)

    def _build_movement(self) -> None:
        toolbar = tk.Frame(self.movement_tab, bg="#ffffff")
        toolbar.pack(fill="x", padx=18, pady=(16, 8))
        title_box = tk.Frame(toolbar, bg="#ffffff")
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text="盘中大数据涨幅监控", bg="#ffffff", fg="#17212b", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        self.movement_status_var = tk.StringVar(value="扫描全A股票，识别低位启动、突然拉升和匀速上涨；仅作观察，不构成买入结论")
        tk.Label(title_box, textvariable=self.movement_status_var, bg="#ffffff", fg="#69747e", anchor="w").pack(anchor="w", pady=(2, 0))
        ttk.Button(toolbar, text="停止异动监控", command=self.stop_movement_monitor).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="开始异动监控", style="Accent.TButton", command=self.start_movement_monitor).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="扫描一次", command=self.scan_movement_once).pack(side="right")

        shell = tk.Frame(self.movement_tab, bg="#ffffff")
        shell.pack(fill="both", expand=True, padx=18, pady=(4, 14))
        shell.columnconfigure(0, weight=1)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(1, weight=1)

        tk.Label(shell, text="突然拉升", bg="#ffffff", fg="#b42332", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        tk.Label(shell, text="匀速上涨", bg="#ffffff", fg="#176b57", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 6))

        columns = ("code", "name", "industry", "price", "pct", "delta", "amount", "risk", "reason")
        headings = {
            "code": "代码", "name": "名称", "industry": "板块", "price": "现价", "pct": "涨幅",
            "delta": "变化", "amount": "成交额", "risk": "风险", "reason": "原因",
        }
        widths = {"code": 68, "name": 86, "industry": 90, "price": 62, "pct": 62, "delta": 62, "amount": 78, "risk": 48, "reason": 210}
        self.sudden_tree = self._create_movement_tree(shell, columns, headings, widths)
        self.steady_tree = self._create_movement_tree(shell, columns, headings, widths)
        self.sudden_tree.grid(row=1, column=0, sticky="nsew")
        self.steady_tree.grid(row=1, column=1, sticky="nsew", padx=(10, 0))

    def _create_movement_tree(
        self,
        parent: tk.Widget,
        columns: tuple[str, ...],
        headings: dict[str, str],
        widths: dict[str, int],
    ) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse", height=14)
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=widths[column], anchor="w" if column in ("name", "industry", "reason") else "center", stretch=column == "reason")
        return tree

    def scan_movement_once(self) -> None:
        self.movement_status_var.set("正在扫描全A股票...")
        self.movement.scan_once_async()

    def start_movement_monitor(self) -> None:
        self.movement.start(interval_seconds=180)
        self.movement_status_var.set("盘中异动监控已启动，每3分钟扫描一次全A股票")

    def stop_movement_monitor(self) -> None:
        self.movement.stop()
        self.movement_status_var.set("盘中异动监控已停止")

    def _render_movement_rows(self, sudden: list[MovementCandidate], steady: list[MovementCandidate]) -> None:
        def render(tree: ttk.Treeview, rows: list[MovementCandidate]) -> None:
            for item in tree.get_children():
                tree.delete(item)
            for row in rows:
                tree.insert(
                    "",
                    "end",
                    iid=f"{row.kind}:{row.code}",
                    values=(
                        row.code,
                        row.name,
                        row.industry,
                        f"{row.price:.2f}",
                        f"{row.pct:+.2f}%",
                        f"{row.delta_pct:+.2f}",
                        f"{row.amount / 10000:.0f}万",
                        row.risk,
                        row.reason,
                    ),
                )
        render(self.sudden_tree, sudden)
        render(self.steady_tree, steady)

    def _build_backtest(self) -> None:
        toolbar = tk.Frame(self.backtest_tab, bg="#ffffff")
        toolbar.pack(fill="x", padx=20, pady=(18, 10))
        tk.Label(toolbar, text="独立历史回测", bg="#ffffff", fg="#17212b", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Button(toolbar, text="刷新回测", command=self.refresh_backtest).pack(side="right")
        self.backtest_status_var = tk.StringVar(value="仅统计真实9:25锁单与真实15:00收盘记录")
        tk.Label(self.backtest_tab, textvariable=self.backtest_status_var, bg="#ffffff", fg="#69747e", anchor="w").pack(fill="x", padx=20)
        columns = ("window", "days", "signals", "no_break", "close_win", "avg_mae", "max_mae", "over2", "streak")
        self.backtest_tree = ttk.Treeview(self.backtest_tab, columns=columns, show="headings", height=6)
        headings = {"window": "区间", "days": "交易日", "signals": "样本数", "no_break": "不破成本率", "close_win": "收盘胜率", "avg_mae": "平均MAE", "max_mae": "最大MAE", "over2": "MAE>2%", "streak": "连续失败"}
        for column in columns:
            self.backtest_tree.heading(column, text=headings[column])
            self.backtest_tree.column(column, width=130 if column == "window" else 105, anchor="center")
        self.backtest_tree.pack(fill="x", padx=20, pady=14)
        self.backtest_explain_var = tk.StringVar()
        tk.Label(self.backtest_tab, textvariable=self.backtest_explain_var, bg="#f8fafb", fg="#35424b", justify="left", anchor="nw", wraplength=1060, padx=14, pady=14).pack(fill="both", expand=True, padx=20, pady=(0, 18))
        self.refresh_backtest()

    def refresh_backtest(self) -> None:
        report = self.backtester.run(self.db.get_settings())
        if hasattr(self, "backtest_tree"):
            for item in self.backtest_tree.get_children(): self.backtest_tree.delete(item)
            for key, label in (("rolling_20", "最近20日"), ("rolling_60", "最近60日"), ("training", "调参样本"), ("holdout_10", "最近10日样本外"), ("overall", "全部正式样本")):
                value = report[key]
                self.backtest_tree.insert("", "end", values=(label, value["trading_days"], value["signals"], f'{value["no_break_rate"]:.1%}', f'{value["close_win_rate"]:.1%}', f'{value["average_mae"]:.2f}%', f'{value["maximum_mae"]:.2f}%', f'{value["mae_over_2_rate"]:.1%}', value["consecutive_failures"]))
        state = "已通过审核" if report["qualified"] else "未通过审核，只能试验观察/等待9:35确认"
        self.backtest_status_var.set(f'{report["profile"]}：{state}')
        self.backtest_explain_var.set(
            "正式资格：至少30个交易日、50个独立信号，且最近10个交易日必须作为样本外验证。\n"
            "目标：不破竞价成本率≥80%，收盘胜率≥70%，平均MAE≤0.50%，MAE>2%的信号≤5%。\n"
            f'已排除非真实/不完整交易日 {report["excluded_days"]} 个，缺少真实收盘的信号 {report["excluded_signals"]} 个。同一交易日的同源表不重复计数。'
        )
        self.auction_performance_var.set(f'{report["profile"]}：{state}｜正式样本 {report["overall"]["signals"]}/50')

    def _build_dashboard(self) -> None:
        attention = tk.Frame(self.dashboard_tab, bg="#f8fafb", height=86, highlightbackground="#e1e7ea", highlightthickness=1)
        attention.pack(fill="x", padx=16, pady=(16, 10))
        attention.pack_propagate(False)
        tk.Label(attention, text="今日需要重点关注", bg="#f8fafb", fg="#25313a", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        self.attention_var = tk.StringVar(value="暂无分析结果")
        tk.Label(attention, textvariable=self.attention_var, bg="#f8fafb", fg="#616d77", anchor="w", justify="left").pack(fill="x", padx=14)
        table_frame = tk.Frame(self.dashboard_tab, bg="#ffffff")
        table_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        columns = ("name", "code", "price", "cost", "quantity", "return", "max_profit", "drawdown", "risk")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {"name": "股票", "code": "代码", "price": "当前价", "cost": "成本", "quantity": "持仓", "return": "收益率", "max_profit": "最高浮盈", "drawdown": "高点回撤", "risk": "风险"}
        widths = {"name": 120, "code": 82, "price": 82, "cost": 82, "quantity": 78, "return": 92, "max_profit": 92, "drawdown": 92, "risk": 150}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center", stretch=col in ("name", "risk"))
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for level, color in RISK_COLORS.items():
            self.tree.tag_configure(f"risk{level}", foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _event: self.notebook.select(self.detail_tab))
        actions = tk.Frame(self.dashboard_tab, bg="#ffffff")
        actions.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(actions, text="删除选中持仓", command=self.delete_selected).pack(side="right")

    def toggle_auction(self) -> None:
        enabled = self.db.get_settings().get("auction_enabled", "1") == "1"
        self.db.set_setting("auction_enabled", "0" if enabled else "1")
        self.auction.wake()
        self.auction_toggle_button.configure(text="启动自动竞价" if enabled else "暂停自动竞价")
        self.auction_status_var.set("自动竞价已暂停" if enabled else "自动竞价已启动，等待下一个采样点")

    def save_auction_settings(self) -> None:
        try:
            amount_wan = float(self.auction_setting_vars["auction_min_amount"].get())
            high_price = float(self.auction_setting_vars["auction_high_price"].get())
            retention = float(self.auction_setting_vars["auction_min_retention"].get())
            late_drop = float(self.auction_setting_vars["auction_late_drop"].get())
            drawdown = float(self.auction_setting_vars["auction_max_drawdown"].get())
            float_ratio = float(self.auction_setting_vars["auction_min_float_amount_ratio"].get())
            market_positive = float(self.auction_setting_vars["auction_min_market_positive_ratio"].get())
            max_orderable = int(self.auction_setting_vars["auction_max_orderable"].get())
            pool_max_size = int(self.auction_setting_vars["auction_pool_max_size"].get())
            pool_per_sector = int(self.auction_setting_vars["auction_pool_max_per_sector"].get())
            pool_amount_wan = float(self.auction_setting_vars["auction_pool_min_amount"].get())
            infoway_interval = float(self.auction_setting_vars["infoway_request_interval"].get())
            if amount_wan <= 0 or high_price <= 0 or not 1 <= retention <= 100 or late_drop <= 0 or drawdown <= 0:
                raise ValueError("竞价门槛必须为正数，留存率应在1%-100%之间")
            if not 0 < float_ratio <= 10 or not 1 <= market_positive <= 100 or not 1 <= max_orderable <= 20:
                raise ValueError("相对竞价额应在0%-10%，市场正竞价在1%-100%，候选上限在1-20只")
            if not 0 <= infoway_interval <= 10:
                raise ValueError("Infoway请求间隔应在0-10秒之间")
            if not 80 <= pool_max_size <= 600 or not 1 <= pool_per_sector <= 10 or pool_amount_wan <= 0:
                raise ValueError("跟踪上限应在80-600只，每板块入池应在1-10只，入池竞价额必须为正数")
            values = {
                "auction_data_source": self.auction_source_var.get(),
                "infoway_request_interval": f"{infoway_interval:g}",
                "auction_min_amount": f"{amount_wan * 10000:g}",
                "auction_high_price": f"{high_price:g}",
                "auction_min_retention": f"{retention / 100:g}",
                "auction_late_drop": f"{late_drop:g}",
                "auction_max_drawdown": f"{drawdown:g}",
                "auction_min_float_amount_ratio": f"{float_ratio / 100:g}",
                "auction_min_market_positive_ratio": f"{market_positive / 100:g}",
                "auction_max_orderable": str(max_orderable),
                "auction_pool_max_size": str(pool_max_size),
                "auction_pool_max_per_sector": str(pool_per_sector),
                "auction_pool_min_amount": f"{pool_amount_wan * 10000:g}",
            }
            for key, value in values.items():
                self.db.set_setting(key, value)
            for key, variable in self.auction_option_vars.items():
                self.db.set_setting(key, "1" if variable.get() else "0")
            self.db.set_setting("secret_infoway_key", protect_secret(self.infoway_key_var.get().strip()))
        except (ValueError, OSError) as exc:
            messagebox.showerror("竞价设置", str(exc)); return
        messagebox.showinfo("竞价设置", "设置已保存；下一次9:25锁单按新门槛计算。")

    def refresh_auction_results(self) -> None:
        self.auction.refresh_saved()
        self._render_auction_rows()

    def _render_auction_rows(self) -> None:
        if not hasattr(self, "auction_tree"):
            return
        for item in self.auction_tree.get_children():
            self.auction_tree.delete(item)
        rows = self.auction.latest_decisions
        selected_filter = self.auction_filter_var.get()
        visible = [row for row in rows if selected_filter == "全部" or row.get("action") == selected_filter]
        for row in visible:
            action = str(row.get("action") or "放弃")
            score = float(row.get("score") or 0)
            grade = "A" if action == "竞价小仓" else "试验" if action == "试验观察" else "B" if action != "放弃" else "C"
            values = (
                row.get("rank"), row.get("data_quality", "正常"), row.get("code"), row.get("name"), row.get("industry"),
                f"{float(row.get('lock_pct') or 0):+.2f}%", f"{float(row.get('peak_pct') or 0):+.2f}%",
                f"{float(row.get('drawdown') or 0):.2f}%", f"{float(row.get('retention') or 0):.0%}",
                f"{float(row.get('auction_amount') or 0) / 10000:.0f}万",
                f"{row.get('sector_resonance', '--')} / {int(row.get('sector_positive_count') or 0)}只",
                row.get("false_risk", "--"), row.get("pullback_risk", "--"), action,
                f"{grade} {score:.0f}", f"{int(row.get('fill_probability') or 0)}%" if row.get("backtest_qualified") else "--",
                row.get("suggested_order", "--"), row.get("no_buy_condition", "--"),
            )
            self.auction_tree.insert("", "end", iid=str(row.get("code")), values=values, tags=(action,))
        counts = {action: sum(1 for row in rows if row.get("action") == action) for action in ("竞价小仓", "试验观察", "等待9:35确认", "放弃")}
        if rows:
            self.auction_summary_var.set(f"今日锁单：竞价小仓 {counts['竞价小仓']}｜试验观察 {counts['试验观察']}｜等待9:35 {counts['等待9:35确认']}｜放弃 {counts['放弃']}")
        else:
            self.auction_summary_var.set("尚未生成今日锁单结果；必须完成9:20-9:25路径并在9:25锁定")
        report = self.backtester.run(self.db.get_settings())
        state = "已通过样本外审核" if report["qualified"] else "未通过样本外审核"
        self.auction_performance_var.set(f'{report["profile"]}：{state}｜正式样本 {report["overall"]["signals"]}/50')

    def _on_auction_select(self, _event: object = None) -> None:
        selection = self.auction_tree.selection()
        if not selection:
            return
        row = next((item for item in self.auction.latest_decisions if item.get("code") == selection[0]), None)
        if not row:
            return
        path = row.get("path") or {}
        path_text = "  ".join(f"{label} {path.get(label):+.2f}%" if path.get(label) is not None else f"{label} --" for label in ("09:20", "09:21", "09:22", "09:23", "09:24", "09:24:30", "09:25"))
        reasons = "；".join(row.get("elimination_reasons") or row.get("warnings") or ["锁单路径稳定，未触发硬淘汰"])
        zones_text = zone_summary(row) if row.get("trade_zones") else "三区间：尚未计算"
        self.auction_detail_var.set(
            f"{row.get('name')} {row.get('code')}｜{path_text}\n"
            f"板块：{row.get('industry')}，板块平均 {float(row.get('sector_pct') or 0):+.2f}%，正竞价 {row.get('sector_positive_count')} 只；前排：{row.get('sector_leaders') or '--'}\n"
            f"结论：{row.get('action')}｜{zones_text}\n"
            f"数据：{row.get('data_quality', '正常')}｜原因：{reasons}｜不买条件：{row.get('no_buy_condition')}"
        )

    def _selected_actionable_candidates(self) -> list[dict[str, Any]]:
        selected = set(self.auction_tree.selection())
        rows = self.auction.latest_decisions
        if selected:
            rows = [row for row in rows if str(row.get("code")) in selected]
        else:
            rows = [row for row in rows if row.get("action") in ("竞价小仓", "试验观察", "等待9:35确认")]
        return [row for row in rows if row.get("action") != "放弃"]

    def calculate_selected_candidate_zones(self) -> None:
        rows = self._selected_actionable_candidates()
        if not rows:
            messagebox.showinfo("计算三个区间", "请先选中一只非放弃候选，或等待9:25生成候选。")
            return
        self._start_candidate_zone_job(rows, sync_after=False)

    def sync_candidates_to_ths(self) -> None:
        rows = self._selected_actionable_candidates()
        if not rows:
            messagebox.showinfo("同步同花顺", "没有可同步的非放弃候选。")
            return
        names = "、".join(f"{row.get('name')} {row.get('code')}" for row in rows)
        if not messagebox.askyesno(
            "同步同花顺自选",
            f"将计算三个区间并把以下 {len(rows)} 只加入同花顺自选：\n\n{names}\n\n"
            "请先让同花顺停留在普通行情/K线页，不要停留在委托页。\n"
            "本操作只会输入股票代码、回车和 Insert。",
        ):
            return
        self._start_candidate_zone_job(rows, sync_after=True)

    def _start_candidate_zone_job(self, rows: list[dict[str, Any]], sync_after: bool) -> None:
        self.auction_status_var.set(f"正在为 {len(rows)} 只候选计算支撑区、博弈区和压力区…")

        def worker() -> None:
            calculated, errors = calculate_candidate_zones(rows)
            self.events.put(("candidate_zones", {"rows": calculated, "errors": errors, "sync_after": sync_after}))

        threading.Thread(target=worker, name="candidate-zone-ui", daemon=True).start()

    def _save_candidate_zones(self, rows: list[dict[str, Any]]) -> None:
        day = date.today().isoformat()
        changed = {str(row.get("code")): row for row in rows}
        self.auction.latest_decisions = [changed.get(str(row.get("code")), row) for row in self.auction.latest_decisions]
        for row in rows:
            self.db.update_auction_decision_payload(day, row)
        self._render_auction_rows()
        if self.auction_tree.selection():
            self._on_auction_select()

    def _run_ths_sync(self, rows: list[dict[str, Any]]) -> None:
        def worker() -> None:
            try:
                completed = self.ths_watchlist.add_codes([str(row.get("code")) for row in rows])
                self.events.put(("ths_sync_done", {"codes": completed, "rows": rows}))
            except Exception as exc:
                self.events.put(("ths_sync_error", str(exc)))

        self.root.withdraw()
        threading.Thread(target=worker, name="ths-watchlist-ui", daemon=True).start()

    def _build_detail(self) -> None:
        self.detail_title = tk.StringVar(value="请在持仓总览中选择一只股票")
        tk.Label(self.detail_tab, textvariable=self.detail_title, bg="#ffffff", fg="#17212b", font=("Microsoft YaHei UI", 16, "bold"), anchor="w").pack(fill="x", padx=22, pady=(20, 10))
        nav = tk.Frame(self.detail_tab, bg="#ffffff")
        nav.pack(fill="x", padx=22, pady=(0, 10))
        ttk.Button(nav, text="上一只", command=lambda: self._step_detail(-1)).pack(side="left")
        self.detail_stock_var = tk.StringVar()
        self.detail_selector = ttk.Combobox(nav, textvariable=self.detail_stock_var, state="readonly", width=32)
        self.detail_selector.pack(side="left", padx=8)
        self.detail_selector.bind("<<ComboboxSelected>>", self._select_detail_from_combo)
        ttk.Button(nav, text="下一只", command=lambda: self._step_detail(1)).pack(side="left")
        ttk.Button(nav, text="编辑当前持仓", style="Accent.TButton", command=self.show_edit_current_dialog).pack(side="left", padx=(12, 0))
        summary = tk.Frame(self.detail_tab, bg="#eef3f2", height=90)
        summary.pack(fill="x", padx=22)
        summary.pack_propagate(False)
        self.detail_metrics: dict[str, tk.StringVar] = {}
        for label in ("当前价", "成本价", "收益率", "最高浮盈", "高点回撤", "风险等级"):
            box = tk.Frame(summary, bg="#eef3f2")
            box.pack(side="left", fill="both", expand=True, padx=8, pady=12)
            tk.Label(box, text=label, bg="#eef3f2", fg="#66727b", font=("Microsoft YaHei UI", 9)).pack()
            variable = tk.StringVar(value="--")
            self.detail_metrics[label] = variable
            tk.Label(box, textvariable=variable, bg="#eef3f2", fg="#1d302c", font=("Microsoft YaHei UI", 13, "bold")).pack(pady=4)
        content = tk.Frame(self.detail_tab, bg="#ffffff")
        content.pack(fill="both", expand=True, padx=22, pady=16)
        left = tk.Frame(content, bg="#ffffff")
        left.pack(side="left", fill="y", padx=(0, 24))
        self.state_vars: dict[str, tk.StringVar] = {}
        for label in ("趋势", "BOLL", "MACD", "KDJ", "成交量", "短线支撑", "重要支撑", "短线压力", "重要压力", "支撑区", "博弈区", "压力区", "强压力", "区间算法", "仓位建议"):
            row = tk.Frame(left, bg="#ffffff")
            row.pack(fill="x", pady=5)
            tk.Label(row, text=label, width=10, anchor="w", bg="#ffffff", fg="#69747e").pack(side="left")
            variable = tk.StringVar(value="--")
            self.state_vars[label] = variable
            tk.Label(row, textvariable=variable, width=18, anchor="w", bg="#ffffff", fg="#26343c", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        analysis_frame = tk.Frame(content, bg="#f8fafb", highlightbackground="#e1e7ea", highlightthickness=1)
        analysis_frame.pack(side="left", fill="both", expand=True)
        tk.Label(analysis_frame, text="分析说明", bg="#f8fafb", fg="#25313a", font=("Microsoft YaHei UI", 11, "bold"), anchor="w").pack(fill="x", padx=14, pady=(12, 6))
        self.explanation = tk.Text(analysis_frame, wrap="word", bg="#f8fafb", fg="#35424b", relief="flat", padx=14, pady=8, font=("Microsoft YaHei UI", 10), spacing2=4)
        self.explanation.pack(fill="both", expand=True)
        self.explanation.insert("1.0", "行情分析将在刷新后显示。")
        self.explanation.configure(state="disabled")

    def _build_alerts(self) -> None:
        columns = ("time", "stock", "change", "price", "reason")
        self.alert_tree = ttk.Treeview(self.alert_tab, columns=columns, show="headings")
        for col, text, width in (("time", "时间", 150), ("stock", "股票", 140), ("change", "风险变化", 100), ("price", "当时价格", 90), ("reason", "原因", 520)):
            self.alert_tree.heading(col, text=text)
            self.alert_tree.column(col, width=width, anchor="w" if col == "reason" else "center")
        self.alert_tree.pack(fill="both", expand=True, padx=16, pady=16)

    def _build_notification_settings(self) -> None:
        settings = self.db.get_settings()
        shell = tk.Frame(self.notification_tab, bg="#ffffff")
        shell.pack(fill="both", expand=True, padx=22, pady=18)
        tk.Label(shell, text="通知渠道", bg="#ffffff", fg="#17212b", font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(shell, text="重要事件才发送；密钥使用当前 Windows 账户加密保存", bg="#ffffff", fg="#69747e", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))
        self.notify_channel_vars: dict[str, tk.BooleanVar] = {}
        channel_row = tk.Frame(shell, bg="#ffffff")
        channel_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        channels = [
            ("notify_windows", "Windows通知"), ("notify_mobile", "手机推送"),
            ("notify_email", "邮箱"), ("notify_telegram", "Telegram"),
            ("notify_wecom", "企业微信"),
        ]
        for key, label in channels:
            variable = tk.BooleanVar(value=settings.get(key, "0") == "1")
            self.notify_channel_vars[key] = variable
            ttk.Checkbutton(channel_row, text=label, variable=variable).pack(side="left", padx=(0, 18))

        left = tk.Frame(shell, bg="#ffffff")
        right = tk.Frame(shell, bg="#ffffff")
        left.grid(row=3, column=0, sticky="nsew", padx=(0, 30))
        right.grid(row=3, column=1, sticky="nsew")
        shell.columnconfigure(0, weight=1); shell.columnconfigure(1, weight=1); shell.rowconfigure(3, weight=1)
        self.notify_text_vars: dict[str, tk.StringVar] = {}
        self.notify_secret_vars: dict[str, tk.StringVar] = {}
        tk.Label(left, text="渠道参数", bg="#ffffff", fg="#26343c", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        def add_field(parent: tk.Frame, row: int, key: str, label: str, secret: bool = False, width: int = 34) -> None:
            tk.Label(parent, text=label, bg="#ffffff", fg="#56636d", width=17, anchor="w").grid(row=row, column=0, sticky="w", pady=4)
            value = settings.get(key, "")
            if secret:
                try: value = unprotect_secret(value)
                except OSError: value = ""
            variable = tk.StringVar(value=value)
            (self.notify_secret_vars if secret else self.notify_text_vars)[key] = variable
            ttk.Entry(parent, textvariable=variable, width=width, show="*" if secret else "").grid(row=row, column=1, sticky="ew", pady=4)

        tk.Label(left, text="手机服务", bg="#ffffff", fg="#56636d", width=17, anchor="w").grid(row=1, column=0, sticky="w", pady=4)
        self.mobile_service_var = tk.StringVar(value=settings.get("notify_mobile_service", "bark"))
        ttk.Combobox(left, textvariable=self.mobile_service_var, values=("bark", "pushplus", "serverchan"), state="readonly", width=31).grid(row=1, column=1, sticky="ew", pady=4)
        add_field(left, 2, "secret_bark_key", "Bark Key", True)
        add_field(left, 3, "bark_server", "Bark服务器")
        add_field(left, 4, "secret_pushplus_token", "PushPlus Token", True)
        add_field(left, 5, "secret_serverchan_key", "Server酱 SendKey", True)
        add_field(left, 6, "secret_telegram_token", "Telegram Bot Token", True)
        add_field(left, 7, "telegram_chat_id", "Telegram Chat ID")
        add_field(left, 8, "secret_wecom_webhook", "企业微信 Webhook", True)
        add_field(left, 9, "smtp_host", "SMTP服务器")
        add_field(left, 10, "smtp_port", "SMTP端口")
        add_field(left, 11, "smtp_user", "SMTP用户名")
        add_field(left, 12, "secret_smtp_password", "SMTP密码/授权码", True)
        add_field(left, 13, "email_to", "收件邮箱")
        self.smtp_ssl_var = tk.BooleanVar(value=settings.get("smtp_ssl", "1") == "1")
        ttk.Checkbutton(left, text="SMTP使用SSL", variable=self.smtp_ssl_var).grid(row=14, column=1, sticky="w", pady=4)
        left.columnconfigure(1, weight=1)

        tk.Label(right, text="推送事件", bg="#ffffff", fg="#26343c", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.notify_event_vars: dict[str, tk.BooleanVar] = {}
        event_fields = [
            ("risk_change", "重要风险等级变化"), ("support_break", "跌破支撑"),
            ("resistance_break", "突破压力"), ("macd_death", "MACD死叉"),
            ("kdj_death", "KDJ高位死叉"), ("profit_drawdown", "利润回撤"),
            ("below_cost", "跌破成本"), ("stop_loss", "达到止损线"),
            ("target_profit", "达到止盈提醒线"), ("stall", "放量滞涨"),
            ("false_break", "假突破"),
        ]
        events_frame = tk.Frame(right, bg="#ffffff")
        events_frame.grid(row=1, column=0, sticky="ew")
        for index, (key, label) in enumerate(event_fields):
            variable = tk.BooleanVar(value=settings.get(f"notify_event_{key}", "1") == "1")
            self.notify_event_vars[key] = variable
            ttk.Checkbutton(events_frame, text=label, variable=variable).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 18), pady=3)
        cooldown_row = tk.Frame(right, bg="#ffffff")
        cooldown_row.grid(row=2, column=0, sticky="w", pady=(8, 16))
        tk.Label(cooldown_row, text="同类事件冷却", bg="#ffffff", fg="#56636d").pack(side="left")
        self.cooldown_var = tk.StringVar(value=settings.get("notify_cooldown_minutes", "30"))
        ttk.Entry(cooldown_row, textvariable=self.cooldown_var, width=7).pack(side="left", padx=8)
        tk.Label(cooldown_row, text="分钟", bg="#ffffff", fg="#56636d").pack(side="left")

        ttk.Separator(right).grid(row=3, column=0, sticky="ew", pady=(0, 14))
        tk.Label(right, text="手机只读网页", bg="#ffffff", fg="#26343c", font=("Microsoft YaHei UI", 10, "bold")).grid(row=4, column=0, sticky="w")
        self.web_enabled_var = tk.BooleanVar(value=settings.get("web_enabled", "0") == "1")
        ttk.Checkbutton(right, text="启用带登录保护的局域网网页", variable=self.web_enabled_var).grid(row=5, column=0, sticky="w", pady=5)
        web_form = tk.Frame(right, bg="#ffffff"); web_form.grid(row=6, column=0, sticky="ew")
        add_field(web_form, 0, "web_host", "监听地址", width=24)
        add_field(web_form, 1, "web_port", "端口", width=24)
        tk.Label(web_form, text="新登录密码", bg="#ffffff", fg="#56636d", width=17, anchor="w").grid(row=2, column=0, sticky="w", pady=4)
        self.web_password_var = tk.StringVar()
        ttk.Entry(web_form, textvariable=self.web_password_var, width=27, show="*").grid(row=2, column=1, sticky="ew", pady=4)
        add_field(web_form, 3, "secret_web_token", "API访问Token", True, 24)
        ttk.Button(web_form, text="生成新Token", command=self.generate_web_token).grid(row=4, column=1, sticky="w", pady=4)
        self.web_address_var = tk.StringVar(value="网页当前未启用")
        tk.Label(right, textvariable=self.web_address_var, bg="#ffffff", fg="#0c8279", anchor="w", justify="left").grid(row=7, column=0, sticky="w", pady=(8, 0))

        actions = tk.Frame(shell, bg="#ffffff")
        actions.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="保存通知设置", style="Accent.TButton", command=self.save_notification_settings).pack(side="left")
        ttk.Button(actions, text="发送测试通知", command=self.send_test_notification).pack(side="left", padx=8)

    def _build_settings(self) -> None:
        form = tk.Frame(self.settings_tab, bg="#ffffff")
        form.pack(anchor="nw", padx=28, pady=24)
        settings = self.db.get_settings()
        fields = [
            ("poll_seconds", "交易时段刷新（秒）"), ("profit_guard_start", "利润保护起点（%）"),
            ("profit_tier_1", "浮盈档位一（%）"), ("drawdown_tier_1", "对应回撤（%）"),
            ("profit_tier_2", "浮盈档位二（%）"), ("drawdown_tier_2", "对应回撤（%）"),
            ("profit_tier_3", "浮盈档位三（%）"), ("drawdown_tier_3", "对应回撤（%）"),
        ]
        self.setting_vars: dict[str, tk.StringVar] = {}
        for row, (key, label) in enumerate(fields):
            tk.Label(form, text=label, bg="#ffffff", fg="#46525c", width=20, anchor="w").grid(row=row, column=0, sticky="w", pady=6)
            variable = tk.StringVar(value=settings.get(key, ""))
            self.setting_vars[key] = variable
            ttk.Entry(form, textvariable=variable, width=14).grid(row=row, column=1, sticky="w", pady=6)
        self.startup_var = tk.BooleanVar(value=settings.get("startup_enabled") == "1")
        ttk.Checkbutton(form, text="登录 Windows 后自动启动", variable=self.startup_var).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(14, 6))
        ttk.Button(form, text="保存设置", style="Accent.TButton", command=self.save_settings).grid(row=len(fields) + 1, column=0, sticky="w", pady=12)

    def show_add_dialog(self) -> None:
        self._show_position_dialog(None)

    def show_edit_current_dialog(self) -> None:
        if not self.selected_code:
            messagebox.showinfo("编辑持仓", "请先在个股详情里选择一只股票。")
            return
        position = next((item for item in self.db.list_positions() if item.code == self.selected_code), None)
        if not position:
            messagebox.showinfo("编辑持仓", "当前股票不在持仓列表中。")
            return
        self._show_position_dialog(position)

    def _show_position_dialog(self, existing: Position | None) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("编辑持仓" if existing else "添加持仓"); dialog.geometry("430x470"); dialog.resizable(False, False); dialog.transient(self.root); dialog.grab_set()
        content = tk.Frame(dialog, bg="#ffffff"); content.pack(fill="both", expand=True, padx=24, pady=18)
        fields = [
            ("code", "股票代码", existing.code if existing else ""),
            ("name", "股票名称", existing.name if existing else ""),
            ("buy_price", "成本价", f"{existing.buy_price:g}" if existing else ""),
            ("buy_date", "买入日期（可不填）", existing.buy_date if existing else date.today().isoformat()),
            ("quantity", "持仓数量", str(existing.quantity) if existing else "0"),
            ("stop_loss", "计划止损价（可选）", f"{existing.stop_loss:g}" if existing and existing.stop_loss is not None else ""),
            ("target_return", "目标收益率%（可选）", f"{existing.target_return:g}" if existing and existing.target_return is not None else ""),
        ]
        variables: dict[str, tk.StringVar] = {}
        for row, (key, label, default) in enumerate(fields):
            tk.Label(content, text=label, bg="#ffffff", fg="#47535c", anchor="w").grid(row=row, column=0, sticky="w", pady=8)
            variable = tk.StringVar(value=default); variables[key] = variable
            ttk.Entry(content, textvariable=variable, width=24, state="disabled" if existing and key == "code" else "normal").grid(row=row, column=1, pady=8, padx=(12, 0))

        def submit() -> None:
            try:
                code = variables["code"].get().strip()
                if len(code) != 6 or not code.isdigit():
                    raise ValueError("股票代码必须是6位数字")
                position = Position(
                    id=existing.id if existing else None, code=code, name=variables["name"].get().strip() or code,
                    buy_price=float(variables["buy_price"].get()), buy_date=variables["buy_date"].get().strip() or date.today().isoformat(),
                    quantity=int(variables["quantity"].get() or 0),
                    stop_loss=float(variables["stop_loss"].get()) if variables["stop_loss"].get().strip() else None,
                    target_return=float(variables["target_return"].get()) if variables["target_return"].get().strip() else None,
                )
                if position.buy_price <= 0:
                    raise ValueError("成本价必须大于0")
                if position.quantity < 0:
                    raise ValueError("持仓数量不能小于0")
                if existing:
                    self.db.update_position(position)
                else:
                    self.db.add_position(position)
            except (ValueError, TypeError) as exc:
                messagebox.showerror("无法保存", str(exc), parent=dialog); return
            dialog.destroy()
            self.selected_code = position.code
            self.refresh_positions()
            self._select_detail_code(position.code)
            self.monitor.refresh_now()

        ttk.Button(content, text="保存持仓", style="Accent.TButton", command=submit).grid(row=len(fields), column=0, columnspan=2, pady=18)

    def refresh_positions(self) -> None:
        analyses = self.monitor.latest
        for item in self.tree.get_children(): self.tree.delete(item)
        positions = self.db.list_positions()
        rows = []
        for position in positions:
            analysis = analyses.get(position.code)
            price = analysis.price if analysis else position.last_price
            current_return = analysis.return_pct if analysis else ((price / position.buy_price - 1) * 100 if price else 0)
            max_profit = analysis.highest_profit_pct if analysis else ((position.highest_price / position.buy_price - 1) * 100 if position.highest_price else 0)
            drawdown = analysis.drawdown_pct if analysis else ((position.highest_price - price) / position.highest_price * 100 if price and position.highest_price else 0)
            risk = analysis.risk_level if analysis else position.risk_level
            rows.append((risk, position, price, current_return, max_profit, drawdown))
        rows.sort(key=lambda item: (-item[0], item[1].code))
        for risk, position, price, current_return, max_profit, drawdown in rows:
            values = (position.name, position.code, f"{price:.2f}" if price else "--", f"{position.buy_price:.2f}", position.quantity, f"{current_return:+.2f}%", f"{max_profit:+.2f}%", f"{drawdown:.2f}%", RISK_TEXT[risk])
            self.tree.insert("", "end", iid=position.code, values=values, tags=(f"risk{risk}",))
        focus = [item for item in rows if item[0] >= 3][:3]
        if focus:
            parts = []
            for risk, position, *_ in focus:
                reason = analyses[position.code].reasons[0] if position.code in analyses else "等待最新分析"
                parts.append(f"{position.name} {risk}级：{reason}")
            self.attention_var.set("    ".join(parts))
        else:
            self.attention_var.set("暂无3级以上风险；请结合盘面和个人计划继续观察")
        if hasattr(self, "detail_selector"):
            choices = [f"{position.name} {position.code}" for _risk, position, *_rest in rows]
            self.detail_selector.configure(values=choices)
            if self.selected_code:
                for choice in choices:
                    if choice.endswith(self.selected_code):
                        self.detail_stock_var.set(choice)
                        break
        self._refresh_alerts()

    def _on_select(self, _event: object = None) -> None:
        selection = self.tree.selection()
        if not selection: return
        self._select_detail_code(selection[0], sync_tree=False)

    def _select_detail_from_combo(self, _event: object = None) -> None:
        value = self.detail_stock_var.get().strip()
        if value:
            self._select_detail_code(value.rsplit(" ", 1)[-1])

    def _step_detail(self, step: int) -> None:
        codes = [position.code for position in self.db.list_positions()]
        if not codes:
            return
        code = self.selected_code if self.selected_code in codes else codes[0]
        self._select_detail_code(codes[(codes.index(code) + step) % len(codes)])

    def _select_detail_code(self, code: str, sync_tree: bool = True) -> None:
        self.selected_code = code
        if sync_tree and self.tree.exists(code):
            self.tree.selection_set(code)
            self.tree.focus(code)
            self.tree.see(code)
        for position in self.db.list_positions():
            if position.code == code:
                self.detail_stock_var.set(f"{position.name} {position.code}")
                break
        self._render_detail(code)

    def _render_detail(self, code: str) -> None:
        positions = {item.code: item for item in self.db.list_positions()}
        position = positions.get(code); analysis = self.monitor.latest.get(code)
        if not position: return
        self.detail_title.set(f"{position.name}  {position.code}")
        if not analysis:
            snapshot = self._latest_analysis_snapshot(position)
            if snapshot:
                values = {"当前价": f"{float(snapshot.get('price') or 0):.2f}", "成本价": f"{position.buy_price:.2f}", "收益率": f"{float(snapshot.get('return_pct') or 0):+.2f}%", "最高浮盈": f"{float(snapshot.get('highest_profit_pct') or 0):+.2f}%", "高点回撤": f"{float(snapshot.get('drawdown_pct') or 0):.2f}%", "风险等级": f"{int(snapshot.get('risk_level') or position.risk_level)}/5"}
                for label, value in values.items(): self.detail_metrics[label].set(value)
                states = {"趋势": str(snapshot.get("trend") or "等待分析"), "BOLL": str(snapshot.get("boll_state") or "等待分析"), "MACD": str(snapshot.get("macd_state") or "等待分析"), "KDJ": str(snapshot.get("kdj_state") or "等待分析"), "成交量": str(snapshot.get("volume_state") or "等待分析"), "短线支撑": self._fmt_price(snapshot.get("support")), "重要支撑": self._fmt_price(snapshot.get("major_support")), "短线压力": self._fmt_price(snapshot.get("resistance")), "重要压力": self._fmt_price(snapshot.get("major_resistance")), "支撑区": str(snapshot.get("support_zone") or "--"), "博弈区": str(snapshot.get("play_zone") or "--"), "压力区": str(snapshot.get("pressure_zone") or "--"), "强压力": str(snapshot.get("strong_pressure_zone") or "--"), "区间算法": str(snapshot.get("zone_method") or "--"), "仓位建议": str(snapshot.get("position_advice") or "--")}
                for label, value in states.items():
                    if label in self.state_vars: self.state_vars[label].set(value)
                self.explanation.configure(state="normal"); self.explanation.delete("1.0", "end"); self.explanation.insert("1.0", str(snapshot.get("explanation") or "已读取最近一次分析快照，请点击刷新行情获取最新结果。")); self.explanation.configure(state="disabled")
                return
            self.detail_metrics["成本价"].set(f"{position.buy_price:.2f}")
            return
        values = {"当前价": f"{analysis.price:.2f}", "成本价": f"{position.buy_price:.2f}", "收益率": f"{analysis.return_pct:+.2f}%", "最高浮盈": f"{analysis.highest_profit_pct:+.2f}%", "高点回撤": f"{analysis.drawdown_pct:.2f}%", "风险等级": f"{analysis.risk_level}/5"}
        for label, value in values.items(): self.detail_metrics[label].set(value)
        states = {"趋势": analysis.trend, "BOLL": analysis.boll_state, "MACD": analysis.macd_state, "KDJ": analysis.kdj_state, "成交量": analysis.volume_state, "短线支撑": self._fmt_price(analysis.support), "重要支撑": self._fmt_price(analysis.major_support), "短线压力": self._fmt_price(analysis.resistance), "重要压力": self._fmt_price(analysis.major_resistance), "支撑区": str(analysis.indicators.get("support_zone", "--")), "博弈区": str(analysis.indicators.get("play_zone", "--")), "压力区": str(analysis.indicators.get("pressure_zone", "--")), "强压力": str(analysis.indicators.get("strong_pressure_zone", "--")), "区间算法": str(analysis.indicators.get("zone_method", "--")), "仓位建议": str(analysis.indicators.get("position_advice", "--"))}
        for label, value in states.items(): self.state_vars[label].set(value)
        self.explanation.configure(state="normal"); self.explanation.delete("1.0", "end"); self.explanation.insert("1.0", analysis.explanation); self.explanation.configure(state="disabled")

    @staticmethod
    def _fmt_price(value: float | None | object) -> str:
        try:
            return f"{float(value):.2f}" if value is not None else "--"
        except (TypeError, ValueError):
            return "--"

    def _latest_analysis_snapshot(self, position: Position) -> dict[str, Any] | None:
        row = self.db.latest_indicator(position.code)
        if not row:
            return None
        try:
            state = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            return None
        snapshot = dict(state.get("_analysis") or {})
        for key in ("support_zone", "play_zone", "pressure_zone", "strong_pressure_zone", "zone_method", "zone_basis", "position_advice", "position_advice_detail"):
            if key in state and key not in snapshot:
                snapshot[key] = state[key]
        for key in ("support", "major_support", "resistance", "major_resistance"):
            if key in state and key not in snapshot:
                snapshot[key] = state[key]
        return snapshot or None

    def delete_selected(self) -> None:
        selection = self.tree.selection()
        if not selection: return
        position = next((p for p in self.db.list_positions() if p.code == selection[0]), None)
        if position and messagebox.askyesno("删除持仓", f"确定停止监控 {position.name} {position.code}？"):
            self.db.delete_position(int(position.id)); self.monitor.latest.pop(position.code, None); self.refresh_positions()

    def toggle_monitor(self) -> None:
        if self.monitor.running:
            self.monitor.stop(); self.monitor_button.configure(text="开始监控"); self.db.set_setting("monitor_enabled", "0")
        else:
            self.monitor.start(); self.monitor_button.configure(text="停止监控"); self.db.set_setting("monitor_enabled", "1")

    def read_ths(self) -> None:
        windows = find_ths_windows()
        if not windows:
            messagebox.showinfo("读取当前同花顺", "未发现可见的同花顺窗口。请先打开同花顺并显示目标股票。")
            return
        window = windows[0]
        try:
            screenshot = capture_window(window, self.base_dir / "screenshots")
            messagebox.showinfo("已读取同花顺", f"窗口：{window.title}\n截图：{screenshot}\n\n第一版通过窗口标题辅助确认，行情监控仍使用独立数据源。")
        except Exception as exc:
            messagebox.showwarning("读取同花顺", f"找到窗口：{window.title}\n但截图失败：{exc}")

    def save_settings(self) -> None:
        try:
            for key, variable in self.setting_vars.items():
                value = float(variable.get())
                if value <= 0: raise ValueError("所有参数必须大于0")
                self.db.set_setting(key, f"{value:g}")
            enabled = self.startup_var.get()
            set_startup(enabled, self.base_dir / "启动股票持仓监控助手.cmd")
            self.db.set_setting("startup_enabled", "1" if enabled else "0")
        except (ValueError, OSError) as exc:
            messagebox.showerror("保存失败", str(exc)); return
        messagebox.showinfo("设置", "设置已保存")

    def generate_web_token(self) -> None:
        token = new_access_token()
        self.notify_secret_vars["secret_web_token"].set(token)
        self.root.clipboard_clear(); self.root.clipboard_append(token)
        messagebox.showinfo("API Token", "已生成并复制到剪贴板。请像保管密码一样保管它。")

    def save_notification_settings(self) -> bool:
        try:
            cooldown = int(self.cooldown_var.get())
            if cooldown < 1:
                raise ValueError("通知冷却时间至少为1分钟")
            port = int(self.notify_text_vars["web_port"].get())
            if not 1024 <= port <= 65535:
                raise ValueError("网页端口应在1024到65535之间")
            for key, variable in self.notify_channel_vars.items():
                self.db.set_setting(key, "1" if variable.get() else "0")
            for key, variable in self.notify_event_vars.items():
                self.db.set_setting(f"notify_event_{key}", "1" if variable.get() else "0")
            self.db.set_setting("notify_mobile_service", self.mobile_service_var.get())
            self.db.set_setting("notify_cooldown_minutes", str(cooldown))
            self.db.set_setting("smtp_ssl", "1" if self.smtp_ssl_var.get() else "0")
            for key, variable in self.notify_text_vars.items():
                self.db.set_setting(key, variable.get().strip())
            for key, variable in self.notify_secret_vars.items():
                self.db.set_setting(key, protect_secret(variable.get().strip()))
            password = self.web_password_var.get()
            current_hash = self.db.get_settings().get("web_password_hash", "")
            if password:
                self.db.set_setting("web_password_hash", hash_password(password))
                self.web_password_var.set("")
                current_hash = self.db.get_settings().get("web_password_hash", "")
            if self.web_enabled_var.get() and not current_hash:
                raise ValueError("启用手机网页前，请设置至少8位登录密码")
            if self.web_enabled_var.get() and not self.notify_secret_vars["secret_web_token"].get().strip():
                token = new_access_token(); self.notify_secret_vars["secret_web_token"].set(token)
                self.db.set_setting("secret_web_token", protect_secret(token))
            self.db.set_setting("web_enabled", "1" if self.web_enabled_var.get() else "0")
            self._sync_web_server(show_errors=True)
        except (ValueError, OSError) as exc:
            messagebox.showerror("通知设置保存失败", str(exc)); return False
        messagebox.showinfo("通知设置", "通知设置已保存")
        return True

    def send_test_notification(self) -> None:
        if not self.save_notification_settings():
            return
        positions = self.db.list_positions()
        if positions:
            position = positions[0]
        else:
            position = Position(
                id=None, code="000000", name="测试通知", buy_price=10.0,
                quantity=0, highest_price=10.4, last_price=10.25, risk_level=4,
            )
        analysis = self.monitor.latest.get(position.code)
        if not analysis:
            price = position.last_price or position.buy_price
            return_pct = (price / position.buy_price - 1) * 100
            analysis = Analysis(
                code=position.code, name=position.name, price=price, return_pct=return_pct,
                highest_profit_pct=return_pct, drawdown_pct=0, risk_level=position.risk_level,
                reasons=["这是一条测试通知"], events=[], trend="等待分析", boll_state="等待分析",
                macd_state="等待分析", kdj_state="等待分析", volume_state="等待分析",
                support=None, major_support=None, resistance=None, major_resistance=None,
                explanation="测试通知", indicators={},
            )
        event = AlertEvent("test", "通知渠道配置测试", True)
        self.monitor.notifications.submit(NotificationJob(position, analysis, position.risk_level, [event], test=True))

    def _sync_web_server(self, show_errors: bool) -> None:
        settings = self.db.get_settings()
        enabled = settings.get("web_enabled", "0") == "1"
        if not enabled:
            self.web.stop(); self.web_address_var.set("网页当前未启用")
            return
        host = settings.get("web_host", "0.0.0.0")
        port = int(settings.get("web_port", "8765"))
        try:
            if self.web.running and self.web.address != (host, port):
                self.web.stop()
            if not self.web.running:
                self.web.start(host, port)
            display_host = "127.0.0.1" if host in ("127.0.0.1", "localhost") else self._local_ip()
            self.web_address_var.set(f"手机浏览器访问：http://{display_host}:{port}\n仅建议在可信局域网内使用")
        except OSError as exc:
            self.web_address_var.set("网页启动失败")
            if show_errors:
                raise OSError(f"手机网页无法监听 {host}:{port}：{exc}") from exc

    @staticmethod
    def _local_ip() -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 80))
            return str(sock.getsockname()[0])
        except OSError:
            return "本机IP"
        finally:
            sock.close()

    def _web_payload(self) -> dict[str, Any]:
        positions = self.db.list_positions()
        alert_rows = self.db.recent_alerts(30)
        last_reason = {row["code"]: row["reason"].split("；") for row in alert_rows}
        items = []
        for position in positions:
            analysis = self.monitor.latest.get(position.code)
            if analysis:
                item = {
                    "code": position.code, "name": position.name, "price": analysis.price,
                    "buy_price": position.buy_price, "return_pct": analysis.return_pct,
                    "drawdown_pct": analysis.drawdown_pct, "risk_level": analysis.risk_level,
                    "reasons": analysis.reasons, "boll_state": analysis.boll_state,
                    "macd_state": analysis.macd_state, "kdj_state": analysis.kdj_state,
                    "volume_state": analysis.volume_state, "support": analysis.support,
                    "major_support": analysis.major_support, "resistance": analysis.resistance,
                    "major_resistance": analysis.major_resistance,
                    "support_zone": analysis.indicators.get("support_zone"),
                    "play_zone": analysis.indicators.get("play_zone"),
                    "pressure_zone": analysis.indicators.get("pressure_zone"),
                    "strong_pressure_zone": analysis.indicators.get("strong_pressure_zone"),
                    "zone_method": analysis.indicators.get("zone_method"),
                    "position_advice": analysis.indicators.get("position_advice"),
                    "position_advice_detail": analysis.indicators.get("position_advice_detail"),
                }
            else:
                snapshot = self._latest_analysis_snapshot(position)
                price = position.last_price
                if snapshot:
                    item = {
                        "code": position.code, "name": position.name, "price": snapshot.get("price") or price,
                        "buy_price": position.buy_price,
                        "return_pct": snapshot.get("return_pct", (price / position.buy_price - 1) * 100 if price else 0),
                        "drawdown_pct": snapshot.get("drawdown_pct", (position.highest_price - price) / position.highest_price * 100 if price and position.highest_price else 0),
                        "risk_level": snapshot.get("risk_level", position.risk_level),
                        "reasons": snapshot.get("reasons") or last_reason.get(position.code, ["等待最新分析"]),
                        "boll_state": snapshot.get("boll_state") or "等待分析",
                        "macd_state": snapshot.get("macd_state") or "等待分析",
                        "kdj_state": snapshot.get("kdj_state") or "等待分析",
                        "volume_state": snapshot.get("volume_state") or "等待分析",
                        "support": snapshot.get("support"), "major_support": snapshot.get("major_support"),
                        "resistance": snapshot.get("resistance"), "major_resistance": snapshot.get("major_resistance"),
                        "support_zone": snapshot.get("support_zone"), "play_zone": snapshot.get("play_zone"),
                        "pressure_zone": snapshot.get("pressure_zone"), "strong_pressure_zone": snapshot.get("strong_pressure_zone"),
                        "zone_method": snapshot.get("zone_method"), "position_advice": snapshot.get("position_advice"),
                        "position_advice_detail": snapshot.get("position_advice_detail"),
                    }
                else:
                    item = {
                        "code": position.code, "name": position.name, "price": price,
                        "buy_price": position.buy_price,
                        "return_pct": (price / position.buy_price - 1) * 100 if price else 0,
                        "drawdown_pct": (position.highest_price - price) / position.highest_price * 100 if price and position.highest_price else 0,
                        "risk_level": position.risk_level, "reasons": last_reason.get(position.code, ["等待最新分析"]),
                        "boll_state": "等待分析", "macd_state": "等待分析", "kdj_state": "等待分析",
                        "volume_state": "等待分析", "support": None, "major_support": None,
                        "resistance": None, "major_resistance": None,
                        "support_zone": None, "play_zone": None, "pressure_zone": None,
                        "strong_pressure_zone": None, "zone_method": None,
                        "position_advice": None, "position_advice_detail": None,
                    }
            items.append(item)
        items.sort(key=lambda item: (-int(item["risk_level"]), item["code"]))
        alerts = [
            {"time": row["created_at"][5:16], "name": row["name"], "code": row["code"], "reason": row["reason"].split("；")[0]}
            for row in alert_rows
        ]
        from datetime import datetime
        return {"updated_at": datetime.now().strftime("%m-%d %H:%M"), "positions": items, "alerts": alerts}

    def _refresh_alerts(self) -> None:
        for item in self.alert_tree.get_children(): self.alert_tree.delete(item)
        for row in self.db.recent_alerts():
            self.alert_tree.insert("", "end", values=(row["created_at"], f'{row["name"]} {row["code"]}', f'{row["old_risk"]} → {row["new_risk"]}', f'{row["price"]:.2f}', row["reason"]))

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event == "show":
                    self.root.deiconify(); self.root.state("normal"); self.root.lift()
                elif event == "exit":
                    self.shutdown(); return
                elif isinstance(event, tuple):
                    kind, payload = event
                    if kind == "status":
                        self.status_var.set(str(payload))
                        self.monitor_button.configure(text="停止监控" if self.monitor.running else "开始监控")
                    elif kind == "error": self.status_var.set(f"行情错误：{payload}")
                    elif kind == "analysis":
                        self.refresh_positions()
                        self._render_detail(self.selected_code or payload.code)
                    elif kind == "cycle": self.status_var.set(f"最近刷新 {payload['time']:%H:%M:%S}｜{payload['total']}只持仓｜失败{payload['failures']}只")
                    elif kind == "movement_update":
                        sudden = list(payload.get("sudden") or [])
                        steady = list(payload.get("steady") or [])
                        self._render_movement_rows(sudden, steady)
                        self.movement_status_var.set(
                            f"{payload.get('time')} 已扫描{payload.get('source_count', 0)}只｜"
                            f"突然拉升{len(sudden)}只｜匀速上涨{len(steady)}只｜用时{payload.get('elapsed', 0):.1f}秒"
                        )
                    elif kind == "movement_error":
                        self.movement_status_var.set(f"异动监控失败：{payload}")
                        self.status_var.set(f"异动监控失败：{payload}")
                    elif kind == "notification_test": messagebox.showinfo("测试通知", str(payload))
                    elif kind == "auction_status": self.auction_status_var.set(str(payload))
                    elif kind == "auction_error":
                        self.auction_status_var.set(str(payload)); self.status_var.set(str(payload))
                    elif kind == "auction_sample":
                        quality_text = "数据正常" if payload.get("quality") == "passed" else f"数据作废：{payload.get('error') or '质量未通过'}"
                        self.auction_status_var.set(
                            f"{payload['label']} 已采集 {payload['count']} 只，用时 {payload['elapsed']:.1f}秒｜"
                            f"覆盖{payload.get('coverage', 0):.1%}｜跨页{payload.get('span', 0):.1f}秒｜{quality_text}"
                        )
                    elif kind == "auction_outcome":
                        self.auction_status_var.set(f"已记录{payload['label']}表现 {payload['count']}只；9:25结论未改变")
                        self._render_auction_rows()
                        self.refresh_backtest()
                    elif kind == "auction_decisions":
                        self.auction.latest_decisions = list(payload)
                        self._render_auction_rows()
                        self.auction_status_var.set(
                            "9:25数据已锁定，结果已生成；9:30后不再重新选股"
                            if payload else "等待今日竞价采样；请在9:15前保持软件运行"
                        )
                        self.refresh_backtest()
                    elif kind == "candidate_zones":
                        rows = list(payload.get("rows") or [])
                        errors = dict(payload.get("errors") or {})
                        try:
                            self._save_candidate_zones(rows)
                        except Exception as exc:
                            errors["保存"] = str(exc)
                        if rows:
                            self.auction_status_var.set(f"已计算 {len(rows)} 只候选的三个区间")
                        if errors:
                            self.status_var.set("部分区间计算失败：" + "；".join(f"{code} {error}" for code, error in errors.items()))
                        if payload.get("sync_after") and rows:
                            self.auction_status_var.set(f"三个区间已算好，正在同步 {len(rows)} 只到同花顺自选…")
                            self._run_ths_sync(rows)
                        elif rows:
                            messagebox.showinfo("三个区间", "区间已计算并保存。\n\n" + "\n".join(
                                f"{row.get('name')} {row.get('code')}：{zone_summary(row)}" for row in rows
                            ))
                    elif kind == "ths_sync_done":
                        self.root.deiconify(); self.root.state("normal"); self.root.lift()
                        rows = list(payload.get("rows") or [])
                        self.auction_status_var.set(f"已向同花顺发送 {len(payload.get('codes') or [])} 只自选指令，三个区间已保存")
                        messagebox.showinfo("同步完成", "已完成自选同步，请按 F6 查看同花顺自选。\n\n" + "\n".join(
                            f"{row.get('name')} {row.get('code')}：{zone_summary(row)}" for row in rows
                        ))
                    elif kind == "ths_sync_error":
                        self.root.deiconify(); self.root.state("normal"); self.root.lift()
                        self.auction_status_var.set("同花顺自选同步已停止")
                        messagebox.showwarning("同步同花顺", str(payload))
        except queue.Empty:
            pass
        self.root.after(250, self._drain_events)

    def hide_to_tray(self) -> None:
        self.root.withdraw()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.movement.stop(); self.web.stop(); self.auction.shutdown(); self.monitor.shutdown(); self.tray.stop(); self.root.after(100, self.root.destroy)


def run_app(base_dir: Path, background: bool = False) -> None:
    root = tk.Tk()
    app = StockMonitorApp(root, Database(base_dir / "data" / "stock_monitor.db"), base_dir, background)
    try:
        root.mainloop()
    finally:
        app.movement.stop(); app.web.stop(); app.auction.stop(); app.monitor.stop(); app.tray.stop()
