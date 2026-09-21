from __future__ import annotations

from .models import Analysis, Bar, Position, Quote
from .zones import calculate_trade_zones, position_advice


def _state_labels(ind: dict[str, object]) -> tuple[str, str, str, str, str]:
    close = float(ind["close"])
    mid = float(ind["boll_mid"])
    upper = float(ind["boll_upper"])
    lower = float(ind["boll_lower"])
    trend = "上涨" if close > mid and float(ind["boll_mid_slope"]) > 0 else "下跌" if close < mid and float(ind["boll_mid_slope"]) < 0 else "震荡"
    boll_state = "偏强" if close > mid else "偏弱" if close < mid else "正常"
    hist = float(ind["macd_hist"])
    hist_prev = float(ind["macd_hist_prev"])
    macd_state = "增强" if hist > hist_prev and hist > 0 else "走弱" if hist < hist_prev else "正常"
    j = float(ind["kdj_j"])
    kdj_state = "高位死叉" if j > 75 and ind["kdj_cross"] == "死叉" else "高位" if j > 80 else "低位" if j < 20 else "正常"
    ratio = float(ind["volume_ratio"])
    volume_state = "放量" if ratio >= 1.5 else "缩量" if ratio <= 0.65 else "正常"
    return trend, boll_state, macd_state, kdj_state, volume_state


