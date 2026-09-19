from __future__ import annotations

import ctypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable

from .data_provider import ResilientProvider
from .indicators import price_structure
from .ths_window import ThsWindow, find_ths_windows
from .zones import calculate_trade_zones


ZONE_KEYS = (
    "support_zone_low", "support_zone_high",
    "play_zone_low", "play_zone_high",
    "pressure_zone_low", "pressure_zone_high",
)


class CandidateSyncError(RuntimeError):
    pass


def calculate_candidate_zone(row: dict[str, Any], provider: Any) -> dict[str, Any]:
    code = str(row.get("code") or "")
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"股票代码无效：{code or '--'}")
    bars = provider.klines(code, "daily", 180)
    if len(bars) < 30:
        raise ValueError(f"{code}日K数据不足")
    reference_price = float(row.get("lock_price") or row.get("auction_price") or bars[-1].close)
    zones = calculate_trade_zones(reference_price, price_structure(bars), bars)
    if any(zones.get(key) is None for key in ZONE_KEYS):
        raise ValueError(f"{code}无法形成完整的三区间")
    result = dict(row)
    result["trade_zones"] = zones
    result["zone_calculated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result["zone_reference_price"] = reference_price
    return result


def calculate_candidate_zones(
    rows: list[dict[str, Any]], provider: Any | None = None, workers: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    provider = provider or ResilientProvider()
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4)), thread_name_prefix="candidate-zone") as pool:
        futures = {pool.submit(calculate_candidate_zone, row, provider): str(row.get("code") or "") for row in rows}
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                errors[code] = str(exc)
    return [results[str(row.get("code"))] for row in rows if str(row.get("code")) in results], errors


def zone_summary(row: dict[str, Any]) -> str:
    zones = row.get("trade_zones") or {}
    return (
        f"支撑区 {zones.get('support_zone', '--')}  |  "
        f"博弈区 {zones.get('play_zone', '--')}  |  "
        f"压力区 {zones.get('pressure_zone', '--')}"
    )


class ThsWatchlistController:
    """Restricted THS automation: stock lookup and Insert only; never opens trading UI."""

    FORBIDDEN_TITLES = ("委托", "下单", "买入", "卖出", "交易登录", "模拟交易")

    def __init__(
        self, window_finder: Callable[[], list[ThsWindow]] = find_ths_windows,
        pause: Callable[[float], None] = time.sleep,
    ):
        self.window_finder = window_finder
        self.pause = pause
        self._lock = threading.Lock()

    def _choose_window(self) -> ThsWindow:
        windows = self.window_finder()
        safe = [window for window in windows if not any(word in window.title for word in self.FORBIDDEN_TITLES)]
        if not safe:
            raise CandidateSyncError("未发现可见的同花顺行情窗口，请先退出委托页并打开普通个股K线。")
        safe.sort(key=lambda item: (item.process_name != "hexin.exe", -len(item.title)))
        return safe[0]

    @staticmethod
    def _key(vk: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)

    def _activate(self, window: ThsWindow) -> None:
        user32 = ctypes.windll.user32
        user32.ShowWindow(window.hwnd, 9)
        if not user32.SetForegroundWindow(window.hwnd):
            raise CandidateSyncError("无法激活同花顺窗口，请手动点一下同花顺后重试。")
        self.pause(0.35)
        if user32.GetForegroundWindow() != window.hwnd:
            raise CandidateSyncError("同花顺未成为当前窗口，同步已停止。")

    def add_codes(self, codes: list[str]) -> list[str]:
        clean = list(dict.fromkeys(str(code) for code in codes if len(str(code)) == 6 and str(code).isdigit()))
        if not clean:
            raise CandidateSyncError("没有可同步的股票代码。")
        with self._lock:
            window = self._choose_window()
            self._activate(window)
            completed: list[str] = []
            for code in clean:
                self._key(0x1B)
                for char in code:
                    self._key(ord(char))
                    self.pause(0.025)
                self.pause(0.45)
                self._key(0x0D)
                self.pause(0.75)
                self._key(0x2D)
                self.pause(0.45)
                completed.append(code)
            return completed
