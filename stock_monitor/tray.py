from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading


WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
IDI_APPLICATION = 32512
TPM_RIGHTBUTTON = 0x0002
MF_STRING = 0x0000


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
    ]


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class TrayIcon:
    def __init__(self, event_queue: queue.Queue[str], tooltip: str = "股票持仓监控助手"):
        self.events = event_queue
        self.tooltip = tooltip
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._wndproc_ref = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
        ]
        user32.CreateWindowExW.restype = wt.HWND
        user32.LoadIconW.restype = wt.HICON
        user32.CreatePopupMenu.restype = wt.HMENU
        kernel32.GetModuleHandleW.restype = wt.HMODULE
        class_name = "StockMonitorTrayWindow"
        callback_message = WM_APP + 1

        def window_proc(hwnd, msg, wparam, lparam):
            if msg == callback_message:
                if lparam == WM_LBUTTONDBLCLK:
                    self.events.put("show")
                elif lparam == WM_RBUTTONUP:
                    menu = user32.CreatePopupMenu()
                    user32.AppendMenuW(menu, MF_STRING, 1001, "打开监控助手")
                    user32.AppendMenuW(menu, MF_STRING, 1002, "退出")
                    point = wt.POINT()
                    user32.GetCursorPos(ctypes.byref(point))
                    user32.SetForegroundWindow(hwnd)
                    user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
                    user32.DestroyMenu(menu)
                return 0
            if msg == WM_COMMAND:
                command = int(wparam) & 0xFFFF
                if command == 1001:
                    self.events.put("show")
                elif command == 1002:
                    self.events.put("exit")
                return 0
            if msg == WM_DESTROY:
                if self._hwnd:
                    data = NOTIFYICONDATAW(); data.cbSize = ctypes.sizeof(data); data.hWnd = hwnd; data.uID = 1
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(window_proc)
        instance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = instance
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, None, None, instance, None)
        self._hwnd = hwnd
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data); data.hWnd = hwnd; data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = callback_message
        data.hIcon = user32.LoadIconW(None, IDI_APPLICATION)
        data.szTip = self.tooltip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        message = wt.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self._hwnd = None
