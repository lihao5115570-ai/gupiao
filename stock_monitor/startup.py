from __future__ import annotations

import winreg
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE = "StockPositionMonitor"


def set_startup(enabled: bool, launcher: str | Path) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_VALUE, 0, winreg.REG_SZ, f'"{Path(launcher).resolve()}" --background')
        else:
            try:
                winreg.DeleteValue(key, APP_VALUE)
            except FileNotFoundError:
                pass
