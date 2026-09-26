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


def _average_true_range(bars: list[Bar], period: int = 14) -> float:
    """Size pressure bands from completed daily candles only."""
    completed = bars[:-1] if len(bars) > 1 else bars
    sample = completed[-(period + 1):]
    if len(sample) < 2:
        return 0.0
    ranges = []
    for previous, current in zip(sample, sample[1:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return fmean(ranges)


def _clustered_pivot_highs(bars: list[Bar], tolerance: float) -> list[float]:
    """Return volume-weighted clusters of confirmed local highs."""
    completed = bars[:-1] if len(bars) > 1 else bars
    window = completed[-60:]
    if len(window) < 5:
        return []
    average_volume = fmean(max(bar.volume, 0.0) for bar in window) or 1.0
    pivots: list[tuple[float, float]] = []
    for index in range(2, len(window) - 2):
        bar = window[index]
        neighbours = window[index - 2:index] + window[index + 1:index + 3]
        if bar.high >= max(item.high for item in neighbours):
            volume_weight = min(max(bar.volume / average_volume, 0.5), 3.0)
            recency_weight = 0.75 + 0.5 * index / max(len(window) - 1, 1)
            pivots.append((bar.high, volume_weight * recency_weight))
    clusters: list[list[tuple[float, float]]] = []
    for level, weight in sorted(pivots):
        if clusters:
            total_weight = sum(item[1] for item in clusters[-1])
            center = sum(item[0] * item[1] for item in clusters[-1]) / total_weight
            if abs(level - center) <= tolerance:
                clusters[-1].append((level, weight))
                continue
        clusters.append([(level, weight)])
    return [
        sum(level * weight for level, weight in cluster) / sum(weight for _, weight in cluster)
        for cluster in clusters
    ]


def _pressure_band(center: float, atr: float, multiplier: float = 1.0) -> tuple[float, float]:
    half_width = max(center * 0.0035, min(atr * 0.12, center * 0.008)) * multiplier
    return round(center - half_width, 2), round(center + half_width, 2)


def _three_pressure_bands(
    price: float,
    structure: dict[str, float | None],
    bars: list[Bar] | None,
    impulse: tuple[float, float] | None = None,
    extra_levels: list[float] | None = None,
) -> dict[str, float | str]:
    """Build three distinct resistance bands instead of overlapping lines and a broad zone."""
    atr = _average_true_range(bars) if bars else price * 0.025
    atr = atr or price * 0.025
    resistance = float(structure.get("resistance") or 0)
    major_resistance = float(structure.get("major_resistance") or 0)
    first_center = resistance if resistance > price * 0.995 else price + atr * 0.75
    minimum_gap = max(price * 0.012, atr * 0.35)

    pivot_levels = _clustered_pivot_highs(bars, max(price * 0.006, atr * 0.25)) if bars else []
    upper_levels = [major_resistance, *(extra_levels or []), *pivot_levels]
    if impulse:
        impulse_low, impulse_high = impulse
        upper_levels.extend((impulse_low + (impulse_high - impulse_low) * 0.86, impulse_high))
    upper_levels = sorted({float(level) for level in upper_levels if level and float(level) >= first_center + minimum_gap})

    if len(upper_levels) >= 2:
        second_center, strong_center = upper_levels[0], upper_levels[1]
    elif len(upper_levels) == 1:
        strong_center = upper_levels[0]
        if strong_center < first_center + minimum_gap * 2:
            strong_center = first_center + minimum_gap * 2
        second_center = (first_center + strong_center) / 2
    else:
        second_center = first_center + minimum_gap
        strong_center = first_center + minimum_gap * 2

    if impulse:
        target = impulse[0] + (impulse[1] - impulse[0]) * 0.86
        second_center = min(max(target, first_center + minimum_gap), impulse[1] - minimum_gap)
        strong_center = max(impulse[1], second_center + minimum_gap)

    first_low, first_high = _pressure_band(first_center, atr)
    second_low, second_high = _pressure_band(second_center, atr)
    strong_low, strong_high = _pressure_band(strong_center, atr, 1.35)
    if first_high >= second_low:
        boundary = round((first_center + second_center) / 2, 2)
        first_high, second_low = boundary, boundary
    if second_high >= strong_low:
        boundary = round((second_center + strong_center) / 2, 2)
        second_high, strong_low = boundary, boundary
    return {
        "pressure_zone_low": first_low,
        "pressure_zone_high": first_high,
        "pressure_zone": format_zone(first_low, first_high),
        "second_pressure_low": second_low,
        "second_pressure_high": second_high,
        "second_pressure_zone": format_zone(second_low, second_high),
        "strong_pressure_low": strong_low,
        "strong_pressure_high": strong_high,
        "strong_pressure_zone": format_zone(strong_low, strong_high),
        "pressure_atr": atr,
    }


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
    if not support_band:
        return None

    step = _zone_step(price)
    support_low = _round_level(support_band[0], step)
    support_high = _round_level(min(support_band[1], price), step, upper=True)
    pressure = _three_pressure_bands(
        price,
        daily_structure,
        daily_bars,
        extra_levels=[float(value) for value in pressure_values if value and float(value) > price],
    )
    daily_support = float(daily_structure.get("support") or support_high)
    pressure_low = float(pressure["pressure_zone_low"])
    daily_resistance = float(daily_structure.get("resistance") or pressure_low)
    play_low = _round_level(max(support_high, min(price, daily_support)), step)
    play_high = _round_level(min(pressure_low, max(price, daily_resistance)), step, upper=True)
    if play_low >= play_high:
        play_low, play_high = support_high, pressure_low

    weekly_date = completed[-1].timestamp[:10]
    daily_date = daily_bars[-1].timestamp[:10] if daily_bars else "--"
    ma_text = " / ".join(f"MA{period}周 {value:.2f}" for period, value in weekly_averages.items() if period in (10, 20, 30))
    return {
        "support_zone_low": support_low, "support_zone_high": support_high,
        "play_zone_low": play_low, "play_zone_high": play_high,
        "support_zone": format_zone(support_low, support_high),
        "play_zone": format_zone(play_low, play_high),
        **pressure,
        "zone_method": "三档压力：日K局部高点聚类 + 周K结构 + 成交量权重 + ATR宽度",
        "zone_basis": f"周线基准截至 {weekly_date}（{ma_text or '周线结构高低点'}）；日线执行参考截至 {daily_date}。第一压力取最近确认高点，第二和强压力结合周线结构；本周未收盘周K不参与计算。",
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


def _impulse_trade_zones(
    price: float,
    structure: dict[str, float | None],
    bars: list[Bar],
) -> dict[str, float | str | None] | None:
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
    pressure = _three_pressure_bands(price, structure, bars, impulse=impulse)

    averages = _moving_averages(bars)
    ma_text = " / ".join(f"MA{period} {value:.2f}" for period, value in averages.items())
    basis = (
        f"急拉波段低点 {impulse_low:.2f}，高点 {impulse_high:.2f}；"
        f"{ma_text}。三档压力由确认的局部高点、成交量权重和ATR波动宽度综合计算。"
    )
    return {
        "support_zone_low": support_low,
        "support_zone_high": support_high,
        "play_zone_low": play_low,
        "play_zone_high": play_high,
        "support_zone": format_zone(support_low, support_high),
        "play_zone": format_zone(play_low, play_high),
        **pressure,
        "zone_method": "三档压力：局部高点聚类 + 成交量权重 + ATR宽度",
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
        impulse_zones = _impulse_trade_zones(price, structure, bars)
        if impulse_zones:
            return impulse_zones

    support = structure.get("support")
    major_support = structure.get("major_support")
    resistance = structure.get("resistance")
    major_resistance = structure.get("major_resistance")

    support_low, support_high = _pad_if_flat(*_ordered_pair(major_support, support))
    pressure = _three_pressure_bands(price, structure, bars)

    play_low = support_high if support_high is not None else price * 0.985
    play_high = float(pressure["pressure_zone_low"])
    if play_low > play_high:
        play_low, play_high = _ordered_pair(support, resistance)
    play_low, play_high = _pad_if_flat(play_low, play_high, 0.01)

    return {
        "support_zone_low": support_low,
        "support_zone_high": support_high,
        "play_zone_low": play_low,
        "play_zone_high": play_high,
        "support_zone": format_zone(support_low, support_high),
        "play_zone": format_zone(play_low, play_high),
        **pressure,
        "zone_method": "日线三档压力：局部高点聚类 + 成交量权重 + ATR宽度",
        "zone_basis": "周K数据不足或暂时不可用；三档压力按已完成日K的局部高点、成交量权重和ATR波动宽度估算。",
        "zone_timeframe": "daily_fallback",
    }


def position_advice(price: float, risk_level: int, zones: dict[str, float | str | None], trend: str, macd_state: str, kdj_state: str) -> tuple[str, str]:
    support_high = zones.get("support_zone_high")
    pressure_low = zones.get("pressure_zone_low")
    pressure_high = zones.get("pressure_zone_high")

    weak = any(word in f"{trend}{macd_state}{kdj_state}" for word in ("下跌", "走弱", "死叉", "偏弱"))
    if risk_level >= 4:
        return "减仓观察", "风险等级较高，优先控制仓位；反弹不能收回关键位时，适合减仓或停止加仓观察。"
    if pressure_high and price > float(pressure_high):
        return "突破后观察", "价格已站到第一压力区上方，重点观察收盘能否守住区间上沿，并配合成交量确认；冲高回落时需要防假突破。"
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
    return "暂不加仓", "价格处于博弈区但动能偏弱，先等待指标修复或重新站上关键位。"

