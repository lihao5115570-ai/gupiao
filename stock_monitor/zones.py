from __future__ import annotations

import math
from statistics import fmean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Bar


def _ordered_pair(a: float | None, b: float | None) -> tuple[float | None, float | None]:
    if a is None and b is None:
        return None, None
    if a is None:
        return b, b
    if b is None:
        return a, a
    return (a, b) if a <= b else (b, a)


def _pad_if_flat(low: float | None, high: float | None, pct: float = 0.006) -> tuple[float | None, float | None]:
    if low is None or high is None or abs(high - low) > 1e-9:
        return low, high
    pad = max(low * pct, 0.01)
    return low - pad, high + pad


def format_zone(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "--"
    return f"{low:.2f} - {high:.2f}"


def _zone_step(price: float) -> float:
    if price >= 10:
        return 0.1
    if price >= 3:
        return 0.05
    return 0.01


def _round_level(value: float, step: float, upper: bool = False) -> float:
    scaled = value / step
    rounded = math.floor(scaled + 1e-9) if upper else math.floor(scaled + 0.5)
    return round(rounded * step, 2)


def _moving_averages(bars: list[Bar]) -> dict[int, float]:
    closes = [bar.close for bar in bars]
    return {period: fmean(closes[-period:]) for period in (5, 10, 20, 30, 60) if len(closes) >= period}


def _strong_impulse(bars: list[Bar], price: float) -> tuple[float, float] | None:
    """Return the strongest recent low-to-later-high leg when it still shapes today's chart."""
    window = bars[-90:]
    if len(window) < 30:
        return None
    recent_start = max(1, len(window) - 35)
    best_peak_index = max(range(recent_start, len(window)), key=lambda index: window[index].high)
    best_high = window[best_peak_index].high
    launch_window = window[max(0, best_peak_index - 20):best_peak_index + 1]
    best_low = min(bar.low for bar in launch_window)
    best_gain = best_high / best_low - 1 if best_low > 0 else 0
    span = best_high - best_low
    if best_gain < 0.35 or best_peak_index < len(window) - 35 or span <= 0:
        return None
    if price < best_low + span * 0.18:
        return None
    return best_low, best_high


def _impulse_trade_zones(price: float, bars: list[Bar]) -> dict[str, float | str | None] | None:
    impulse = _strong_impulse(bars, price)
    if not impulse:
        return None
    impulse_low, impulse_high = impulse
    span = impulse_high - impulse_low
    step = _zone_step(price)

    # The bands describe the four visible structures of a rapid mark-up cycle:
    # launch platform, long/short contest, trapped-volume pressure and prior peak.
    support_low = _round_level(impulse_low + span * 0.24, step)
    support_high = _round_level(impulse_low + span * 0.35, step, upper=True)
    play_low = _round_level(impulse_low + span * 0.37, step)
    play_high = _round_level(impulse_low + span * 0.60, step, upper=True)
    pressure_low = _round_level(impulse_low + span * 0.69, step)
    pressure_high = _round_level(impulse_low + span * 0.86, step, upper=True)
    strong_low = _round_level(impulse_low + span * 0.97, step)
    strong_high = _round_level(impulse_high, step)

    averages = _moving_averages(bars)
    ma_text = " / ".join(f"MA{period} {value:.2f}" for period, value in averages.items())
    basis = (
        f"急拉波段低点 {impulse_low:.2f}，高点 {impulse_high:.2f}；"
        f"{ma_text}。区间由均线骨架、启动平台和拉升成交密集带综合估算。"
    )
    return {
        "support_zone_low": support_low,
        "support_zone_high": support_high,
        "play_zone_low": play_low,
        "play_zone_high": play_high,
        "pressure_zone_low": pressure_low,
        "pressure_zone_high": pressure_high,
        "strong_pressure_low": strong_low,
        "strong_pressure_high": strong_high,
        "support_zone": format_zone(support_low, support_high),
        "play_zone": format_zone(play_low, play_high),
        "pressure_zone": format_zone(pressure_low, pressure_high),
        "strong_pressure_zone": format_zone(strong_low, strong_high),
        "zone_method": "急拉结构：均线 + 启动平台 + 成交密集区",
        "zone_basis": basis,
        "impulse_low": impulse_low,
        "impulse_high": impulse_high,
    }


def calculate_trade_zones(
    price: float,
    structure: dict[str, float | None],
    bars: list[Bar] | None = None,
) -> dict[str, float | str | None]:
    if bars:
        impulse_zones = _impulse_trade_zones(price, bars)
        if impulse_zones:
            return impulse_zones

    support = structure.get("support")
    major_support = structure.get("major_support")
    resistance = structure.get("resistance")
    major_resistance = structure.get("major_resistance")

    support_low, support_high = _pad_if_flat(*_ordered_pair(major_support, support))
    pressure_low, pressure_high = _pad_if_flat(*_ordered_pair(resistance, major_resistance))

    play_low = support_high if support_high is not None else price * 0.985
    play_high = pressure_low if pressure_low is not None else price * 1.015
    if play_low > play_high:
        play_low, play_high = _ordered_pair(support, resistance)
    play_low, play_high = _pad_if_flat(play_low, play_high, 0.01)

    return {
        "support_zone_low": support_low,
        "support_zone_high": support_high,
        "play_zone_low": play_low,
        "play_zone_high": play_high,
        "pressure_zone_low": pressure_low,
        "pressure_zone_high": pressure_high,
        "strong_pressure_low": None,
        "strong_pressure_high": None,
        "support_zone": format_zone(support_low, support_high),
        "play_zone": format_zone(play_low, play_high),
        "pressure_zone": format_zone(pressure_low, pressure_high),
        "strong_pressure_zone": "--",
        "zone_method": "常规结构：近期支撑压力",
        "zone_basis": "未检测到近期强急拉波段，按近期高低点和支撑压力估算。",
    }


def position_advice(price: float, risk_level: int, zones: dict[str, float | str | None], trend: str, macd_state: str, kdj_state: str) -> tuple[str, str]:
    support_high = zones.get("support_zone_high")
    pressure_low = zones.get("pressure_zone_low")
    pressure_high = zones.get("pressure_zone_high")

    weak = any(word in f"{trend}{macd_state}{kdj_state}" for word in ("下跌", "走弱", "死叉", "偏弱"))
    if risk_level >= 4:
        return "减仓观察", "风险等级较高，优先控制仓位；反弹不能收回关键位时，适合减仓或停止加仓观察。"
    if pressure_low and price >= float(pressure_low):
        if risk_level >= 3 or weak:
            return "压力区减仓观察", "价格已接近或进入压力区，且动能不够强，适合逢高减仓观察，不适合追高加仓。"
        return "压力区不追高", "价格接近压力区，已有仓位可继续观察，新增仓位建议等待突破确认或回踩。"
    if support_high and price <= float(support_high):
        if risk_level <= 2 and not weak:
            return "支撑区小仓观察", "价格接近支撑区且风险不高，可按自己的计划小仓观察，跌破支撑应及时收缩仓位。"
        return "支撑区谨慎观察", "价格在支撑区附近但技术状态偏弱，先看能否止跌，不建议盲目加仓。"
    if risk_level <= 2 and not weak:
        return "持仓观察", "价格位于博弈区且风险较低，已有仓位可继续观察，等待向压力区或支撑区靠近。"
    if pressure_high and price > float(pressure_high):
        return "突破后观察", "价格强于主要压力区，重点观察突破是否有效；放量回落时需要防假突破。"
    return "暂不加仓", "价格处于博弈区但动能偏弱，先等待指标修复或重新站上关键位。"
