from __future__ import annotations

import unittest
import tempfile
import queue
from pathlib import Path
from datetime import datetime

from stock_monitor.auction import AuctionEngine, REQUIRED_PATH, apply_crosscheck, apply_data_quality_gate, build_candidate_pool, score_auction_paths
from stock_monitor.database import Database


def make_path(
    code: str,
    name: str,
    industry: str,
    values: list[float],
    amount: float = 50_000_000,
    price: float = 10.5,
    prev_close: float = 10.0,
) -> list[dict]:
    return [
        {
            "trading_day": "2026-09-14", "sample_label": label,
            "sampled_at": f"2026-09-14 {label}:00" if len(label) == 5 else f"2026-09-14 {label}",
            "code": code, "name": name, "industry": industry,
            "auction_price": price, "auction_pct": pct, "auction_amount": amount,
            "prev_close": prev_close, "open_price": price, "market_value": 8_000_000_000,
            "float_market_value": 5_000_000_000,
        }
        for label, pct in zip(REQUIRED_PATH, values)
    ]


class AuctionDecisionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "auction_min_amount": "20000000", "auction_high_price": "100",
            "auction_min_retention": "0.70", "auction_late_drop": "0.50",
            "auction_max_drawdown": "2.00", "auction_include_chinext": "0",
            "auction_include_star": "0", "auction_include_bse": "0",
            "auction_include_st": "0", "auction_include_high_price": "0",
        }

    def _sector_peers(self) -> list[dict]:
        rows = []
        rows += make_path("600002", "板块乙", "测试板块", [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.5])
        rows += make_path("600003", "板块丙", "测试板块", [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.3])
        rows += make_path("600004", "板块丁", "测试板块", [0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.4])
        rows += make_path("600005", "板块戊", "测试板块", [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.2])
        return rows

    def test_candidate_pool_limits_price_board_sector_and_size(self):
        def candidate(code: str, name: str, sector: str, price: float, pct: float, amount: float) -> dict:
            return {
                "code": code, "name": name, "industry": sector,
                "auction_price": price, "auction_pct": pct, "auction_amount": amount,
                "market_value": 5_000_000_000, "float_market_value": 3_000_000_000,
            }

        rows = [
            candidate("600001", "样本甲", "板块A", 12, 2.0, 30_000_000),
            candidate("600002", "样本乙", "板块A", 20, 1.8, 20_000_000),
            candidate("600003", "样本丙", "板块A", 30, 1.5, 10_000_000),
            candidate("600004", "样本丁", "板块A", 40, 1.0, 8_000_000),
            candidate("600005", "高价股", "板块A", 61, 2.0, 50_000_000),
            candidate("300001", "创业股", "板块A", 10, 2.0, 50_000_000),
            candidate("600006", "ST样本", "板块A", 10, 2.0, 50_000_000),
        ]
        pool = build_candidate_pool(rows, {
            "auction_high_price": "60", "auction_pool_min_amount": "5000000",
            "auction_pool_max_per_sector": "2", "auction_pool_min_sector_positive": "3",
            "auction_pool_max_size": "500",
        })
        self.assertEqual(["600001", "600002"], [row["code"] for row in pool])

    def test_stable_resonant_candidate_is_orderable(self):
        rows = make_path("600001", "样本甲", "测试板块", [2.0, 2.2, 2.4, 2.5, 2.6, 2.7, 2.7]) + self._sector_peers()
        scored = score_auction_paths(rows, self.settings)
        result = {row["code"]: row for row in scored}
        self.assertEqual("可挂入", result["600001"]["action"])
        self.assertTrue(result["600001"]["can_order"])
        self.assertEqual("低", result["600001"]["false_risk"])
        self.assertEqual(1, sum(row["action"] == "可挂入" for row in scored))

    def test_low_retention_and_late_drop_are_eliminated(self):
        rows = make_path("600001", "样本甲", "测试板块", [2.0, 4.0, 5.0, 5.2, 4.8, 4.6, 3.0]) + self._sector_peers()
        result = {row["code"]: row for row in score_auction_paths(rows, self.settings)}["600001"]
        self.assertEqual("放弃", result["action"])
        self.assertTrue(any("留存率" in reason for reason in result["elimination_reasons"]))
        self.assertTrue(any("9:24:30后走弱" in reason for reason in result["elimination_reasons"]))

    def test_high_open_waits_for_pullback(self):
        rows = make_path("600001", "样本甲", "测试板块", [7.2, 7.4, 7.6, 7.8, 8.0, 8.1, 8.1], price=10.81) + self._sector_peers()
        result = {row["code"]: row for row in score_auction_paths(rows, self.settings)}["600001"]
        self.assertEqual("等回踩", result["action"])
        self.assertFalse(result["can_order"])

    def test_small_amount_and_disabled_board_are_eliminated(self):
        rows = make_path("300001", "创业样本", "测试板块", [2.0, 2.2, 2.4, 2.5, 2.6, 2.7, 2.7], amount=5_000_000) + self._sector_peers()
        result = {row["code"]: row for row in score_auction_paths(rows, self.settings)}["300001"]
        self.assertEqual("放弃", result["action"])
        self.assertTrue(any("创业板" in reason for reason in result["elimination_reasons"]))
        self.assertTrue(any("竞价额不足" in reason for reason in result["elimination_reasons"]))

    def test_bad_global_data_quality_forces_abandon(self):
        rows = make_path("600001", "样本甲", "测试板块", [2.0, 2.2, 2.4, 2.5, 2.6, 2.7, 2.7]) + self._sector_peers()
        scored = score_auction_paths(rows, self.settings)
        gated = apply_data_quality_gate(scored, ["09:25跨页时差过大"])
        self.assertTrue(all(row["action"] == "放弃" for row in gated))
        self.assertTrue(all(row["data_quality"] == "不可用" for row in gated))

    def test_second_source_must_match_price_and_timestamp(self):
        paths = make_path("600001", "样本甲", "测试板块", [2.0, 2.2, 2.4, 2.5, 2.6, 2.7, 2.7]) + self._sector_peers()
        checked_at = datetime.combine(datetime.now().date(), datetime.strptime("09:25:10", "%H:%M:%S").time())
        stamp = checked_at.strftime("%Y%m%d") + "092501"
        rows = score_auction_paths(paths, self.settings)
        verified = apply_crosscheck(rows, {"600001": {"price": 10.5, "pct": 2.7, "timestamp": stamp}}, checked_at=checked_at)
        self.assertEqual("可挂入", next(row for row in verified if row["code"] == "600001")["action"])
        rows = score_auction_paths(paths, self.settings)
        rejected = apply_crosscheck(rows, {"600001": {"price": 10.8, "pct": 5.7, "timestamp": stamp}}, checked_at=checked_at)
        self.assertEqual("放弃", next(row for row in rejected if row["code"] == "600001")["action"])

    def test_auction_quality_and_outcome_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "auction.db")
            path_rows = make_path("600001", "样本甲", "测试板块", [2.0, 2.2, 2.4, 2.5, 2.6, 2.7, 2.7]) + self._sector_peers()
            for label in REQUIRED_PATH:
                db.save_auction_snapshot("2026-09-14", label, f"2026-09-14 {label}", [row for row in path_rows if row["sample_label"] == label])
            self.assertEqual(35, len(db.auction_snapshots("2026-09-14")))
            run = {
                "trading_day": "2026-09-14", "sample_label": "09:25",
                "target_at": "2026-09-14 09:25:00", "started_at": "2026-09-14 09:25:00.100",
                "finished_at": "2026-09-14 09:25:05.000", "expected_total": 5000,
                "raw_count": 5000, "normalized_count": 4900, "coverage": 1,
                "page_span_seconds": 4.9, "start_delay_seconds": 0.1, "status": "passed", "error": "",
            }
            db.save_auction_sample_run(run)
            self.assertEqual("passed", db.auction_sample_runs("2026-09-14")[0]["status"])
            decisions = score_auction_paths(db.auction_snapshots("2026-09-14"), self.settings)
            db.save_auction_decisions("2026-09-14", decisions)
            db.save_auction_outcomes(
                "2026-09-14", "09:30", "2026-09-14 09:30:02",
                [{"code": "600001", "auction_price": 10.52, "high": 10.55, "low": 10.45, "auction_amount": 70_000_000}],
            )
            db.save_auction_outcomes(
                "2026-09-14", "09:35", "2026-09-14 09:35:02",
                [{"code": "600001", "auction_price": 10.70, "high": 10.85, "low": 10.40, "auction_amount": 80_000_000}],
            )
            self.assertEqual({"09:30", "09:35"}, db.auction_outcome_labels("2026-09-14"))
            performance = db.auction_performance(30)
            self.assertEqual(1, performance["samples"])
            self.assertEqual(1, performance["positive_935"])

    def test_engine_lock_crosscheck_and_fresh_outcome_end_to_end(self):
        fixed_925 = datetime(2026, 9, 14, 9, 25, 1)
        path_rows = make_path("600001", "样本甲", "测试板块", [2.0, 2.2, 2.4, 2.5, 2.6, 2.7, 2.7]) + self._sector_peers()

        class FakeProvider:
            last_quality = {
                "started_at": "2026-09-14 09:25:01.000000", "finished_at": "2026-09-14 09:25:05.000000",
                "expected_total": 5000, "raw_count": 5000, "normalized_count": 4900,
                "coverage": 1.0, "page_span_seconds": 4.0,
            }

            def fetch_all(self):
                return [dict(row) for row in path_rows if row["sample_label"] == "09:25"]

        class FakeVerifier:
            stamp = "20260914092501"

            def fetch(self, codes):
                return {
                    code: {"price": 10.5, "pct": 2.7, "timestamp": self.stamp, "high": 10.5, "low": 10.5, "amount": 60_000_000}
                    for code in codes
                }

        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "engine.db")
            for label in REQUIRED_PATH[:-1]:
                db.save_auction_snapshot("2026-09-14", label, f"2026-09-14 {label}", [row for row in path_rows if row["sample_label"] == label])
                db.save_auction_sample_run({
                    "trading_day": "2026-09-14", "sample_label": label,
                    "target_at": f"2026-09-14 {label}", "started_at": f"2026-09-14 {label}",
                    "finished_at": f"2026-09-14 {label}", "expected_total": 5000,
                    "raw_count": 5000, "normalized_count": 4900, "coverage": 1,
                    "page_span_seconds": 4, "start_delay_seconds": 0, "status": "passed", "error": "",
                })
            clock = {"now": fixed_925}
            verifier = FakeVerifier()
            engine = AuctionEngine(db, queue.Queue(), provider=FakeProvider(), verifier=verifier, now_fn=lambda: clock["now"])
            engine._capture("09:25", fixed_925)
            locked = db.auction_decisions("2026-09-14")
            candidate = next(row for row in locked if row["code"] == "600001")
            self.assertEqual("试验观察", candidate["action"])
            self.assertFalse(candidate["can_order"])
            self.assertEqual("通过", candidate["crosscheck"])

            clock["now"] = datetime(2026, 9, 14, 9, 35, 5)
            verifier.stamp = "20260914092501"
            engine._capture_outcome("09:35", clock["now"])
            self.assertNotIn("09:35", db.auction_outcome_labels("2026-09-14"))
            verifier.stamp = "20260914093505"
            engine._capture_outcome("09:35", clock["now"])
            self.assertIn("09:35", db.auction_outcome_labels("2026-09-14"))


if __name__ == "__main__":
    unittest.main()
