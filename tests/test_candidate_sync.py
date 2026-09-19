from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from stock_monitor.candidate_sync import (
    CandidateSyncError,
    ThsWatchlistController,
    calculate_candidate_zone,
    calculate_candidate_zones,
    zone_summary,
)
from stock_monitor.models import Bar
from stock_monitor.database import Database
from stock_monitor.ths_window import ThsWindow


def sample_bars() -> list[Bar]:
    bars = []
    for index in range(80):
        close = 10 + index * 0.03
        bars.append(Bar(f"2026-05-{index + 1:02d}", close - 0.05, close, close + 0.12, close - 0.12, 1_000_000 + index, 0))
    return bars


class FakeProvider:
    def klines(self, code, timeframe, limit):
        self.last_call = (code, timeframe, limit)
        return sample_bars()


class RecordingController(ThsWatchlistController):
    def __init__(self, windows):
        super().__init__(window_finder=lambda: windows, pause=lambda _seconds: None)
        self.keys = []

    def _activate(self, window):
        self.activated = window

    def _key(self, vk):
        self.keys.append(vk)


class CandidateZoneTests(unittest.TestCase):
    def test_candidate_gets_complete_three_zones(self):
        row = {"code": "600001", "name": "样本", "action": "可挂入", "lock_price": 12.4}
        result = calculate_candidate_zone(row, FakeProvider())
        zones = result["trade_zones"]
        self.assertLessEqual(zones["support_zone_low"], zones["support_zone_high"])
        self.assertLessEqual(zones["play_zone_low"], zones["play_zone_high"])
        self.assertLessEqual(zones["pressure_zone_low"], zones["pressure_zone_high"])
        self.assertIn("支撑区", zone_summary(result))

    def test_batch_reports_invalid_code_without_losing_valid_result(self):
        rows = [
            {"code": "600001", "action": "可挂入", "lock_price": 12.4},
            {"code": "bad", "action": "等回踩", "lock_price": 10},
        ]
        results, errors = calculate_candidate_zones(rows, FakeProvider(), workers=2)
        self.assertEqual(["600001"], [row["code"] for row in results])
        self.assertIn("bad", errors)

    def test_watchlist_automation_uses_only_lookup_enter_and_insert(self):
        controller = RecordingController([ThsWindow(100, "同花顺 - 个股K线", "hexin.exe")])
        completed = controller.add_codes(["600001", "600001", "000002"])
        self.assertEqual(["600001", "000002"], completed)
        allowed = {0x1B, 0x0D, 0x2D, *range(ord("0"), ord("9") + 1)}
        self.assertTrue(set(controller.keys).issubset(allowed))
        self.assertEqual(2, controller.keys.count(0x2D))

    def test_trading_window_is_rejected(self):
        controller = RecordingController([ThsWindow(100, "同花顺委托下单", "hexin.exe")])
        with self.assertRaises(CandidateSyncError):
            controller.add_codes(["600001"])

    def test_calculated_zones_can_be_saved_back_to_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "zones.db")
            row = {"rank": 1, "code": "600001", "name": "样本", "action": "可挂入", "score": 90, "lock_price": 12.4}
            db.save_auction_decisions("2026-09-15", [row])
            calculated = calculate_candidate_zone(row, FakeProvider())
            db.update_auction_decision_payload("2026-09-15", calculated)
            saved = db.auction_decisions("2026-09-15")[0]
            self.assertIn("trade_zones", saved)
            self.assertEqual("可挂入", saved["action"])


if __name__ == "__main__":
    unittest.main()
