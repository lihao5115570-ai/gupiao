from __future__ import annotations

import math
from datetime import date, datetime, time as clock_time
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


def _completed_weekly_bars(bars: list[Bar]) -> list[Bar]:
    """Exclude a still-forming weekly candle so core zones do not drift each day."""
    if not bars:
        return []
    try:
        last_day = datetime.strptime(bars[-1].timestamp[:10], "%Y-%m-%d").date()
    except ValueError:
        return bars[:-1] if len(bars) > 1 else bars
    today = date.today()
    same_week = last_day.isocalendar()[:2] == today.isocalendar()[:2]
    week_finished = today.weekday() > 4 or (today.weekday() == 4 and datetime.now().time() >= clock_time(15, 5))
    return bars if not same_week or week_finished else bars[:-1]


def _nearest_band(values: list[float], price: float, below: bool) -> tuple[float, float] | None:
    clean = sorted({round(value, 4) for value in values if value > 0 and (value < price if below else value > price)})
    if not clean:
        return None
    nearest = clean[-2:] if below else clean[:2]
    if len(nearest) == 1:
        anchor = nearest[0]
        return anchor * 0.97, anchor * 1.03
    low, high = nearest[0], nearest[-1]
    if high - low > price * 0.15:
        anchor = high if below else low
        return anchor * 0.96, anchor * 1.04
    return low, high


def _hybrid_trade_zones(
    price: float,
    daily_structure: dict[str, float | None],
    daily_bars: list[Bar],
    weekly_bars: list[Bar],
) -> dict[str, float | str | None] | None:
    completed = _completed_weekly_bars(weekly_bars)
    if len(completed) < 20:
        return None
    from .indicators import price_structure

    weekly_structure = price_structure(completed)
    weekly_averages = _moving_averages(completed)
    support_values = [
        weekly_structure.get("support"), weekly_structure.get("major_support"),
        weekly_structure.get("low_5"), weekly_structure.get("low_10"), weekly_structure.get("low_20"),
        weekly_averages.get(10), weekly_averages.get(20), weekly_averages.get(30),
    ]
    pressure_values = [
        weekly_structure.get("resistance"), weekly_structure.get("major_resistance"),
        weekly_structure.get("high_5"), weekly_structure.get("high_10"), weekly_structure.get("high_20"),
        weekly_structure.get("previous_high"), weekly_averages.get(10), weekly_averages.get(20),
    ]
    support_band = _nearest_band([float(value) for value in support_values if value], price, below=True)
    pressure_band = _nearest_band([float(value) for value in pressure_values if value], price, below=False)
    if not support_band or not pressure_band:
        return None

    step = _zone_step(price)
    support_low = _round_level(support_band[0], step)
    support_high = _round_level(min(support_band[1], price), step, upper=True)
    pressure_low = _round_level(max(pressure_band[0], price), step)
    pressure_high = _round_level(pressure_band[1], step, upper=True)
    daily_support = float(daily_structure.get("support") or support_high)
    daily_resistance = float(daily_structure.get("resistance") or pressure_low)
    play_low = _round_level(max(support_high, min(price, daily_support)), step)
    play_high = _round_level(min(pressure_low, max(price, daily_resistance)), step, upper=True)
    if play_low >= play_high:
        play_low, play_high = support_high, pressure_low

    higher_weekly = sorted({float(value) for value in pressure_values if value and float(value) > pressure_high})
    strong_low = _round_level(higher_weekly[0], step) if higher_weekly else None
    strong_high = _round_level(higher_weekly[1], step, upper=True) if len(higher_weekly) > 1 else strong_low
    weekly_date = completed[-1].timestamp[:10]
    daily_date = daily_bars[-1].timestamp[:10] if daily_bars else "--"
    ma_text = " / ".join(f"MA{period}周 {value:.2f}" for period, value in weekly_averages.items() if period in (10, 20, 30))
    return {
        "support_zone_low": support_low, "support_zone_high": support_high,
        "play_zone_low": play_low, "play_zone_high": play_high,
        "pressure_zone_low": pressure_low, "pressure_zone_high": pressure_high,
        "strong_pressure_low": strong_low, "strong_pressure_high": strong_high,
        "support_zone": format_zone(support_low, support_high),
        "play_zone": format_zone(play_low, play_high),
        "pressure_zone": format_zone(pressure_low, pressure_high),
        "strong_pressure_zone": format_zone(strong_low, strong_high),
        "zone_method": "多周期：已完成周K定核心，日K定博弈",
        "zone_basis": f"周线基准截至 {weekly_date}（{ma_text or '周线结构高低点'}）；日线执行参考截至 {daily_date}。本周未收盘周K不改变核心区。",
        "zone_weekly_date": weekly_date,
        "zone_daily_date": daily_date,
        "zone_timeframe": "weekly_daily_hybrid",
    }


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
        "zone_timeframe": "daily_fallback",
        "impulse_low": impulse_low,
        "impulse_high": impulse_high,
    }


def calculate_trade_zones(
    price: float,
    structure: dict[str, float | None],
    bars: list[Bar] | None = None,
    weekly_bars: list[Bar] | None = None,
) -> dict[str, float | str | None]:
    if bars and weekly_bars:
        hybrid = _hybrid_trade_zones(price, structure, bars, weekly_bars)
        if hybrid:
            return hybrid
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
        "zone_method": "日线降级：近期支撑压力",
        "zone_basis": "周K数据不足或暂时不可用，当前按日线近期高低点估算；不应视为稳定周线核心区。",
        "zone_timeframe": "daily_fallback",
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
