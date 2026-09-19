from __future__ import annotations

import queue
import unittest

from stock_monitor.big_movement import BigMovementMonitor


def row(code: str, pct: float, amount: float = 50_000_000) -> dict:
    return {
        "code": code,
        "name": "测试股票",
        "industry": "测试板块",
        "price": 10.0,
        "pct": pct,
        "open_pct": 0.2,
        "amount": amount,
    }


class BigMovementTests(unittest.TestCase):
    def test_sudden_lift_requires_recent_jump(self):
        monitor = BigMovementMonitor(queue.Queue())
        monitor.classify([row("600001", 1.5)])
        result = monitor.classify([row("600001", 2.6)])
        self.assertTrue(result["sudden"])
        self.assertEqual(result["sudden"][0].code, "600001")

    def test_steady_rise_requires_repeated_smooth_steps(self):
        monitor = BigMovementMonitor(queue.Queue())
        monitor.classify([row("600002", 2.0)])
        monitor.classify([row("600002", 2.3)])
        monitor.classify([row("600002", 2.6)])
        result = monitor.classify([row("600002", 2.9)])
        self.assertTrue(result["steady"])
        self.assertEqual(result["steady"][0].code, "600002")

    def test_filters_low_amount_and_excluded_boards(self):
        monitor = BigMovementMonitor(queue.Queue())
        monitor.classify([row("688001", 1.0, 100_000_000), row("600003", 1.0, 5_000_000)])
        result = monitor.classify([row("688001", 3.0, 100_000_000), row("600003", 3.0, 5_000_000)])
        self.assertFalse(result["sudden"])
