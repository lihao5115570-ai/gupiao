from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from stock_monitor.database import Database
from stock_monitor.mobile_notifications import AlertEvent, EventGate
from stock_monitor.mobile_notifications import NotificationDispatcher
from stock_monitor.mobile_web import MobileWebServer
from stock_monitor.security import hash_password, new_access_token, protect_secret, unprotect_secret, verify_password


class NotificationAndWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_secret_roundtrip_and_password_hash(self):
        encrypted = protect_secret("phone-token")
        self.assertNotIn("phone-token", encrypted)
        self.assertEqual("phone-token", unprotect_secret(encrypted))
        password_hash = hash_password("strong-pass-88")
        self.assertTrue(verify_password("strong-pass-88", password_hash))
        self.assertFalse(verify_password("wrong-pass", password_hash))

    def test_state_lock_requires_recovery(self):
        gate = EventGate(self.db)
        settings = {"notify_cooldown_minutes": "30", "notify_event_support_break": "1"}
        active = {"support_break": AlertEvent("support_break", "跌破35.40支撑", True)}
        recovered = {"support_break": AlertEvent("support_break", "站回35.40支撑", False)}
        self.assertEqual(1, len(gate.select("601606", active, settings)))
        self.assertEqual([], gate.select("601606", active, settings))
        self.assertEqual([], gate.select("601606", recovered, settings))
        self.assertEqual([], gate.select("601606", active, settings), "冷却期内再次跌破仍不应发送")

    def test_bark_and_pushplus_payloads(self):
        from unittest.mock import patch
        dispatcher = object.__new__(NotificationDispatcher)
        with patch.object(NotificationDispatcher, "_post") as post:
            dispatcher._send_mobile(
                {"notify_mobile_service": "bark", "secret_bark_key": protect_secret("bark-key"), "bark_server": "https://api.day.app"},
                "风险提醒", "跌破支撑",
            )
            self.assertIn("bark-key", post.call_args.args[0])
            post.reset_mock()
            dispatcher._send_mobile(
                {"notify_mobile_service": "pushplus", "secret_pushplus_token": protect_secret("push-token")},
                "风险提醒", "跌破支撑",
            )
            self.assertEqual("push-token", post.call_args.args[1]["token"])

    def test_web_requires_login_and_serves_api_after_login(self):
        assets = Path(__file__).resolve().parents[1] / "stock_monitor" / "web"
        token = new_access_token()
        settings = {
            "web_password_hash": hash_password("mobile-pass-88"),
            "secret_web_token": protect_secret(token),
        }
        server = MobileWebServer(assets, lambda: settings, lambda: {"positions": [], "alerts": [], "updated_at": "09-11 10:00"})
        server.start("127.0.0.1", 0)
        try:
            host, port = server.address
            base = f"http://{host}:{port}"
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/api/status", timeout=3)
            self.assertEqual(401, error.exception.code)
            request = urllib.request.Request(base + "/api/status", headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual([], payload["positions"])
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            login = urllib.parse.urlencode({"password": "mobile-pass-88"}).encode()
            with opener.open(urllib.request.Request(base + "/login", data=login), timeout=3) as response:
                self.assertEqual(base + "/", response.url)
                self.assertIn("持仓监控", response.read().decode("utf-8"))
            with opener.open(base + "/api/status", timeout=3) as response:
                self.assertEqual(200, response.status)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
