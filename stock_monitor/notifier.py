from __future__ import annotations

import subprocess
from xml.sax.saxutils import escape

from .models import Analysis, Position


class WindowsNotifier:
    def __init__(self, app_name: str = "股票持仓监控助手"):
        self.app_name = app_name

    def show(self, position: Position, analysis: Analysis, old_risk: int) -> None:
        title = f"{position.name} {position.code}｜风险 {old_risk} → {analysis.risk_level}"
        reason = "；".join(analysis.reasons[:3])
        body = (
            f"当前 {analysis.price:.2f}｜成本 {position.buy_price:.2f}｜"
            f"收益 {analysis.return_pct:+.2f}%\n{reason}\n请打开软件查看"
        )
        self._toast(title, body)

    def _toast(self, title: str, body: str) -> None:
        xml = (
            "<toast><visual><binding template='ToastGeneric'>"
            f"<text>{escape(title)}</text><text>{escape(body)}</text>"
            "</binding></visual></toast>"
        )
        safe_xml = xml.replace("'", "''")
        safe_app = self.app_name.replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop';"
            "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null;"
            "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null;"
            f"$x=New-Object Windows.Data.Xml.Dom.XmlDocument;$x.LoadXml('{safe_xml}');"
            "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{safe_app}').Show($t)"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags,
            )
        except OSError:
            pass
