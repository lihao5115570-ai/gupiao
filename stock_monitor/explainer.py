from __future__ import annotations

from .models import Analysis


class ExplanationService:
    """Event-gated explanation layer.

    The MVP uses deterministic local text. A remote model adapter can be injected
    later without changing the monitor or risk engine.
    """

    IMPORTANT_EVENTS = {
        "MACD死叉", "KDJ高位死叉", "情况B：放量跌破BOLL中轨",
        "情况C：突破前高后回落", "情况F：放量跌破重要支撑",
        "利润保护回撤提醒", "到达计划止损观察位",
    }

    def should_generate(self, analysis: Analysis, old_risk: int) -> bool:
        return old_risk != analysis.risk_level or bool(self.IMPORTANT_EVENTS.intersection(analysis.events))

    def explain(self, analysis: Analysis) -> str:
        return analysis.explanation
