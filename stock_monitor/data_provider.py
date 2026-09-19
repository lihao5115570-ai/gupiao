from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

from .models import Bar, Quote


class MarketDataError(RuntimeError):
    pass


def secid_for(code: str) -> str:
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError("A股代码必须是6位数字")
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market}.{code}"


class EastMoneyProvider:
    """Small stdlib-only adapter for Eastmoney's public quote endpoints."""

    QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
    KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    def _get_json(self, url: str, params: dict[str, str]) -> dict:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{url}?{query}",
            headers={"User-Agent": "Mozilla/5.0 StockMonitor/0.1", "Referer": "https://quote.eastmoney.com/"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise MarketDataError(f"行情请求失败：{exc}") from exc

    @staticmethod
    def _price(value: object) -> float:
        if value in (None, "-", ""):
            return 0.0
        return float(value) / 100.0

    def quote(self, code: str) -> Quote:
        payload = self._get_json(
            self.QUOTE_URL,
            {
                "secid": secid_for(code),
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170",
                "_": str(int(time.time() * 1000)),
            },
        )
        data = payload.get("data")
        if not data:
            raise MarketDataError(f"未找到股票 {code} 的实时行情")
        return Quote(
            code=str(data.get("f57") or code), name=str(data.get("f58") or code),
            price=self._price(data.get("f43")), pre_close=self._price(data.get("f60")),
            open=self._price(data.get("f46")), high=self._price(data.get("f44")),
            low=self._price(data.get("f45")), volume=float(data.get("f47") or 0),
            amount=float(data.get("f48") or 0), pct_change=self._price(data.get("f170")),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def klines(self, code: str, timeframe: str = "daily", limit: int = 180) -> list[Bar]:
        klt = {"daily": "101", "60m": "60", "15m": "15"}.get(timeframe)
        if not klt:
            raise ValueError(f"不支持的周期：{timeframe}")
        payload = self._get_json(
            self.KLINE_URL,
            {
                "secid": secid_for(code), "klt": klt, "fqt": "1", "lmt": str(limit),
                "end": "20500101", "iscca": "1", "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
        )
        data = payload.get("data") or {}
        raw = data.get("klines") or []
        bars: list[Bar] = []
        for item in raw:
            parts = item.split(",")
            if len(parts) < 7:
                continue
            bars.append(
                Bar(
                    timestamp=parts[0], open=float(parts[1]), close=float(parts[2]),
                    high=float(parts[3]), low=float(parts[4]), volume=float(parts[5]),
                    amount=float(parts[6]), pct_change=float(parts[8] or 0) if len(parts) > 8 else 0,
                    turnover=float(parts[10] or 0) if len(parts) > 10 else 0,
                )
            )
        if not bars:
            raise MarketDataError(f"未找到股票 {code} 的{timeframe} K线")
        return bars


class TencentProvider:
    QUOTE_URL = "https://qt.gtimg.cn/q="
    DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    @staticmethod
    def symbol(code: str) -> str:
        secid_for(code)
        return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code

    def _read(self, url: str, encoding: str = "utf-8") -> str:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 StockMonitor/0.1", "Referer": "https://gu.qq.com/"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode(encoding, errors="replace")
        except Exception as exc:
            raise MarketDataError(f"腾讯行情请求失败：{exc}") from exc

    def quote(self, code: str) -> Quote:
        symbol = self.symbol(code)
        text = self._read(self.QUOTE_URL + symbol, "gb18030")
        matched = re.search(r'="(.*)";', text)
        if not matched:
            raise MarketDataError(f"未找到股票 {code} 的腾讯实时行情")
        fields = matched.group(1).split("~")
        if len(fields) < 38 or not fields[3]:
            raise MarketDataError(f"股票 {code} 的腾讯行情字段不完整")
        stamp = fields[30]
        timestamp = datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S") if len(stamp) == 14 else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return Quote(
            code=fields[2], name=fields[1], price=float(fields[3]), pre_close=float(fields[4] or 0),
            open=float(fields[5] or 0), high=float(fields[33] or 0), low=float(fields[34] or 0),
            volume=float(fields[6] or 0), amount=float(fields[37] or 0) * 10_000,
            pct_change=float(fields[32] or 0), timestamp=timestamp,
        )

    def klines(self, code: str, timeframe: str = "daily", limit: int = 180) -> list[Bar]:
        symbol = self.symbol(code)
        if timeframe == "daily":
            params = urllib.parse.urlencode({"param": f"{symbol},day,,,{limit},qfq"})
            payload = json.loads(self._read(f"{self.DAILY_URL}?{params}"))
            node = (payload.get("data") or {}).get(symbol) or {}
            raw = node.get("qfqday") or node.get("day") or []
        elif timeframe in ("60m", "15m"):
            key = "m60" if timeframe == "60m" else "m15"
            params = urllib.parse.urlencode({"param": f"{symbol},{key},,{limit}"})
            payload = json.loads(self._read(f"{self.MINUTE_URL}?{params}"))
            node = (payload.get("data") or {}).get(symbol) or {}
            raw = node.get(key) or []
        else:
            raise ValueError(f"不支持的周期：{timeframe}")
        bars: list[Bar] = []
        for parts in raw:
            if len(parts) < 6:
                continue
            timestamp = str(parts[0])
            if timeframe != "daily" and len(timestamp) == 12:
                timestamp = datetime.strptime(timestamp, "%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M")
            bars.append(
                Bar(timestamp, float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]),
                    float(parts[5]), float(parts[7] or 0) if len(parts) > 7 else 0)
            )
        if not bars:
            raise MarketDataError(f"未找到股票 {code} 的腾讯{timeframe} K线")
        return bars


class ResilientProvider:
    """Use Tencent first and transparently fail over to Eastmoney."""

    def __init__(self, timeout: float = 8.0):
        self.providers = [TencentProvider(timeout), EastMoneyProvider(timeout)]

    def _call(self, method: str, *args):
        errors = []
        for provider in self.providers:
            try:
                return getattr(provider, method)(*args)
            except MarketDataError as exc:
                errors.append(str(exc))
        raise MarketDataError("；备用源均失败：" + " | ".join(errors))

    def quote(self, code: str) -> Quote:
        return self._call("quote", code)

    def klines(self, code: str, timeframe: str = "daily", limit: int = 180) -> list[Bar]:
        return self._call("klines", code, timeframe, limit)
