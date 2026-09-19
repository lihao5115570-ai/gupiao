from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(slots=True)
class Bar:
    timestamp: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float = 0.0
    pct_change: float = 0.0
    turnover: float = 0.0


@dataclass(slots=True)
class Quote:
    code: str
    name: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    pct_change: float
    timestamp: str


@dataclass(slots=True)
class Position:
    id: int | None
    code: str
    name: str
    buy_price: float
    buy_date: str = field(default_factory=lambda: date.today().isoformat())
    quantity: int = 0
    stop_loss: float | None = None
    target_return: float | None = None
    highest_price: float = 0.0
    last_price: float = 0.0
    risk_level: int = 1
    active: bool = True


@dataclass(slots=True)
class Analysis:
    code: str
    name: str
    price: float
    return_pct: float
    highest_profit_pct: float
    drawdown_pct: float
    risk_level: int
    reasons: list[str]
    events: list[str]
    trend: str
    boll_state: str
    macd_state: str
    kdj_state: str
    volume_state: str
    support: float | None
    major_support: float | None
    resistance: float | None
    major_resistance: float | None
    explanation: str
    indicators: dict[str, Any]
