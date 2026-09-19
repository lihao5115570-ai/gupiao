from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace
from datetime import datetime, time as clock_time
from typing import Any

from .data_provider import ResilientProvider
from .database import Database
from .explainer import ExplanationService
from .indicators import calculate, price_structure
from .models import Analysis, Bar, Position
from .mobile_notifications import EventGate, NotificationDispatcher, NotificationJob, notification_conditions
from .risk_engine import evaluate


def is_trading_session(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return clock_time(9, 30) <= current <= clock_time(11, 30) or clock_time(13, 0) <= current <= clock_time(15, 0)


class MonitorEngine:
    def __init__(self, database: Database, events: queue.Queue[tuple[str, Any]], provider: Any | None = None):
        self.db = database
        self.events = events
        self.provider = provider or ResilientProvider()
        self.notification_gate = EventGate(database)
        self.notifications = NotificationDispatcher(database, events)
        self.explainer = ExplanationService()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache: dict[tuple[str, str], tuple[float, list[Bar]]] = {}
        self.latest: dict[str, Analysis] = {}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="stock-monitor", daemon=True)
        self._thread.start()
        self.events.put(("status", "监控运行中"))

    def stop(self) -> None:
        self._stop.set(); self._wake.set()
        self.events.put(("status", "监控已停止"))

    def shutdown(self) -> None:
        self.stop()
        self.notifications.close()

    def refresh_now(self) -> None:
        if not self.running:
            self.start()
        self._cache.clear()
        self._wake.set()

    def _bars(self, code: str, timeframe: str, max_age: int) -> list[Bar]:
        key = (code, timeframe)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        bars = self.provider.klines(code, timeframe, 180)
        self._cache[key] = (time.monotonic(), bars)
        self.db.save_bars(code, timeframe, bars)
        return bars

    def _analyze_position(self, position: Position, settings: dict[str, str]) -> None:
        quote = self.provider.quote(position.code)
        self.db.save_quote(quote)
        daily = self._bars(position.code, "daily", 1800)
        bars_60 = self._bars(position.code, "60m", 600)
        bars_15 = self._bars(position.code, "15m", 180)
        daily_ind = calculate(daily)
        intraday = {"60m": calculate(bars_60), "15m": calculate(bars_15)}
        structure = price_structure(daily)
        old_risk = position.risk_level
        analysis = evaluate(position, quote, daily, daily_ind, structure, settings, intraday)
        highest = float(analysis.indicators["highest_price"])
        self.db.update_position_state(int(position.id), analysis.price, highest, analysis.risk_level)
        self.db.save_analysis(position, analysis, old_risk)
        self.latest[position.code] = analysis
        important = self.explainer.should_generate(analysis, old_risk)
        if important:
            analysis = replace(analysis, explanation=self.explainer.explain(analysis))
            self.latest[position.code] = analysis
        conditions = notification_conditions(position, analysis, old_risk)
        notification_events = self.notification_gate.select(position.code, conditions, settings)
        if notification_events:
            self.notifications.submit(NotificationJob(position, analysis, old_risk, notification_events))
        self.events.put(("analysis", analysis))

    def _run_cycle(self) -> None:
        positions = self.db.list_positions()
        settings = self.db.get_settings()
        if not positions:
            self.events.put(("status", "暂无持仓，请先添加"))
            return
        failures = 0
        for position in positions:
            if self._stop.is_set():
                break
            try:
                self._analyze_position(position, settings)
            except Exception as exc:
                failures += 1
                self.events.put(("error", f"{position.name} {position.code}：{exc}"))
        self.events.put(("cycle", {"total": len(positions), "failures": failures, "time": datetime.now()}))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self._run_cycle()
            settings = self.db.get_settings()
            seconds = int(settings.get("poll_seconds", "60")) if is_trading_session() else int(settings.get("off_hours_poll_seconds", "1800"))
            self._wake.wait(max(15, seconds))
