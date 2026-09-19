from __future__ import annotations

import math
from statistics import fmean, pstdev

from .models import Bar


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def boll(closes: list[float], period: int = 20, width: float = 2.0) -> dict[str, list[float | None]]:
    mid: list[float | None] = []
    upper: list[float | None] = []
    lower: list[float | None] = []
    for index in range(len(closes)):
        if index + 1 < period:
            mid.append(None); upper.append(None); lower.append(None)
            continue
        window = closes[index + 1 - period:index + 1]
        mean = fmean(window)
        std = pstdev(window)
        mid.append(mean); upper.append(mean + width * std); lower.append(mean - width * std)
    return {"mid": mid, "upper": upper, "lower": lower}


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[float]]:
    fast_line = ema(closes, fast)
    slow_line = ema(closes, slow)
    dif = [a - b for a, b in zip(fast_line, slow_line)]
    dea = ema(dif, signal)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


def kdj(bars: list[Bar], period: int = 9) -> dict[str, list[float]]:
    k_values: list[float] = []
    d_values: list[float] = []
    j_values: list[float] = []
    k = d = 50.0
    for index, bar in enumerate(bars):
        window = bars[max(0, index + 1 - period):index + 1]
        low = min(item.low for item in window)
        high = max(item.high for item in window)
        rsv = 50.0 if math.isclose(high, low) else (bar.close - low) / (high - low) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        j = 3 * k - 2 * d
        k_values.append(k); d_values.append(d); j_values.append(j)
    return {"k": k_values, "d": d_values, "j": j_values}


def calculate(bars: list[Bar]) -> dict[str, object]:
    if len(bars) < 30:
        raise ValueError("至少需要30根K线计算指标")
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    b = boll(closes)
    m = macd(closes)
    k = kdj(bars)
    last, previous = bars[-1], bars[-2]
    volume_ma5 = fmean(volumes[-5:])
    volume_ma10 = fmean(volumes[-10:])
    mid = float(b["mid"][-1])
    upper = float(b["upper"][-1])
    lower = float(b["lower"][-1])
    previous_mid = float(b["mid"][-2])
    previous_width = float(b["upper"][-2]) - float(b["lower"][-2])
    width_now = upper - lower
    macd_cross = "无"
    if m["dif"][-2] <= m["dea"][-2] and m["dif"][-1] > m["dea"][-1]:
        macd_cross = "金叉"
    elif m["dif"][-2] >= m["dea"][-2] and m["dif"][-1] < m["dea"][-1]:
        macd_cross = "死叉"
    kdj_cross = "无"
    if k["k"][-2] <= k["d"][-2] and k["k"][-1] > k["d"][-1]:
        kdj_cross = "金叉"
    elif k["k"][-2] >= k["d"][-2] and k["k"][-1] < k["d"][-1]:
        kdj_cross = "死叉"
    return {
        "close": last.close, "previous_close": previous.close,
        "boll_mid": mid, "boll_upper": upper, "boll_lower": lower,
        "boll_mid_slope": mid - previous_mid, "boll_width_change": width_now - previous_width,
        "macd_dif": m["dif"][-1], "macd_dea": m["dea"][-1], "macd_hist": m["hist"][-1],
        "macd_hist_prev": m["hist"][-2], "macd_cross": macd_cross,
        "kdj_k": k["k"][-1], "kdj_d": k["d"][-1], "kdj_j": k["j"][-1],
        "kdj_j_prev": k["j"][-2], "kdj_cross": kdj_cross,
        "volume": last.volume, "volume_ma5": volume_ma5, "volume_ma10": volume_ma10,
        "volume_ratio": last.volume / volume_ma5 if volume_ma5 else 0,
        "price_change": last.close - previous.close,
    }


def price_structure(bars: list[Bar]) -> dict[str, float | None]:
    if len(bars) < 20:
        raise ValueError("至少需要20根K线分析价格结构")
    completed = bars[:-1] if len(bars) > 1 else bars
    result: dict[str, float | None] = {}
    for period in (5, 10, 20):
        window = completed[-period:]
        result[f"high_{period}"] = max(item.high for item in window)
        result[f"low_{period}"] = min(item.low for item in window)
    lows = sorted({round(item.low, 2) for item in completed[-60:]}, reverse=True)
    highs = sorted({round(item.high, 2) for item in completed[-60:]})
    price = bars[-1].close
    below = [value for value in lows if value < price]
    above = [value for value in highs if value > price]
    result["support"] = below[0] if below else result["low_5"]
    result["major_support"] = float(result["low_20"])
    result["resistance"] = above[0] if above else result["high_5"]
    result["major_resistance"] = float(result["high_20"])
    older = completed[:-5][-20:] if len(completed) > 5 else completed
    result["previous_high"] = max(item.high for item in older)
    result["previous_low"] = min(item.low for item in older)
    return result
