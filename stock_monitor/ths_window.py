from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ThsWindow:
    hwnd: int
    title: str
    process_name: str = ""


def _window_process_name(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(0x1000, False, pid.value)
    if not process:
        return ""
    try:
        size = wt.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value).lower()
    finally:
        kernel32.CloseHandle(process)
    return ""


def find_ths_windows() -> list[ThsWindow]:
    user32 = ctypes.windll.user32
    windows: list[ThsWindow] = []
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def enum_callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        lowered = title.lower()
        process_name = _window_process_name(hwnd)
        if "同花顺" in title or "ths" in lowered or "ifind" in lowered or process_name == "hexin.exe":
            windows.append(ThsWindow(int(hwnd), title, process_name))
        return True

    callback = callback_type(enum_callback)
    user32.EnumWindows(callback, 0)
    return windows


def capture_window(window: ThsWindow, output_dir: str | Path) -> Path:
    from PIL import ImageGrab

    rect = wt.RECT()
    if not ctypes.windll.user32.GetWindowRect(window.hwnd, ctypes.byref(rect)):
        raise RuntimeError("无法读取同花顺窗口位置")
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise RuntimeError("同花顺窗口当前不可见，请先恢复窗口")
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "ths_current.png"
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(target)
    return target
