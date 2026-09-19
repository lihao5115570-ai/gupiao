from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stock_monitor.auction import REQUIRED_PATH
from stock_monitor.backtest import AuctionBacktester, apply_backtest_gate, meets_targets, stamp_profile, summarize_signals
from stock_monitor.database import Database


def paths(day: str) -> list[dict]:
    result = []
    for stock_index in range(5):
        code = f"600{stock_index + 1:03d}"
        for point_index, label in enumerate(REQUIRED_PATH):
            pct = 1.2 + stock_index * 0.1 + point_index * 0.08
            result.append({
                "trading_day": day, "sample_label": label, "sampled_at": f"{day} {label}",
                "code": code, "name": f"样本{stock_index}", "industry": "测试板块",
                "auction_price": 10 + stock_index, "auction_pct": pct,
                "auction_amount": 80_000_000, "prev_close": 9.8 + stock_index,
                "open_price": 10 + stock_index, "market_value": 10_000_000_000,
                "float_market_value": 8_000_000_000,
            })
    return result


class BacktestTests(unittest.TestCase):
    def test_metrics_include_mae_and_failure_streak(self):
        rows = [
            {"trading_day": "2026-01-01", "code": "1", "mae": 0, "no_break": True, "close_win": True},
            {"trading_day": "2026-01-02", "code": "2", "mae": 1, "no_break": False, "close_win": False},
            {"trading_day": "2026-01-03", "code": "3", "mae": 3, "no_break": False, "close_win": False},
        ]
        value = summarize_signals(rows)
        self.assertAlmostEqual(4 / 3, value.average_mae)
        self.assertEqual(3, value.maximum_mae)
        self.assertEqual(2, value.consecutive_failures)
        self.assertFalse(meets_targets(value))

    def test_gate_never_claims_orderable_without_audit(self):
        rows = apply_backtest_gate([{"action": "可挂入", "score": 90}], {"qualified": False, "profile": "保守试验版"})
        self.assertEqual("试验观察", rows[0]["action"])
        self.assertFalse(rows[0]["can_order"])

    def test_thirty_days_and_sixty_live_signals_can_qualify(self):
        settings = {
            "auction_min_amount": "20000000", "auction_high_price": "100",
            "auction_min_retention": "0.70", "auction_late_drop": "0.50",
            "auction_max_drawdown": "2.00", "auction_min_float_amount_ratio": "0.0005",
            "auction_min_market_positive_ratio": "0.35", "auction_max_orderable": "2",
            "auction_max_per_sector": "2", "auction_profile_name": "保守试验版",
        }
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "backtest.db")
            for index in range(30):
                day = f"2026-07-{index + 1:02d}"
                day_rows = paths(day)
                for label in REQUIRED_PATH:
                    db.save_auction_snapshot(day, label, f"{day} {label}", [row for row in day_rows if row["sample_label"] == label])
                    db.save_auction_sample_run({
                        "trading_day": day, "sample_label": label, "target_at": f"{day} {label}",
                        "started_at": f"{day} {label}", "finished_at": f"{day} {label}",
                        "coverage": 1, "status": "passed", "source_kind": "live",
                    })
                decisions = stamp_profile([
                    {"rank": stock + 1, "code": f"600{stock + 1:03d}", "name": f"样本{stock}",
                     "action": "可挂入", "screen_action": "可挂入", "score": 90,
                     "lock_price": 10 + stock}
                    for stock in range(2)
                ], settings)
                db.save_auction_decisions(day, decisions)
                db.save_auction_outcomes(day, "15:00", f"{day} 15:00:05", [
                    {"code": f"600{stock + 1:03d}", "auction_price": 10.2 + stock,
                     "low": 10 + stock, "high": 10.5 + stock, "source_kind": "live"}
                    for stock in range(5)
                ])
            report = AuctionBacktester(db).run(settings)
            self.assertEqual(30, report["overall"]["trading_days"])
            self.assertEqual(60, report["overall"]["signals"])
            self.assertEqual(10, report["holdout_10"]["trading_days"])
            self.assertTrue(report["qualified"])

            changed = dict(settings, auction_min_retention="0.92")
            changed_report = AuctionBacktester(db).run(changed)
            self.assertEqual(0, changed_report["overall"]["signals"])
            self.assertFalse(changed_report["qualified"])

    def test_unknown_source_is_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "unknown.db")
            day_rows = paths("2026-08-01")
            for label in REQUIRED_PATH:
                db.save_auction_snapshot("2026-08-01", label, f"2026-08-01 {label}", [row for row in day_rows if row["sample_label"] == label])
                db.save_auction_sample_run({
                    "trading_day": "2026-08-01", "sample_label": label,
                    "target_at": f"2026-08-01 {label}", "started_at": f"2026-08-01 {label}",
                    "finished_at": f"2026-08-01 {label}", "status": "passed",
                })
            report = AuctionBacktester(db).run({})
            self.assertEqual(0, report["overall"]["signals"])
            self.assertEqual(1, report["excluded_days"])


if __name__ == "__main__":
    unittest.main()
