from __future__ import annotations

import json
import queue
import smtplib
import ssl
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from .database import Database
from .models import Analysis, Position
from .notifier import WindowsNotifier
from .security import unprotect_secret


EVENT_LABELS = {
    "risk_change": "风险等级上升",
    "support_break": "跌破支撑",
    "resistance_break": "突破压力",
    "macd_death": "MACD死叉",
    "kdj_death": "KDJ死叉",
    "profit_drawdown": "利润回撤",
    "below_cost": "跌破成本",
    "stop_loss": "达到止损线",
    "target_profit": "达到止盈提醒线",
    "stall": "放量滞涨",
    "false_break": "假突破",
}


@dataclass(slots=True)
class AlertEvent:
    event_type: str
    reason: str
    active: bool


@dataclass(slots=True)
class NotificationJob:
    position: Position
    analysis: Analysis
    old_risk: int
    events: list[AlertEvent]
    test: bool = False


def notification_conditions(position: Position, analysis: Analysis, old_risk: int) -> dict[str, AlertEvent]:
    indicators = analysis.indicators
    events_text = set(analysis.events)
    return {
        "risk_change": AlertEvent(
            "risk_change", f"风险等级 {old_risk} → {analysis.risk_level}",
            analysis.risk_level != old_risk and (max(old_risk, analysis.risk_level) >= 3 or abs(analysis.risk_level - old_risk) >= 2),
        ),
        "support_break": AlertEvent("support_break", f"跌破 {analysis.major_support:.2f} 重要支撑" if analysis.major_support else "跌破重要支撑", bool(analysis.major_support and analysis.price < analysis.major_support)),
        "resistance_break": AlertEvent("resistance_break", f"突破 {analysis.major_resistance:.2f} 重要压力" if analysis.major_resistance else "突破重要压力", bool(analysis.major_resistance and analysis.price > analysis.major_resistance)),
        "macd_death": AlertEvent("macd_death", "MACD出现死叉", indicators.get("macd_cross") == "死叉"),
        "kdj_death": AlertEvent("kdj_death", "KDJ出现高位死叉", indicators.get("kdj_cross") == "死叉" and float(indicators.get("kdj_j", 0)) >= 70),
        "profit_drawdown": AlertEvent("profit_drawdown", f"最高浮盈 {analysis.highest_profit_pct:.1f}%，高点回撤 {analysis.drawdown_pct:.1f}%", "利润保护回撤提醒" in events_text),
        "below_cost": AlertEvent("below_cost", f"现价跌破成本 {position.buy_price:.2f}", analysis.price < position.buy_price),
        "stop_loss": AlertEvent("stop_loss", f"达到止损观察线 {position.stop_loss:.2f}" if position.stop_loss else "达到止损观察线", bool(position.stop_loss and analysis.price <= position.stop_loss)),
        "target_profit": AlertEvent("target_profit", f"收益达到止盈提醒线 {position.target_return:.1f}%" if position.target_return else "达到止盈提醒线", bool(position.target_return and analysis.return_pct >= position.target_return)),
        "stall": AlertEvent("stall", "出现放量滞涨迹象", any("情况A" in item or "放量滞涨" in item for item in events_text)),
        "false_break": AlertEvent("false_break", "突破前高后回落，存在假突破风险", any("情况C" in item or "假突破" in item for item in events_text)),
    }


class EventGate:
    def __init__(self, database: Database):
        self.db = database

    def select(self, code: str, conditions: dict[str, AlertEvent], settings: dict[str, str]) -> list[AlertEvent]:
        cooldown = timedelta(minutes=max(1, int(settings.get("notify_cooldown_minutes", "30"))))
        selected: list[AlertEvent] = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for event_type, event in conditions.items():
            state = self.db.notification_state(code, event_type)
            was_active = bool(state["active"]) if state else False
            enabled = settings.get(f"notify_event_{event_type}", "0") == "1"
            can_send = True
            if state and state["last_sent_at"]:
                try:
                    can_send = now - datetime.fromisoformat(state["last_sent_at"]) >= cooldown
                except ValueError:
                    can_send = True
            is_transition = event.active and not was_active
            should_send = enabled and is_transition and can_send
            self.db.set_notification_state(code, event_type, event.active, event.reason, should_send)
            if should_send:
                selected.append(event)
        return selected


def format_notification(job: NotificationJob) -> tuple[str, str]:
    analysis = job.analysis
    title = f"{job.position.name} {job.position.code}｜风险 {analysis.risk_level}/5"
    reasons = job.events or [AlertEvent("test", reason, True) for reason in analysis.reasons[:4]]
    numbered = "\n".join(f"{index}. {event.reason}" for index, event in enumerate(reasons[:4], 1))
    body = (
        f"现价：{analysis.price:.2f}\n成本：{job.position.buy_price:.2f}\n"
        f"收益：{analysis.return_pct:+.2f}%\n\n触发原因：\n{numbered}\n\n建议：请打开软件查看详情。"
    )
    return title, body