def evaluate(
    position: Position,
    quote: Quote,
    daily: list[Bar],
    daily_ind: dict[str, object],
    structure: dict[str, float | None],
    settings: dict[str, str],
    intraday: dict[str, dict[str, object]] | None = None,
    weekly: list[Bar] | None = None,
) -> Analysis:
    price = quote.price or daily[-1].close
    held_bars = [bar for bar in daily if bar.timestamp[:10] >= position.buy_date]
    held_high = max((bar.high for bar in held_bars), default=price)
    highest = max(position.highest_price, position.buy_price, price, held_high)
    return_pct = (price / position.buy_price - 1) * 100
    highest_profit_pct = (highest / position.buy_price - 1) * 100
    drawdown_pct = (highest - price) / highest * 100 if highest else 0
    trend, boll_state, macd_state, kdj_state, volume_state = _state_labels(daily_ind)
    zones = calculate_trade_zones(price, structure, daily, weekly)
    score = 0
    reasons: list[str] = []
    events: list[str] = []

    close = float(daily_ind["close"])
    previous_close = float(daily_ind["previous_close"])
    mid = float(daily_ind["boll_mid"])
    upper = float(daily_ind["boll_upper"])
    lower = float(daily_ind["boll_lower"])
    hist = float(daily_ind["macd_hist"])
    hist_prev = float(daily_ind["macd_hist_prev"])
    j = float(daily_ind["kdj_j"])
    ratio = float(daily_ind["volume_ratio"])
    kdj_dead = daily_ind["kdj_cross"] == "死叉"
    macd_weak = hist < hist_prev

    near_upper = upper > lower and (upper - close) / (upper - lower) <= 0.15
    stalled = close <= max(b.high for b in daily[-6:-1]) and ratio >= 1.35
    if near_upper and j >= 80 and macd_weak and stalled:
        score += 3; reasons.append("高位滞涨迹象，短线风险增加"); events.append("情况A：上轨附近多指标转弱")
    broke_mid = previous_close >= mid and close < mid
    if broke_mid and macd_weak and kdj_dead and ratio >= 1.25:
        score += 4; reasons.append("趋势转弱，需要重点检查持仓风险"); events.append("情况B：放量跌破BOLL中轨")
    previous_high = float(structure["previous_high"] or 0)
    recent_broke_high = max(b.high for b in daily[-4:-1]) > previous_high if len(daily) >= 5 else False
    if recent_broke_high and close < previous_high and ratio >= 1.25:
        score += 3; reasons.append("存在假突破风险"); events.append("情况C：突破前高后回落")
    prior_segment = daily[-40:-20] if len(daily) >= 40 else daily[:-10]
    if prior_segment:
        prior_price_high = max(b.high for b in prior_segment)
        if close > prior_price_high and hist < max(0.0, hist_prev):
            score += 2; reasons.append("价格创新高但动能未同步，存在顶背离迹象"); events.append("情况D：顶背离迹象")
    major_support = float(zones.get("support_zone_low") or structure["major_support"] or 0)
    if major_support and close < major_support and ratio >= 1.25:
        score += 4; reasons.append(f"重要支撑 {major_support:.2f} 失守，风险等级提高"); events.append("情况F：放量跌破重要支撑")
    if kdj_dead and j >= 70:
        score += 1; reasons.append("KDJ高位死叉")
        events.append("KDJ高位死叉")
    if daily_ind["macd_cross"] == "死叉":
        score += 1; reasons.append("MACD出现死叉")
        events.append("MACD死叉")
    if close < lower:
        score += 2; reasons.append("价格跌破BOLL下轨")
    if position.stop_loss and price <= position.stop_loss:
        score += 4; reasons.append(f"价格已到达计划止损观察位 {position.stop_loss:.2f}")
        events.append("到达计划止损观察位")

    tiers = [
        (float(settings.get("profit_tier_3", 30)), float(settings.get("drawdown_tier_3", 8)), 4),
        (float(settings.get("profit_tier_2", 20)), float(settings.get("drawdown_tier_2", 7)), 3),
        (float(settings.get("profit_tier_1", 10)), float(settings.get("drawdown_tier_1", 5)), 2),
    ]
    for profit_threshold, drawdown_threshold, points in tiers:
        if highest_profit_pct >= profit_threshold and drawdown_pct >= drawdown_threshold:
            score += points
            reasons.append(f"最高浮盈达到 {highest_profit_pct:.1f}%，已从高点回撤 {drawdown_pct:.1f}%")
            events.append("利润保护回撤提醒")
            break

    if intraday:
        weak_frames = sum(1 for item in intraday.values() if float(item.get("macd_hist", 0)) < float(item.get("macd_hist_prev", 0)))
        if weak_frames >= 2:
            score += 1; reasons.append("60分钟与15分钟动能同步走弱")

    risk_level = 1 if score == 0 else 2 if score <= 2 else 3 if score <= 4 else 4 if score <= 7 else 5
    if not reasons:
        reasons.append("当前未触发明显的联合风险条件")
    support = structure.get("support")
    resistance = structure.get("resistance")
    advice_title, advice_text = position_advice(price, risk_level, zones, trend, macd_state, kdj_state)
    guard_start = float(settings.get("profit_guard_start", 5))
    profit_guard_active = highest_profit_pct >= guard_start
    guard_text = "已进入利润保护观察状态。" if profit_guard_active else "尚未进入利润保护观察状态。"
    strong_pressure_text = (
        f"；强压力 {zones['strong_pressure_zone']}"
        if zones.get("strong_pressure_zone") != "--" else ""
    )
    explanation = (
        f"当前趋势为{trend}，股价{'位于' if close >= mid else '跌至'}BOLL中轨{'上方' if close >= mid else '下方'}。"
        f"MACD状态{macd_state}，KDJ处于{kdj_state}，成交量{volume_state}。{guard_text}\n\n"
        f"当前风险等级：{risk_level}/5。主要依据：{'；'.join(reasons[:4])}。\n\n"
        f"重点观察：{f'{resistance:.2f} 压力' if resistance else '上方压力'}；"
        f"{f'{support:.2f} 支撑' if support else '下方支撑'}。本提示仅用于持仓风险观察，不构成买卖建议。"
        f"\n\n自动区间：支撑区 {zones['support_zone']}；博弈区 {zones['play_zone']}；压力区 {zones['pressure_zone']}"
        f"{strong_pressure_text}。"
        f"\n计算方法：{zones['zone_method']}。{zones['zone_basis']}"
        f"\n\n仓位观察建议：{advice_title}。{advice_text}"
    )
    merged = dict(daily_ind)
    merged.update({"highest_price": highest, "return_pct": return_pct, "highest_profit_pct": highest_profit_pct, "drawdown_pct": drawdown_pct, "profit_guard_active": profit_guard_active})
    merged.update(zones)
    merged.update({"position_advice": advice_title, "position_advice_detail": advice_text})
    return Analysis(
        code=position.code, name=position.name, price=price, return_pct=return_pct,
        highest_profit_pct=highest_profit_pct, drawdown_pct=drawdown_pct,
        risk_level=risk_level, reasons=reasons, events=list(dict.fromkeys(events)),
        trend=trend, boll_state=boll_state, macd_state=macd_state, kdj_state=kdj_state,
        volume_state=volume_state, support=support, major_support=zones.get("support_zone_low") or structure.get("major_support"),
        resistance=resistance, major_resistance=zones.get("pressure_zone_high") or structure.get("major_resistance"),
        explanation=explanation, indicators=merged,
    )
