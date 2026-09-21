from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stock_monitor.database import Database
from stock_monitor.indicators import calculate, price_structure
from stock_monitor.models import Bar, Position, Quote
from stock_monitor.data_provider import TencentProvider, secid_for
from stock_monitor.monitor import is_trading_session
from stock_monitor.risk_engine import evaluate
from stock_monitor.zones import calculate_trade_zones


def sample_bars(count: int = 80, falling: bool = False) -> list[Bar]:
    bars = []
    start = date(2026, 1, 1)
    for index in range(count):
        base = 20 + index * 0.08
        if falling and index > count - 5:
            base -= (index - (count - 5)) * 1.4
        volume = 1_000_000 if index < count - 1 else 2_000_000
        bars.append(Bar((start + timedelta(days=index)).isoformat(), base - 0.1, base, base + 0.25, base - 0.25, volume))
    return bars


class CoreTests(unittest.TestCase):
    def test_database_position_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            row_id = db.add_position(Position(None, "601606", "长城军工", 34.2, quantity=1000))
            positions = db.list_positions()
            self.assertEqual(row_id, positions[0].id)
            self.assertEqual("601606", positions[0].code)
            self.assertEqual(1000, positions[0].quantity)
            self.assertEqual(7, len({"stocks", "positions", "price_data", "indicators", "signals", "alerts", "settings"}))

    def test_indicators_and_structure(self):
        bars = sample_bars()
        ind = calculate(bars)
        structure = price_structure(bars)
        self.assertGreater(ind["boll_upper"], ind["boll_mid"])
        self.assertGreater(ind["boll_mid"], ind["boll_lower"])
        self.assertIn("support", structure)
        self.assertIn(ind["macd_cross"], ("无", "金叉", "死叉"))

    def test_trade_zones_are_derived_from_structure(self):
        structure = {
            "support": 35.4,
            "major_support": 34.8,
            "resistance": 37.2,
            "major_resistance": 38.1,
        }
        zones = calculate_trade_zones(36.0, structure)
        self.assertEqual("34.80 - 35.40", zones["support_zone"])
        self.assertEqual("35.40 - 37.20", zones["play_zone"])
        self.assertEqual("37.20 - 38.10", zones["pressure_zone"])

    def test_rapid_rally_uses_launch_platform_and_congestion_bands(self):
        bars = []
        start = date(2026, 7, 1)
        for index in range(45):
            close = 12.0 + index * 0.01
            bars.append(Bar((start + timedelta(days=index)).isoformat(), close, close, close + 0.2, close - 0.2, 100_000))
        rally = [
            (10.84, 11.63), (12.48, 12.79), (12.52, 14.07), (13.82, 15.48),
            (14.86, 17.03), (17.62, 18.73), (17.80, 19.71), (16.65, 18.18),
            (17.51, 19.58), (19.18, 21.54), (19.01, 22.37), (20.50, 23.18),
            (22.00, 23.36), (20.48, 22.30), (18.90, 19.74), (17.01, 18.26),
            (16.28, 17.15), (15.79, 16.53), (15.50, 15.98), (15.43, 16.04),
        ]
        for index, (low, high) in enumerate(rally, len(bars)):
            close = (low + high) / 2
            bars.append(Bar((start + timedelta(days=index)).isoformat(), close, close, high, low, 800_000))
        zones = calculate_trade_zones(15.74, price_structure(bars), bars)
        self.assertEqual("13.80 - 15.20", zones["support_zone"])
        self.assertEqual("15.50 - 18.30", zones["play_zone"])
        self.assertEqual("19.50 - 21.60", zones["pressure_zone"])
        self.assertEqual("23.00 - 23.40", zones["strong_pressure_zone"])
        self.assertIn("急拉结构", zones["zone_method"])

    def test_weekly_core_zones_and_daily_execution_are_separated(self):
        daily = sample_bars(80)
        weekly = []
        start = date(2024, 1, 5)
        for index in range(60):
            center = 18.0 + (index % 10) * 0.45
            weekly.append(Bar((start + timedelta(days=index * 7)).isoformat(), center, center + 0.1, center + 1.2, center - 1.0, 5_000_000))
        price = 21.0
        first = calculate_trade_zones(price, price_structure(daily), daily, weekly)
        changed_daily = list(daily)
        changed_daily[-1] = Bar(changed_daily[-1].timestamp, 24.0, 24.5, 25.0, 23.5, 3_000_000)
        second = calculate_trade_zones(price, price_structure(changed_daily), changed_daily, weekly)
        self.assertEqual("weekly_daily_hybrid", first["zone_timeframe"])
        self.assertIn("已完成周K定核心", first["zone_method"])
        self.assertEqual(first["support_zone"], second["support_zone"])
        self.assertEqual(first["pressure_zone"], second["pressure_zone"])

    def test_risk_output_is_advisory(self):
        bars = sample_bars(falling=True)
        ind = calculate(bars)
        structure = price_structure(bars)
        position = Position(1, "002134", "天津普林", 21.8, quantity=1000, highest_price=28)
        quote = Quote("002134", "天津普林", bars[-1].close, bars[-2].close, bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].volume, 0, -4, "2026-09-11 14:00:00")
        analysis = evaluate(position, quote, bars, ind, structure, {"profit_tier_1": "10", "drawdown_tier_1": "5", "profit_tier_2": "20", "drawdown_tier_2": "7", "profit_tier_3": "30", "drawdown_tier_3": "8"})
        self.assertIn(analysis.risk_level, range(1, 6))
        self.assertIn("不构成买卖建议", analysis.explanation)
        self.assertIn("利润保护观察状态", analysis.explanation)
        self.assertNotIn("马上卖出", analysis.explanation)

    def test_highest_price_starts_at_buy_date(self):
        bars = sample_bars()
        bars[10].high = 99
        position = Position(1, "002134", "天津普林", 24, buy_date=bars[50].timestamp, quantity=100)
        quote = Quote("002134", "天津普林", bars[-1].close, bars[-2].close, bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].volume, 0, 1, "2026-09-11 14:00:00")
        analysis = evaluate(position, quote, bars, calculate(bars), price_structure(bars), {})
        self.assertLess(analysis.indicators["highest_price"], 99)

    def test_trading_session(self):
        from datetime import datetime
        self.assertTrue(is_trading_session(datetime(2026, 9, 11, 10, 0)))
        self.assertFalse(is_trading_session(datetime(2026, 9, 12, 10, 0)))

    def test_market_symbol_mapping(self):
        self.assertEqual("1.601606", secid_for("601606"))
        self.assertEqual("sz002134", TencentProvider.symbol("002134"))


if __name__ == "__main__":
    unittest.main()
