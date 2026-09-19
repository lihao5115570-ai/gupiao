from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .auction import REQUIRED_PATH
from .database import Database


@dataclass(slots=True)
class BacktestMetrics:
    trading_days: int = 0
    signals: int = 0
    no_break_rate: float = 0.0
    close_win_rate: float = 0.0
    average_mae: float = 0.0
    maximum_mae: float = 0.0
    mae_over_2_rate: float = 0.0
    consecutive_failures: int = 0


PROFILE_KEYS = (
    "auction_min_amount", "auction_high_price", "auction_min_retention", "auction_late_drop",
    "auction_max_drawdown", "auction_min_float_amount_ratio", "auction_min_market_positive_ratio",
    "auction_max_orderable", "auction_max_per_sector", "auction_include_chinext",
    "auction_include_star", "auction_include_bse", "auction_include_st", "auction_include_high_price",
    "auction_pool_enabled", "auction_pool_max_size", "auction_pool_max_per_sector",
    "auction_pool_min_price", "auction_preferred_min_price", "auction_preferred_max_price",
    "auction_pool_min_pct", "auction_pool_max_pct", "auction_pool_min_amount",
)


def profile_id(settings: dict[str, str]) -> str:
    payload = {key: str(settings.get(key, "")) for key in PROFILE_KEYS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def stamp_profile(rows: list[dict[str, Any]], settings: dict[str, str]) -> list[dict[str, Any]]:
    fingerprint = profile_id(settings)
    parameters = {key: str(settings.get(key, "")) for key in PROFILE_KEYS}
    for row in rows:
        row["profile_id"] = fingerprint
        row["profile_parameters"] = parameters
    return rows


def summarize_signals(signals: list[dict[str, Any]]) -> BacktestMetrics:
    if not signals:
        return BacktestMetrics()
    maes = [float(row["mae"]) for row in signals]
    failures = longest = 0
    for row in sorted(signals, key=lambda item: (item["trading_day"], item["code"])):
        failed = not row["no_break"] or not row["close_win"]
        failures = failures + 1 if failed else 0
        longest = max(longest, failures)
    return BacktestMetrics(
        trading_days=len({row["trading_day"] for row in signals}),
        signals=len(signals),
        no_break_rate=sum(bool(row["no_break"]) for row in signals) / len(signals),
        close_win_rate=sum(bool(row["close_win"]) for row in signals) / len(signals),
        average_mae=sum(maes) / len(maes),
        maximum_mae=max(maes),
        mae_over_2_rate=sum(value > 2.0 for value in maes) / len(maes),
        consecutive_failures=longest,
    )


def meets_targets(metrics: BacktestMetrics) -> bool:
    return (
        metrics.no_break_rate >= 0.80
        and metrics.close_win_rate >= 0.70
        and metrics.average_mae <= 0.50
        and metrics.mae_over_2_rate <= 0.05
    )


class AuctionBacktester:
    def __init__(self, database: Database):
        self.db = database

    def _formal_day(self, day: str) -> bool:
        runs = {str(row["sample_label"]): row for row in self.db.auction_sample_runs(day)}
        return all(
            label in runs and runs[label]["status"] == "passed" and runs[label]["source_kind"] == "live"
            for label in REQUIRED_PATH
        )

    def signals(self, settings: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
        formal: list[dict[str, Any]] = []
        formal_days: list[str] = []
        excluded_days = excluded_signals = 0
        fingerprint = profile_id(settings)
        for day in self.db.auction_days():
            if not self._formal_day(day):
                excluded_days += 1
                continue
            decisions = self.db.auction_decisions(day)
            profile_rows = [row for row in decisions if row.get("profile_id") == fingerprint]
            if not profile_rows:
                excluded_days += 1
                continue
            formal_days.append(day)
            close_rows = {
                str(row["code"]): row for row in self.db.auction_outcomes(day)
                if row["observation_label"] == "15:00" and row["source_kind"] == "live"
            }
            candidates = [
                row for row in profile_rows if row.get("screen_action", row.get("action")) == "可挂入"
            ]
            for row in candidates:
                outcome = close_rows.get(str(row["code"]))
                entry = float(row.get("lock_price") or 0)
                if not outcome or entry <= 0 or float(outcome["low"]) <= 0 or float(outcome["price"]) <= 0:
                    excluded_signals += 1
                    continue
                low = float(outcome["low"])
                high = float(outcome["high"])
                close = float(outcome["price"])
                formal.append({
                    "trading_day": day, "code": str(row["code"]), "entry": entry,
                    "low": low, "high": high, "close": close,
                    "mae": max(0.0, (entry - low) / entry * 100),
                    "no_break": low >= entry, "close_win": close > entry,
                })
        return formal, formal_days, {"excluded_days": excluded_days, "excluded_signals": excluded_signals}

    def run(self, settings: dict[str, str]) -> dict[str, Any]:
        signals, formal_days, excluded = self.signals(settings)
        days = sorted(set(formal_days))

        def in_last(day_count: int) -> list[dict[str, Any]]:
            selected = set(days[-day_count:])
            return [row for row in signals if row["trading_day"] in selected]

        holdout_days = set(days[-10:])
        holdout = [row for row in signals if row["trading_day"] in holdout_days]
        training = [row for row in signals if row["trading_day"] not in holdout_days]
        def metrics(rows: list[dict[str, Any]], window_days: set[str]) -> dict[str, Any]:
            value = asdict(summarize_signals(rows))
            value["trading_days"] = len(window_days)
            return value

        overall_metrics = summarize_signals(signals)
        overall_days = set(days)
        holdout_metrics = summarize_signals(holdout)
        overall_metrics.trading_days = len(overall_days)
        holdout_metrics.trading_days = len(holdout_days)
        qualified = (
            overall_metrics.trading_days >= 30
            and overall_metrics.signals >= 50
            and holdout_metrics.trading_days >= 10
            and meets_targets(overall_metrics)
            and meets_targets(holdout_metrics)
        )
        return {
            "profile": settings.get("auction_profile_name", "保守试验版"),
            "qualified": qualified,
            "overall": metrics(signals, overall_days),
            "rolling_20": metrics(in_last(20), set(days[-20:])),
            "rolling_60": metrics(in_last(60), set(days[-60:])),
            "training": metrics(training, set(days[:-10])),
            "holdout_10": metrics(holdout, holdout_days),
            **excluded,
        }


def apply_backtest_gate(rows: list[dict[str, Any]], report: dict[str, Any]) -> list[dict[str, Any]]:
    qualified = bool(report.get("qualified"))
    for row in rows:
        row["screen_action"] = row.get("screen_action", row.get("action"))
        row["backtest_profile"] = report.get("profile", "保守试验版")
        row["backtest_qualified"] = qualified
        if row.get("action") == "可挂入":
            row["action"] = "竞价小仓" if qualified else "试验观察"
            row["can_order"] = qualified
            if not qualified:
                row["suggested_order"] = "仅记录，不作竞价买入结论"
                row["no_buy_condition"] = "回测和最近10日样本外验证尚未达标"
        elif row.get("action") == "等回踩":
            row["action"] = "等待9:35确认"
            row["can_order"] = False
    order = {"竞价小仓": 0, "试验观察": 1, "等待9:35确认": 2, "放弃": 3}
    rows.sort(key=lambda row: (order.get(str(row.get("action")), 9), -float(row.get("score") or 0)))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    return rows