class NotificationDispatcher:
    def __init__(self, database: Database, app_events: queue.Queue[Any] | None = None):
        self.db = database
        self.app_events = app_events
        self.windows = WindowsNotifier()
        self.jobs: queue.Queue[NotificationJob | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, name="notification-dispatcher", daemon=True)
        self._thread.start()

    def submit(self, job: NotificationJob) -> None:
        self.jobs.put(job)

    def close(self) -> None:
        self.jobs.put(None)

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            self._dispatch(job)

    def _dispatch(self, job: NotificationJob) -> None:
        settings = self.db.get_settings()
        title, body = format_notification(job)
        event_type = "test" if job.test else ",".join(event.event_type for event in job.events)
        if settings.get("notify_windows", "1") == "1":
            self.windows.show(job.position, job.analysis, job.old_risk)
            self.db.log_delivery(job.position.code, event_type, "windows", "sent")
        channel_calls: list[tuple[str, Any]] = []
        if settings.get("notify_mobile") == "1":
            channel_calls.append((settings.get("notify_mobile_service", "bark"), self._send_mobile))
        if settings.get("notify_telegram") == "1": channel_calls.append(("telegram", self._send_telegram))
        if settings.get("notify_wecom") == "1": channel_calls.append(("wecom", self._send_wecom))
        if settings.get("notify_email") == "1": channel_calls.append(("email", self._send_email))
        for channel, sender in channel_calls:
            try:
                sender(settings, title, body)
                self.db.log_delivery(job.position.code, event_type, channel, "sent")
            except Exception as exc:
                message = f"{channel}推送失败：{type(exc).__name__}"
                self.db.log_delivery(job.position.code, event_type, channel, "failed", message)
                if self.app_events:
                    self.app_events.put(("error", message))
        if job.test and self.app_events:
            self.app_events.put(("notification_test", "测试通知已处理，请检查已启用的接收渠道"))

    @staticmethod
    def _post(url: str, data: dict, json_body: bool = False) -> None:
        headers = {"User-Agent": "StockMonitor/0.2"}
        if json_body:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        else:
            payload = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise RuntimeError("HTTP请求失败")

    def _send_mobile(self, settings: dict[str, str], title: str, body: str) -> None:
        service = settings.get("notify_mobile_service", "bark")
        if service == "bark":
            key = unprotect_secret(settings.get("secret_bark_key", ""))
            if not key: raise ValueError("未配置Bark Key")
            server = settings.get("bark_server", "https://api.day.app").rstrip("/")
            self._post(f"{server}/{urllib.parse.quote(key, safe='')}", {"title": title, "body": body, "group": "股票监控", "level": "timeSensitive"}, True)
        elif service == "pushplus":
            token = unprotect_secret(settings.get("secret_pushplus_token", ""))
            if not token: raise ValueError("未配置PushPlus Token")
            self._post("https://www.pushplus.plus/send", {"token": token, "title": title, "content": body, "template": "txt"}, True)
        elif service == "serverchan":
            key = unprotect_secret(settings.get("secret_serverchan_key", ""))
            if not key: raise ValueError("未配置Server酱SendKey")
            self._post(f"https://sctapi.ftqq.com/{urllib.parse.quote(key, safe='')}.send", {"title": title, "desp": body})
        else:
            raise ValueError("未知手机推送服务")

    def _send_telegram(self, settings: dict[str, str], title: str, body: str) -> None:
        token = unprotect_secret(settings.get("secret_telegram_token", ""))
        chat_id = settings.get("telegram_chat_id", "")
        if not token or not chat_id: raise ValueError("未配置Telegram")
        self._post(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": f"【{title}】\n\n{body}", "disable_web_page_preview": "true"})

    def _send_wecom(self, settings: dict[str, str], title: str, body: str) -> None:
        webhook = unprotect_secret(settings.get("secret_wecom_webhook", ""))
        if not webhook: raise ValueError("未配置企业微信Webhook")
        self._post(webhook, {"msgtype": "text", "text": {"content": f"【{title}】\n\n{body}"}}, True)

    @staticmethod
    def _send_email(settings: dict[str, str], title: str, body: str) -> None:
        host = settings.get("smtp_host", "")
        user = settings.get("smtp_user", "")
        recipient = settings.get("email_to", "")
        password = unprotect_secret(settings.get("secret_smtp_password", ""))
        if not all((host, user, recipient, password)): raise ValueError("邮箱配置不完整")
        message = EmailMessage(); message["Subject"] = title; message["From"] = user; message["To"] = recipient; message.set_content(body)
        port = int(settings.get("smtp_port", "465"))
        if settings.get("smtp_ssl", "1") == "1":
            with smtplib.SMTP_SSL(host, port, timeout=12, context=ssl.create_default_context()) as smtp:
                smtp.login(user, password); smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=12) as smtp:
                smtp.starttls(context=ssl.create_default_context()); smtp.login(user, password); smtp.send_message(message)
