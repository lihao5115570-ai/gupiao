from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .database import Database
from .security import unprotect_secret


MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81,m:1+t:81"
FIELDS = "f2,f3,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21,f100"
QUOTE_HOSTS = (
    "http://push2.eastmoney.com",
    "http://2.push2.eastmoney.com",
    "http://4.push2.eastmoney.com",
    "http://6.push2.eastmoney.com",
    "http://8.push2.eastmoney.com",
    "https://push2.eastmoney.com/webguest",
    "https://82.push2.eastmoney.com/webguest",
    "https://73.push2.eastmoney.com/webguest",
    "https://push2delay.eastmoney.com",
)

# The first three observations catch cancellable-order inflation before 09:20.
# The official decision path remains the seven points requested from 09:20 onward.
SAMPLE_SCHEDULE = (
    ("09:15", clock_time(9, 15, 0)),
    ("09:16", clock_time(9, 16, 0)),
    ("09:17", clock_time(9, 17, 0)),
    ("09:18", clock_time(9, 18, 0)),
    ("09:19", clock_time(9, 19, 0)),
    ("09:19:30", clock_time(9, 19, 30)),
    ("09:20", clock_time(9, 20, 0)),
    ("09:20:30", clock_time(9, 20, 30)),
    ("09:21", clock_time(9, 21, 0)),
    ("09:21:30", clock_time(9, 21, 30)),
    ("09:22", clock_time(9, 22, 0)),
    ("09:22:30", clock_time(9, 22, 30)),
    ("09:23", clock_time(9, 23, 0)),
    ("09:23:30", clock_time(9, 23, 30)),
    ("09:24", clock_time(9, 24, 0)),
    ("09:24:30", clock_time(9, 24, 30)),
    ("09:25", clock_time(9, 25, 0)),
)
REQUIRED_PATH = ("09:20", "09:21", "09:22", "09:23", "09:24", "09:24:30", "09:25")


def manual_auction_codes(settings: dict[str, str]) -> set[str]:
    """Return validated six-digit codes that must remain in the timed auction path."""
    raw = str(settings.get("auction_manual_codes") or "")
    for separator in ("，", ";", "；", " ", "\n", "\t"):
        raw = raw.replace(separator, ",")
    return {code for code in (part.strip() for part in raw.split(",")) if len(code) == 6 and code.isdigit()}


ACTION_ORDER = {"可挂入": 0, "等回踩": 1, "放弃": 2}
OUTCOME_SCHEDULE = (
    ("09:30", clock_time(9, 30, 0)),
    ("09:35", clock_time(9, 35, 0)),
    ("10:00", clock_time(10, 0, 0)),
    ("15:00", clock_time(15, 0, 5)),
)


class AuctionDataError(RuntimeError):
    pass


def _number(value: object) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _board(code: str) -> str:
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("4", "8", "92")):
        return "北交所"
    return "主板"


def _limit_pct(code: str, name: str) -> float:
    upper_name = name.upper()
    if "ST" in upper_name or "退" in name:
        return 5.0
    board = _board(code)
    return 30.0 if board == "北交所" else 20.0 if board in ("创业板", "科创板") else 10.0


class AuctionMarketProvider:
    """Parallel full-market snapshot adapter for the existing Eastmoney feed."""

    def __init__(
        self,
        timeout: float = 4.0,
        workers: int = 8,
        page_size: int = 100,
        retry_rounds: int = 1,
        retry_sleep: float = 0.2,
    ):
        self.timeout = timeout
        self.workers = workers
        self.page_size = page_size
        self.retry_rounds = retry_rounds
        self.retry_sleep = retry_sleep
        self.last_quality: dict[str, Any] = {}

    def _page(self, page: int) -> tuple[list[dict], int, datetime]:
        query = urllib.parse.urlencode(
            {
                "pn": str(page), "pz": str(self.page_size), "po": "1", "np": "1",
                "fltt": "2", "invt": "2", "fid": "f3", "fs": MARKET_FS,
                "fields": FIELDS, "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "_": str(int(time.time() * 1000)),
            }
        )
        errors = []
        for offset in range(len(QUOTE_HOSTS)):
            host = QUOTE_HOSTS[(page + offset) % len(QUOTE_HOSTS)]
            request = urllib.request.Request(
                f"{host}/api/qt/clist/get?{query}",
                headers={"User-Agent": "Mozilla/5.0 StockMonitor/1.0", "Referer": "https://quote.eastmoney.com/"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                data = payload.get("data") or {}
                return list(data.get("diff") or []), int(data.get("total") or 0), datetime.now()
            except Exception as exc:
                errors.append(str(exc))
                try:
                    process = subprocess.run(
                        [
                            "curl.exe", "-s", "-L", "--compressed", "--max-time", str(int(self.timeout)),
                            "-A", "Mozilla/5.0", "-H", "Referer: https://quote.eastmoney.com/",
                            f"{host}/api/qt/clist/get?{query}",
                        ],
                        capture_output=True, timeout=self.timeout + 3,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if process.returncode == 0 and process.stdout:
                        payload = json.loads(process.stdout.decode("utf-8", errors="replace"))
                        data = payload.get("data") or {}
                        return list(data.get("diff") or []), int(data.get("total") or 0), datetime.now()
                except Exception as curl_exc:
                    errors.append(str(curl_exc))
        raise AuctionDataError(f"全市场第{page}页获取失败：{errors[-1] if errors else '未知错误'}")

    def _page_with_retry(self, page: int) -> tuple[list[dict], int, datetime]:
        last_error: AuctionDataError | None = None
        for attempt in range(self.retry_rounds + 1):
            if attempt and self.retry_sleep > 0:
                time.sleep(self.retry_sleep * min(attempt, 3))
            try:
                return self._page(page)
            except AuctionDataError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    @staticmethod
    def _normalize(row: dict) -> dict | None:
        code = str(row.get("f12") or "")
        price = _number(row.get("f2"))
        pct = _number(row.get("f3"))
        prev_close = _number(row.get("f18"))
        if len(code) != 6 or not code.isdigit() or not price or pct is None or not prev_close:
            return None
        return {
            "code": code,
            "name": str(row.get("f14") or code),
            "industry": str(row.get("f100") or "未分类"),
            "auction_price": price,
            "auction_pct": pct,
            "auction_amount": _number(row.get("f6")) or 0.0,
            "auction_volume": _number(row.get("f5")) or 0.0,
            "prev_close": prev_close,
            "open_price": _number(row.get("f17")) or price,
            "market_value": _number(row.get("f20")) or 0.0,
            "float_market_value": _number(row.get("f21")) or 0.0,
            "high": _number(row.get("f15")) or price,
            "low": _number(row.get("f16")) or price,
        }

    def fetch_all(self) -> list[dict]:
        started_at = datetime.now()
        first_rows, total, first_at = self._page_with_retry(1)
        pages = max(1, math.ceil(total / self.page_size))
        page_rows: dict[int, list[dict]] = {1: first_rows}
        fetched_times = [first_at]
        failures: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="auction-fetch") as pool:
            futures = {pool.submit(self._page, page): page for page in range(2, pages + 1)}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    rows, _page_total, fetched_at = future.result()
                    page_rows[page] = rows
                    fetched_times.append(fetched_at)
                except AuctionDataError as exc:
                    failures[page] = str(exc)
        for _round_no in range(self.retry_rounds):
            if not failures:
                break
            retry_pages = sorted(failures)
            failures = {}
            for page in retry_pages:
                if self.retry_sleep > 0:
                    time.sleep(self.retry_sleep)
                try:
                    rows, _page_total, fetched_at = self._page_with_retry(page)
                    page_rows[page] = rows
                    fetched_times.append(fetched_at)
                except AuctionDataError as exc:
                    failures[page] = str(exc)
        if failures:
            raise AuctionDataError(f"全市场行情缺页（{len(failures)}/{pages}），本次快照作废")
        result = []
        seen = set()
        raw_count = sum(len(rows) for rows in page_rows.values())
        raw_codes = {
            str(raw.get("f12") or "")
            for rows in page_rows.values() for raw in rows
            if len(str(raw.get("f12") or "")) == 6
        }
        for page in sorted(page_rows):
            for raw in page_rows[page]:
                row = self._normalize(raw)
                if row and row["code"] not in seen:
                    seen.add(row["code"])
                    result.append(row)
        if len(result) < 1000:
            raise AuctionDataError(f"全市场有效股票只有{len(result)}只，拒绝生成不完整决策")
        finished_at = datetime.now()
        self.last_quality = {
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "expected_total": total, "raw_count": raw_count, "raw_unique_count": len(raw_codes),
            "normalized_count": len(result),
            "coverage": min(1.0, len(raw_codes) / total) if total else 0.0,
            "page_span_seconds": (max(fetched_times) - min(fetched_times)).total_seconds() if fetched_times else 0.0,
            "pages": pages,
        }
        return result


def _infoway_symbol(code: str) -> str:
    suffix = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    return f"{code}.{suffix}"


def _plain_code(symbol: str) -> str:
    match = re.search(r"(\d{6})", symbol or "")
    return match.group(1) if match else ""


class InfowayAuctionProvider:
    """Infoway HTTP adapter for validating 9:20-9:25 auction collection."""

    BASE = "https://data.infoway.io"

    def __init__(
        self,
        api_key: str,
        timeout: float = 8.0,
        batch_size: int = 100,
        request_interval: float = 1.05,
    ):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.batch_size = max(1, min(100, int(batch_size)))
        self.request_interval = max(0.0, float(request_interval))
        self.last_quality: dict[str, Any] = {}
        self._symbols_cache: list[dict] | None = None

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.api_key:
            raise AuctionDataError("Infoway API Key未配置")
        data = None
        headers = {
            "User-Agent": "StockMonitor/1.0",
            "apiKey": self.api_key,
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.BASE + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except Exception as exc:
            raise AuctionDataError(f"Infoway请求失败：{exc}") from exc
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AuctionDataError("Infoway返回内容不是JSON") from exc
        if int(result.get("ret") or 0) != 200:
            message = str(result.get("msg") or "unknown")
            raise AuctionDataError(f"Infoway返回错误：{message}")
        return result

    def symbols(self) -> list[dict]:
        if self._symbols_cache is not None:
            return self._symbols_cache
        payload = self._request("GET", "/common/basic/symbols?type=STOCK_CN")
        symbols = []
        for item in payload.get("data") or []:
            symbol = str(item.get("symbol") or "")
            code = _plain_code(symbol)
            if len(code) != 6 or not code.isdigit():
                continue
            symbols.append({
                "symbol": symbol,
                "code": code,
                "name": str(item.get("name_cn") or item.get("name_hk") or item.get("name_en") or code),
                "index": bool(item.get("index")),
            })
        self._symbols_cache = [item for item in symbols if not item["index"]]
        return self._symbols_cache

    @staticmethod
    def _pct(value: object) -> float | None:
        text = str(value or "").strip().replace("%", "")
        return _number(text)

    def _kline_chunk(self, symbols: list[str]) -> list[dict]:
        payload = self._request(
            "POST",
            "/stock/v2/batch_kline",
            {"klineType": 1, "klineNum": 2, "codes": ",".join(symbols)},
        )
        rows = []
        for item in payload.get("data") or []:
            symbol = str(item.get("s") or "")
            code = _plain_code(symbol)
            points = list(item.get("respList") or [])
            if not code or not points:
                continue
            point = points[0]
            price = _number(point.get("c"))
            pct = self._pct(point.get("pc"))
            amount = _number(point.get("vw")) or 0.0
            pca = _number(point.get("pca")) or 0.0
            if not price or pct is None:
                continue
            prev_close = price - pca if pca else price / (1 + pct / 100) if pct > -99 else 0
            rows.append({
                "code": code,
                "name": code,
                "industry": "未分类",
                "auction_price": price,
                "auction_pct": pct,
                "auction_amount": amount,
                "auction_volume": _number(point.get("v")) or 0.0,
                "prev_close": prev_close,
                "open_price": _number(point.get("o")) or price,
                "market_value": 0.0,
                "float_market_value": 0.0,
                "high": _number(point.get("h")) or price,
                "low": _number(point.get("l")) or price,
            })
        return rows

    def fetch_all(
        self,
        codes: set[str] | None = None,
        metadata: dict[str, dict] | None = None,
    ) -> list[dict]:
        started_at = datetime.now()
        all_symbols = self.symbols()
        if len(all_symbols) < 1000:
            raise AuctionDataError(f"Infoway A股产品列表仅{len(all_symbols)}只，拒绝生成不完整竞价结果")
        symbols = all_symbols
        if codes:
            symbols = [item for item in all_symbols if item["code"] in codes]
            if len(symbols) < min(30, len(codes)):
                raise AuctionDataError(f"Infoway候选池仅匹配{len(symbols)}/{len(codes)}只")
        if not symbols:
            raise AuctionDataError(f"Infoway A股产品列表仅{len(symbols)}只，拒绝生成不完整竞价结果")
        name_by_code = {item["code"]: item["name"] for item in symbols}
        metadata = metadata or {}
        result: list[dict] = []
        fetched_times = []
        chunks = [symbols[start:start + self.batch_size] for start in range(0, len(symbols), self.batch_size)]
        for index, chunk in enumerate(chunks):
            if index and self.request_interval:
                time.sleep(self.request_interval)
            fetched_times.append(datetime.now())
            rows = self._kline_chunk([item["symbol"] for item in chunk])
            for row in rows:
                extra = metadata.get(row["code"], {})
                row["name"] = str(extra.get("name") or name_by_code.get(row["code"], row["name"]))
                row["industry"] = str(extra.get("industry") or row["industry"])
                row["market_value"] = float(extra.get("market_value") or 0)
                row["float_market_value"] = float(extra.get("float_market_value") or 0)
            result.extend(rows)
        finished_at = datetime.now()
        unique_codes = {row["code"] for row in result}
        self.last_quality = {
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "expected_total": len(symbols),
            "market_total": len(all_symbols),
            "raw_count": len(result),
            "raw_unique_count": len(unique_codes),
            "normalized_count": len(unique_codes),
            "coverage": min(1.0, len(unique_codes) / len(symbols)) if symbols else 0.0,
            "page_span_seconds": (max(fetched_times) - min(fetched_times)).total_seconds() if fetched_times else 0.0,
            "pages": len(chunks),
            "source": "infoway",
            "pool_mode": bool(codes),
        }
        return result

    def fetch_sample(self, limit: int = 100) -> list[dict]:
        all_symbols = self.symbols()
        symbols = all_symbols[:max(1, min(100, limit))]
        started_at = datetime.now()
        rows = self._kline_chunk([item["symbol"] for item in symbols])
        finished_at = datetime.now()
        names = {item["code"]: item["name"] for item in symbols}
        for row in rows:
            row["name"] = names.get(row["code"], row["name"])
        self.last_quality = {
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "expected_total": len(all_symbols),
            "sample_total": len(symbols),
            "raw_count": len(rows),
            "raw_unique_count": len({row["code"] for row in rows}),
            "normalized_count": len({row["code"] for row in rows}),
            "coverage": min(1.0, len(rows) / len(symbols)) if symbols else 0.0,
            "page_span_seconds": (finished_at - started_at).total_seconds(),
            "pages": 1,
            "source": "infoway",
        }
        return rows


def provider_from_settings(settings: dict[str, str]) -> Any:
    if settings.get("auction_data_source", "eastmoney") == "infoway":
        try:
            key = unprotect_secret(settings.get("secret_infoway_key", ""))
        except Exception:
            key = settings.get("secret_infoway_key", "")
        return InfowayAuctionProvider(
            key,
            request_interval=_setting_float(settings, "infoway_request_interval", 1.05),
        )
    return AuctionMarketProvider()


def build_candidate_pool(rows: list[dict], settings: dict[str, str]) -> list[dict]:
    """Build a small, liquid pre-auction universe before the timed path begins."""
    min_price = _setting_float(settings, "auction_pool_min_price", 3.0)
    max_price = _setting_float(settings, "auction_high_price", 60.0)
    min_pct = _setting_float(settings, "auction_pool_min_pct", 0.30)
    max_pct = _setting_float(settings, "auction_pool_max_pct", 6.50)
    min_amount = _setting_float(settings, "auction_pool_min_amount", 5_000_000)
    max_size = max(50, int(_setting_float(settings, "auction_pool_max_size", 500)))
    per_sector = max(1, int(_setting_float(settings, "auction_pool_max_per_sector", 5)))
    min_sector_positive = max(1, int(_setting_float(settings, "auction_pool_min_sector_positive", 3)))

    eligible: list[dict] = []
    for raw in rows:
        row = dict(raw)
        code = str(row.get("code") or "")
        name = str(row.get("name") or code)
        price = float(row.get("auction_price") or 0)
        pct = float(row.get("auction_pct") or 0)
        amount = float(row.get("auction_amount") or 0)
        industry = str(row.get("industry") or "未分类")
        upper_name = name.upper()
        if _board(code) != "主板":
            continue
        if "ST" in upper_name or "退" in name or upper_name.startswith(("N", "C")):
            continue
        if not min_price <= price < max_price:
            continue
        if not min_pct <= pct <= max_pct:
            continue
        if amount < min_amount or industry == "未分类":
            continue
        eligible.append(row)

    sector_members: dict[str, list[dict]] = defaultdict(list)
    for row in eligible:
        sector_members[str(row["industry"])].append(row)

    pool: list[dict] = []
    for sector, members in sector_members.items():
        positive_count = sum(1 for row in members if float(row.get("auction_pct") or 0) > 0)
        avg_pct = sum(float(row.get("auction_pct") or 0) for row in members) / len(members)
        if positive_count < min_sector_positive or avg_pct <= 0:
            continue
        ranked = sorted(
            members,
            key=lambda row: (
                -float(row.get("auction_amount") or 0),
                -float(row.get("auction_pct") or 0),
                str(row.get("code") or ""),
            ),
        )
        for row in ranked[:per_sector]:
            item = dict(row)
            item["pool_sector_positive"] = positive_count
            item["pool_sector_pct"] = round(avg_pct, 2)
            pool.append(item)

    preferred_min = _setting_float(settings, "auction_preferred_min_price", 8.0)
    preferred_max = _setting_float(settings, "auction_preferred_max_price", 45.0)
    pool.sort(
        key=lambda row: (
            -int(preferred_min <= float(row.get("auction_price") or 0) <= preferred_max),
            -int(row.get("pool_sector_positive") or 0),
            -float(row.get("pool_sector_pct") or 0),
            -float(row.get("auction_amount") or 0),
            str(row.get("code") or ""),
        )
    )
    return pool[:max_size]


class TencentQuoteVerifier:
    """Fast independent quote check for the small final candidate set."""

    URL = "https://qt.gtimg.cn/q="

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout

    @staticmethod
    def _symbol(code: str) -> str:
        return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code

    def _read(self, symbols: list[str]) -> bytes:
        url = self.URL + ",".join(symbols)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 StockMonitor/1.0", "Referer": "https://gu.qq.com/"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except Exception as first_error:
            process = subprocess.run(
                [
                    "curl.exe", "-s", "-L", "--compressed", "--max-time", str(int(self.timeout)),
                    "-A", "Mozilla/5.0", "-H", "Referer: https://gu.qq.com/", url,
                ],
                capture_output=True, timeout=self.timeout + 3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.returncode != 0 or not process.stdout:
                raise AuctionDataError(f"腾讯交叉行情失败：{first_error}")
            return process.stdout

    def fetch(self, codes: list[str]) -> dict[str, dict]:
        result = {}
        unique_codes = list(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        for start in range(0, len(unique_codes), 50):
            chunk = unique_codes[start:start + 50]
            text = self._read([self._symbol(code) for code in chunk]).decode("gb18030", errors="replace")
            for symbol, payload in re.findall(r'v_([a-z]{2}\d{6})="([^"]*)"', text):
                fields = payload.split("~")
                if len(fields) < 38 or not fields[2] or not fields[3]:
                    continue
                code = fields[2]
                try:
                    result[code] = {
                        "code": code, "name": fields[1], "price": float(fields[3]),
                        "prev_close": float(fields[4] or 0), "open": float(fields[5] or 0),
                        "pct": float(fields[32] or 0), "timestamp": fields[30],
                        "high": float(fields[33] or fields[3]), "low": float(fields[34] or fields[3]),
                        "amount": float(fields[37] or 0) * 10_000,
                    }
                except (TypeError, ValueError):
                    continue
        return result


def apply_crosscheck(
    rows: list[dict], quotes: dict[str, dict] | None, error: str = "",
    checked_at: datetime | None = None, max_age_seconds: float = 20,
) -> list[dict]:
    checked_at = checked_at or datetime.now()
    today = checked_at.strftime("%Y%m%d")
    for row in rows:
        if row.get("action") != "可挂入":
            continue
        quote = (quotes or {}).get(str(row.get("code")))
        reason = ""
        if error:
            reason = f"第二行情源不可用：{error}"
        elif not quote:
            reason = "第二行情源缺少该股票"
        else:
            stamp = str(quote.get("timestamp") or "")
            price_diff = abs(float(quote.get("price") or 0) - float(row.get("lock_price") or 0))
            pct_diff = abs(float(quote.get("pct") or 0) - float(row.get("lock_pct") or 0))
            max_price_diff = max(0.02, float(row.get("lock_price") or 0) * 0.003)
            if len(stamp) != 14 or not stamp.startswith(today) or stamp[8:14] < "092450":
                reason = f"第二行情源时间戳异常({stamp or '空'})"
            else:
                quote_time = datetime.strptime(stamp, "%Y%m%d%H%M%S")
                age = (checked_at - quote_time).total_seconds()
                if age > max_age_seconds or age < -5:
                    reason = f"第二行情源时间偏差{age:.0f}秒"
            if not reason and (price_diff > max_price_diff or pct_diff > 0.30):
                reason = f"双源价格不一致(价差{price_diff:.2f}，涨幅差{pct_diff:.2f}个百分点)"
        if reason:
            row["action"] = "放弃"
            row["can_order"] = False
            row["suggested_price"] = None
            row["suggested_order"] = "--"
            row["crosscheck"] = "失败"
            row["elimination_reasons"].append(reason)
            row["no_buy_condition"] = reason
        else:
            row["crosscheck"] = "通过"
            row["crosscheck_source"] = "腾讯行情"
            row["crosscheck_timestamp"] = quote.get("timestamp")
    rows.sort(key=lambda row: (ACTION_ORDER[row["action"]], -row["score"], -row["auction_amount"], row["code"]))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    return rows


def _setting_float(settings: dict[str, str], key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def score_auction_paths(snapshot_rows: list[Any], settings: dict[str, str]) -> list[dict]:
    by_code: dict[str, dict[str, dict]] = defaultdict(dict)
    for raw in snapshot_rows:
        row = dict(raw)
        by_code[str(row["code"])][str(row["sample_label"])] = row

    lock_rows = {code: points.get("09:25") for code, points in by_code.items() if points.get("09:25")}
    sector_members: dict[str, list[dict]] = defaultdict(list)
    for row in lock_rows.values():
        sector_members[str(row.get("industry") or "未分类")].append(row)

    sector_stats = {}
    for sector, members in sector_members.items():
        valid = [float(row["auction_pct"]) for row in members if row.get("auction_pct") is not None]
        positive = [row for row in members if float(row.get("auction_pct") or 0) > 0]
        leaders = sorted(positive, key=lambda row: float(row.get("auction_pct") or 0), reverse=True)[:3]
        sector_stats[sector] = {
            "positive_count": len(positive),
            "avg_pct": round(sum(valid) / len(valid), 2) if valid else 0.0,
            "leaders": "、".join(f"{row['name']} {float(row['auction_pct']):+.2f}%" for row in leaders),
        }

    min_amount = _setting_float(settings, "auction_min_amount", 20_000_000)
    high_price = _setting_float(settings, "auction_high_price", 100)
    min_retention = _setting_float(settings, "auction_min_retention", 0.70)
    late_drop_limit = _setting_float(settings, "auction_late_drop", 0.50)
    max_drawdown = _setting_float(settings, "auction_max_drawdown", 2.00)
    min_float_ratio = _setting_float(settings, "auction_min_float_amount_ratio", 0.0005)
    min_market_positive = _setting_float(settings, "auction_min_market_positive_ratio", 0.35)
    preferred_min_price = _setting_float(settings, "auction_preferred_min_price", 8.0)
    preferred_max_price = _setting_float(settings, "auction_preferred_max_price", 45.0)
    valid_market = [float(row["auction_pct"]) for row in lock_rows.values() if row.get("auction_pct") is not None]
    market_positive_ratio = sum(1 for value in valid_market if value > 0) / len(valid_market) if valid_market else 0.0
    results = []
    specified_codes = manual_auction_codes(settings)

    for code, points in by_code.items():
        locked = points.get("09:25")
        if not locked:
            continue
        name = str(locked.get("name") or code)
        sector = str(locked.get("industry") or "未分类")
        pct_925 = float(locked.get("auction_pct") or 0)
        prev_close = float(locked.get("prev_close") or 0)
        lock_price = float(locked.get("auction_price") or 0)
        amount = float(locked.get("auction_amount") or 0)
        float_market_value = float(locked.get("float_market_value") or 0)
        float_amount_ratio = amount / float_market_value if float_market_value > 0 else 0.0
        observed = [float(item["auction_pct"]) for item in points.values() if item.get("auction_pct") is not None]
        peak_pct = max(observed) if observed else pct_925
        # The decision table is a candidate history, not a dump of every flat/down stock.
        if peak_pct <= 0 and pct_925 <= 0 and code not in specified_codes:
            continue
        drawdown = max(0.0, peak_pct - pct_925)
        retention = pct_925 / peak_pct if peak_pct > 0 else 0.0
        pct_92430 = float((points.get("09:24:30") or {}).get("auction_pct") or pct_925)
        late_drop = max(0.0, pct_92430 - pct_925)
        limit_pct = _limit_pct(code, name)
        limit_price = _round_price(prev_close * (1 + limit_pct / 100)) if prev_close else 0.0
        near_limit = pct_925 >= limit_pct - 0.5
        board = _board(code)
        sector_info = sector_stats.get(sector, {"positive_count": 0, "avg_pct": 0.0, "leaders": ""})
        required_count = sum(1 for label in REQUIRED_PATH if label in points)

        eliminate = []
        warnings = []
        upper_name = name.upper()
        if upper_name.startswith(("N", "C")) or upper_name.endswith(("-U", "-W", "-V")):
            eliminate.append("新股/特殊交易标识默认关闭")
        if board == "创业板" and settings.get("auction_include_chinext", "0") != "1":
            eliminate.append("创业板默认关闭")
        if board == "科创板" and settings.get("auction_include_star", "0") != "1":
            eliminate.append("科创板默认关闭")
        if board == "北交所" and settings.get("auction_include_bse", "0") != "1":
            eliminate.append("北交所默认关闭")
        if ("ST" in upper_name or "退" in name) and settings.get("auction_include_st", "0") != "1":
            eliminate.append("ST/退市整理默认关闭")
        if lock_price >= high_price and settings.get("auction_include_high_price", "0") != "1":
            eliminate.append(f"高价股≥{high_price:g}元")
        if required_count < len(REQUIRED_PATH):
            eliminate.append(f"正式路径不完整({required_count}/{len(REQUIRED_PATH)})")
        if pct_925 <= 0:
            eliminate.append("9:25锁单涨幅≤0")
        if peak_pct > 0 and retention < min_retention:
            eliminate.append(f"锁单留存率{retention:.0%}低于{min_retention:.0%}")
        if late_drop >= late_drop_limit:
            eliminate.append(f"9:24:30后走弱{late_drop:.2f}个百分点")
        if drawdown > max_drawdown:
            eliminate.append(f"最高涨幅回撤{drawdown:.2f}个百分点")
        if amount < min_amount:
            eliminate.append(f"竞价额不足{min_amount / 10_000:.0f}万元")
        if float_market_value <= 0:
            eliminate.append("流通市值缺失")
        elif float_amount_ratio < min_float_ratio:
            eliminate.append(f"竞价额/流通市值仅{float_amount_ratio:.3%}")
        if market_positive_ratio < min_market_positive:
            eliminate.append(f"全市场正竞价比例仅{market_positive_ratio:.0%}")
        if sector == "未分类":
            eliminate.append("板块数据缺失")
        elif sector_info["positive_count"] < 3 or sector_info["avg_pct"] <= 0:
            eliminate.append(f"板块无共振(正竞价{sector_info['positive_count']}只)")

        if pct_925 > 7:
            warnings.append("高开超过7%，禁止直接追价")
        if near_limit:
            warnings.append("接近涨停，成交和回落风险高")
        if retention < 0.85:
            warnings.append("锁单留存一般")
        if drawdown > 1:
            warnings.append("竞价路径已有明显回撤")
        if sector_info["avg_pct"] < 0.5:
            warnings.append("板块整体强度一般")
        preferred_price = preferred_min_price <= lock_price <= preferred_max_price
        if lock_price < preferred_min_price:
            warnings.append(f"低于优先价格带{preferred_min_price:g}元，波动和流动性风险更高")
        elif lock_price > preferred_max_price:
            warnings.append(f"高于优先价格带{preferred_max_price:g}元，同等资金可分配股数较少")

        stable = retention >= 0.90 and drawdown <= 0.8 and late_drop <= 0.15
        resonance = sector_info["positive_count"] >= 5 and sector_info["avg_pct"] >= 0.6
        moderate_gap = 1.0 <= pct_925 <= 5.5
        deep_liquidity = amount >= min_amount * 1.25 and float_amount_ratio >= min_float_ratio * 1.25
        if not moderate_gap:
            warnings.append("锁单涨幅不在保守可挂区间1%-5.5%")
        if not deep_liquidity:
            warnings.append("竞价流动性仅达到基础门槛，未达到A档")
        if not resonance and not any("板块无共振" in reason for reason in eliminate):
            warnings.append("板块共振未达到A档（至少5只正竞价且板块均值≥0.6%）")
        if eliminate:
            action = "放弃"
        elif pct_925 > 7 or near_limit or not stable or not resonance or not moderate_gap or not deep_liquidity:
            action = "等回踩"
        else:
            action = "可挂入"

        score = 100.0
        score -= min(35, drawdown * 12)
        score -= min(25, max(0, 0.9 - retention) * 100)
        score -= min(20, late_drop * 20)
        score += min(8, max(0, sector_info["avg_pct"]) * 2)
        score += min(7, amount / max(min_amount, 1) * 2)
        score += 3 if preferred_price else -5
        score -= min(30, len(eliminate) * 10)
        score = round(max(0, min(100, score)), 1)

        if action == "可挂入":
            suggested_value = min(limit_price, _round_price(lock_price * 1.003))
            suggested_order = f"≤{suggested_value:.2f}"
            no_buy = f"开盘高于{suggested_value:.2f}不追；板块转弱或跌破锁单价放弃"
        elif action == "等回踩":
            suggested_value = _round_price(max(prev_close, lock_price * 0.985))
            suggested_order = f"回踩至{suggested_value:.2f}附近"
            no_buy = "未回踩、回踩放量跌破或板块前排转弱则不买"
        else:
            suggested_value = None
            suggested_order = "--"
            no_buy = "已触发淘汰条件，不挂单"

        probability = int(max(5, min(90, 45 + score * 0.35 + min(15, amount / max(min_amount, 1) * 4) - max(0, pct_925 - 5) * 3)))
        if action == "放弃":
            probability = min(probability, 30)
        false_risk = "高" if retention < min_retention or drawdown > max_drawdown or late_drop >= late_drop_limit else "中" if retention < 0.85 or drawdown > 1 else "低"
        pullback_risk = "高" if pct_925 > 7 or near_limit or not resonance else "中" if pct_925 > 5 or drawdown > 1 else "低"
        path = {label: (round(float(points[label]["auction_pct"]), 2) if label in points and points[label].get("auction_pct") is not None else None) for label, _when in SAMPLE_SCHEDULE}

        results.append({
            "rank": 0, "code": code, "name": name, "board": board, "industry": sector,
            "specified": code in specified_codes,
            "action": action, "can_order": action == "可挂入", "score": score,
            "lock_pct": round(pct_925, 2), "peak_pct": round(peak_pct, 2),
            "drawdown": round(drawdown, 2), "retention": round(retention, 4),
            "late_drop": round(late_drop, 2), "auction_amount": amount,
            "float_market_value": float_market_value,
            "float_amount_ratio": round(float_amount_ratio, 6),
            "market_positive_ratio": round(market_positive_ratio, 4),
            "lock_price": lock_price, "open_price": lock_price, "prev_close": prev_close,
            "limit_price": limit_price, "near_limit": near_limit,
            "sector_positive_count": sector_info["positive_count"],
            "sector_pct": sector_info["avg_pct"], "sector_leaders": sector_info["leaders"],
            "sector_resonance": "强" if resonance else "弱",
            "false_risk": false_risk, "pullback_risk": pullback_risk,
            "suggested_price": suggested_value, "suggested_order": suggested_order,
            "fill_probability": probability, "no_buy_condition": no_buy,
            "elimination_reasons": eliminate, "warnings": warnings, "path": path,
        })

    results.sort(key=lambda row: (ACTION_ORDER[row["action"]], -row["score"], -row["auction_amount"], row["code"]))
    max_orderable = max(1, int(_setting_float(settings, "auction_max_orderable", 3)))
    max_per_sector = max(1, int(_setting_float(settings, "auction_max_per_sector", 1)))
    kept = 0
    sector_kept: dict[str, int] = defaultdict(int)
    for row in results:
        if row["action"] != "可挂入":
            continue
        sector = str(row["industry"])
        if kept >= max_orderable or sector_kept[sector] >= max_per_sector:
            row["action"] = "等回踩"
            row["can_order"] = False
            row["warnings"].append("超过每日或同板块可挂入数量上限")
            row["suggested_price"] = _round_price(max(row["prev_close"], row["lock_price"] * 0.985))
            row["suggested_order"] = f"回踩至{row['suggested_price']:.2f}附近"
            row["no_buy_condition"] = "优先级低于同板块/全市场前排，未充分回踩不买"
        else:
            kept += 1
            sector_kept[sector] += 1
    results.sort(key=lambda row: (ACTION_ORDER[row["action"]], -row["score"], -row["auction_amount"], row["code"]))
    for index, row in enumerate(results, 1):
        row["rank"] = index
    return results


def apply_data_quality_gate(rows: list[dict], reasons: list[str]) -> list[dict]:
    if not reasons:
        return rows
    reason_text = "；".join(reasons)
    for row in rows:
        row["data_quality"] = "不可用"
        row["data_quality_reasons"] = list(reasons)
        row["elimination_reasons"].extend(reason for reason in reasons if reason not in row["elimination_reasons"])
        if row["action"] != "放弃":
            row["action"] = "放弃"
            row["can_order"] = False
            row["suggested_price"] = None
            row["suggested_order"] = "--"
            row["no_buy_condition"] = f"竞价数据质量未通过：{reason_text}"
    rows.sort(key=lambda row: (-row["score"], -row["auction_amount"], row["code"]))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    return rows


class AuctionEngine:
    def __init__(
        self, database: Database, events: Any, provider: AuctionMarketProvider | None = None,
        verifier: TencentQuoteVerifier | None = None, now_fn: Any = datetime.now,
    ):
        self.db = database
        self.events = events
        self.provider = provider or AuctionMarketProvider()
        self._provider_injected = provider is not None
        self.verifier = verifier or TencentQuoteVerifier()
        self._now = now_fn
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._capturing = threading.Lock()
        self._completed: dict[str, set[str]] = defaultdict(set)
        self._outcome_completed: dict[str, set[str]] = defaultdict(set)
        self._pool_attempted: set[str] = set()
        self._pool_failures: dict[str, int] = defaultdict(int)
        self._pool_codes: dict[str, set[str]] = {}
        self._pool_metadata: dict[str, dict[str, dict]] = {}
        self.latest_decisions: list[dict] = []

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="auction-monitor", daemon=True)
        self._thread.start()
        self.events.put(("auction_status", "竞价监控已启动，等待下一个采样点"))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def shutdown(self) -> None:
        self.stop()

    def wake(self) -> None:
        self._wake.set()

    def refresh_saved(self, trading_day: str | None = None) -> list[dict]:
        day = trading_day or date.today().isoformat()
        self.latest_decisions = self.db.auction_decisions(day)
        return self.latest_decisions

    def capture_now(self) -> None:
        threading.Thread(target=self._manual_capture, name="auction-manual", daemon=True).start()

    def test_infoway_source(self) -> None:
        threading.Thread(target=self._test_infoway_source, name="auction-infoway-test", daemon=True).start()

    def _test_infoway_source(self) -> None:
        try:
            settings = self.db.get_settings()
            provider = InfowayAuctionProvider(
                unprotect_secret(settings.get("secret_infoway_key", "")),
                request_interval=_setting_float(settings, "infoway_request_interval", 1.05),
            )
            self.events.put(("auction_status", "正在测试Infoway数据源，先取A股列表和100只样本…"))
            rows = provider.fetch_sample(100)
            quality = provider.last_quality
            valid = [row for row in rows if row.get("auction_price") and row.get("auction_pct") is not None]
            if not valid:
                raise AuctionDataError("Infoway样本没有返回有效价格/涨幅")
            self.events.put((
                "auction_status",
                f"Infoway连通成功：样本{len(valid)}/100；全市场列表约{quality.get('expected_total', 0)}只。明天9:20-9:25可做真实采样验证。",
            ))
        except Exception as exc:
            self.events.put(("auction_error", f"Infoway测试失败：{exc}"))

    def _manual_capture(self) -> None:
        now = self._now()
        if now.time() < clock_time(9, 15):
            self.events.put(("auction_error", "竞价尚未开始；请保持软件运行，9:15会自动采样"))
            return
        if now.time() >= clock_time(9, 25, 40):
            self.events.put(("auction_error", "9:25锁单窗口已结束，不能用盘后数据补写竞价结果"))
            return
        label = next((name for name, target in reversed(SAMPLE_SCHEDULE) if now.time() >= target), "09:15")
        self._capture(label, now)

    def _prepare_infoway_pool(self, now: datetime) -> None:
        day = now.date().isoformat()
        if day in self._pool_attempted:
            return
        self._pool_attempted.add(day)
        settings = self.db.get_settings()
        if settings.get("auction_data_source") != "infoway" or settings.get("auction_pool_enabled", "1") != "1":
            return
        if not self._capturing.acquire(blocking=False):
            self._pool_attempted.discard(day)
            return
        try:
            self.events.put(("auction_status", "正在生成9:20后跟踪的盘前核心池…"))
            started = time.monotonic()
            market_rows = AuctionMarketProvider(timeout=5.0, workers=10, retry_rounds=2).fetch_all()
            pool = build_candidate_pool(market_rows, settings)
            min_pool = max(30, int(_setting_float(settings, "auction_pool_min_size", 80)))
            if len(pool) < min_pool:
                raise AuctionDataError(f"盘前核心池仅{len(pool)}只，低于安全下限{min_pool}只")
            specified_codes = manual_auction_codes(settings)
            self._pool_codes[day] = {str(row["code"]) for row in pool} | specified_codes
            self._pool_metadata[day] = {
                str(row["code"]): {
                    "name": row.get("name"),
                    "industry": row.get("industry"),
                    "market_value": row.get("market_value"),
                    "float_market_value": row.get("float_market_value"),
                }
                for row in market_rows
            }
            sectors = len({str(row.get("industry") or "") for row in pool})
            self.events.put((
                "auction_status",
                f"盘前核心池已就绪：{len(pool)}只/{sectors}个板块｜指定{len(specified_codes)}只｜用时{time.monotonic() - started:.1f}秒",
            ))
        except Exception as exc:
            self._pool_failures[day] += 1
            if self._pool_failures[day] < 2 and now.time() < clock_time(9, 19, 20):
                self._pool_attempted.discard(day)
            self.events.put(("auction_error", f"盘前核心池生成失败：{exc}；不会冒险用不完整路径出结论"))
        finally:
            self._capturing.release()

    def _capture(self, label: str, now: datetime) -> None:
        if not self._capturing.acquire(blocking=False):
            return
        day = now.date().isoformat()
        target_time = dict(SAMPLE_SCHEDULE)[label]
        target_at = datetime.combine(now.date(), target_time)
        started_wall = self._now()
        run_payload = {
            "trading_day": day, "sample_label": label,
            "target_at": target_at.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": started_wall.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "finished_at": started_wall.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "status": "failed", "source_kind": "live", "error": "",
        }
        try:
            self.events.put(("auction_status", f"{label} 正在采集全市场竞价数据…"))
            started = time.monotonic()
            settings = self.db.get_settings()
            if not self._provider_injected:
                self.provider = provider_from_settings(settings)
            if isinstance(self.provider, InfowayAuctionProvider) and settings.get("auction_pool_enabled", "1") == "1":
                pool_codes = self._pool_codes.get(day)
                if not pool_codes:
                    raise AuctionDataError("盘前核心池未生成；请在9:18前启动软件")
                tracked_codes = set(pool_codes) | manual_auction_codes(settings)
                rows = self.provider.fetch_all(tracked_codes, self._pool_metadata.get(day))
            else:
                rows = self.provider.fetch_all()
            finished_wall = self._now()
            sampled_at = finished_wall.strftime("%Y-%m-%d %H:%M:%S")
            quality = dict(self.provider.last_quality)
            coverage = float(quality.get("coverage") or 0)
            page_span = float(quality.get("page_span_seconds") or 0)
            start_delay = max(0.0, (started_wall - target_at).total_seconds())
            quality_reasons = []
            if coverage < _setting_float(settings, "auction_min_market_coverage", 0.98):
                quality_reasons.append(f"市场覆盖率{coverage:.1%}不足")
            if page_span > _setting_float(settings, "auction_max_snapshot_span", 12):
                quality_reasons.append(f"跨页时差{page_span:.1f}秒过大")
            if start_delay > _setting_float(settings, "auction_max_start_delay", 3):
                quality_reasons.append(f"启动延迟{start_delay:.1f}秒")
            run_payload.update(quality)
            run_payload.update({
                "finished_at": finished_wall.strftime("%Y-%m-%d %H:%M:%S.%f"),
                "start_delay_seconds": start_delay,
                "status": "passed" if not quality_reasons else "failed",
                "error": "；".join(quality_reasons),
            })
            self.db.save_auction_sample_run(run_payload)
            self.db.save_auction_snapshot(day, label, sampled_at, rows)
            self._completed[day].add(label)
            elapsed = time.monotonic() - started
            self.events.put(("auction_sample", {
                "label": label, "count": len(rows), "elapsed": elapsed, "time": sampled_at,
                "quality": run_payload["status"], "coverage": coverage, "span": page_span,
                "delay": start_delay, "error": run_payload["error"],
            }))
            if label == "09:25":
                decisions = score_auction_paths(self.db.auction_snapshots(day), settings)
                decisions = apply_data_quality_gate(decisions, self._quality_reasons(day))
                if settings.get("auction_require_crosscheck", "1") == "1" and any(row["action"] == "可挂入" for row in decisions):
                    codes = [str(row["code"]) for row in decisions if row["action"] == "可挂入"]
                    try:
                        decisions = apply_crosscheck(
                            decisions, self.verifier.fetch(codes), checked_at=self._now(),
                            max_age_seconds=_setting_float(settings, "auction_max_crosscheck_age", 20),
                        )
                    except Exception as verify_error:
                        decisions = apply_crosscheck(decisions, None, str(verify_error))
                from .backtest import AuctionBacktester, apply_backtest_gate, stamp_profile
                decisions = stamp_profile(decisions, settings)
                decisions = apply_backtest_gate(decisions, AuctionBacktester(self.db).run(settings))
                self.db.save_auction_decisions(day, decisions)
                self.latest_decisions = decisions
                self.events.put(("auction_decisions", decisions))
        except Exception as exc:
            run_payload.update({"finished_at": self._now().strftime("%Y-%m-%d %H:%M:%S.%f"), "error": str(exc)})
            self.db.save_auction_sample_run(run_payload)
            self.events.put(("auction_error", f"{label}采样失败：{exc}"))
        finally:
            self._capturing.release()

    def _quality_reasons(self, day: str) -> list[str]:
        runs = {str(row["sample_label"]): row for row in self.db.auction_sample_runs(day)}
        reasons = []
        for label in REQUIRED_PATH:
            run = runs.get(label)
            if not run:
                reasons.append(f"{label}采样记录缺失")
            elif run["status"] != "passed":
                reasons.append(f"{label}数据质量失败({run['error'] or '未知原因'})")
        return reasons

    def _capture_outcome(self, label: str, now: datetime) -> None:
        if not self._capturing.acquire(blocking=False):
            return
        try:
            self.events.put(("auction_status", f"正在记录{label}候选表现（不会重新选股）…"))
            wanted = [str(row.get("code")) for row in self.latest_decisions if row.get("action") in ("竞价小仓", "试验观察", "等待9:35确认")]
            day = now.date().isoformat()
            if not wanted:
                self._outcome_completed[day].add(label)
                self.events.put(("auction_outcome", {"label": label, "count": 0}))
                return
            quotes = self.verifier.fetch(wanted)
            checked_at = self._now()
            target_at = datetime.combine(now.date(), dict(OUTCOME_SCHEDULE)[label])
            fresh_quotes = {}
            for code, quote in quotes.items():
                stamp = str(quote.get("timestamp") or "")
                try:
                    quote_at = datetime.strptime(stamp, "%Y%m%d%H%M%S")
                except ValueError:
                    continue
                if quote_at >= target_at - timedelta(seconds=5) and -5 <= (checked_at - quote_at).total_seconds() <= 30:
                    fresh_quotes[code] = quote
            if len(fresh_quotes) < len(set(wanted)):
                raise AuctionDataError(f"{label}候选报价仅{len(fresh_quotes)}/{len(set(wanted))}只时间有效")
            matched = [
                {
                    "code": code, "auction_price": quote["price"], "high": quote["high"],
                    "low": quote["low"], "auction_amount": quote["amount"], "source_kind": "live",
                }
                for code, quote in fresh_quotes.items()
            ]
            observed_at = checked_at.strftime("%Y-%m-%d %H:%M:%S")
            self.db.save_auction_outcomes(day, label, observed_at, matched)
            self._outcome_completed[day].add(label)
            self.events.put(("auction_outcome", {"label": label, "count": len(matched)}))
        except Exception as exc:
            self.events.put(("auction_error", f"{label}表现记录失败（不影响已锁定结论）：{exc}"))
        finally:
            self._capturing.release()

    def _run(self) -> None:
        self.db.prune_auction_history(120)
        today = date.today().isoformat()
        active_day = today
        existing = self.db.auction_snapshots(today)
        self._completed[today].update(str(row["sample_label"]) for row in existing)
        self._outcome_completed[today].update(self.db.auction_outcome_labels(today))
        self.refresh_saved(today)
        if not self.latest_decisions and "09:25" in self._completed[today]:
            self.latest_decisions = score_auction_paths(existing, self.db.get_settings())
            self.latest_decisions = apply_data_quality_gate(self.latest_decisions, self._quality_reasons(today))
            settings = self.db.get_settings()
            if settings.get("auction_require_crosscheck", "1") == "1" and any(row["action"] == "可挂入" for row in self.latest_decisions):
                # A restart after the lock window must not fabricate a historical cross-check.
                self.latest_decisions = apply_crosscheck(self.latest_decisions, None, "9:25交叉校验记录缺失")
            from .backtest import AuctionBacktester, apply_backtest_gate, stamp_profile
            self.latest_decisions = stamp_profile(self.latest_decisions, settings)
            self.latest_decisions = apply_backtest_gate(self.latest_decisions, AuctionBacktester(self.db).run(settings))
            self.db.save_auction_decisions(today, self.latest_decisions)
        if self.latest_decisions:
            self.events.put(("auction_decisions", self.latest_decisions))
        while not self._stop.is_set():
            now = self._now()
            day = now.date().isoformat()
            if day != active_day:
                active_day = day
                existing = self.db.auction_snapshots(day)
                self._completed[day].update(str(row["sample_label"]) for row in existing)
                self._outcome_completed[day].update(self.db.auction_outcome_labels(day))
                self.latest_decisions = self.db.auction_decisions(day)
                self.events.put(("auction_decisions", self.latest_decisions))
            if now.weekday() >= 5:
                self._wake.wait(30); self._wake.clear(); continue
            if self.db.get_settings().get("auction_enabled", "1") != "1":
                self._wake.wait(10); self._wake.clear(); continue
            settings = self.db.get_settings()
            if (
                settings.get("auction_data_source") == "infoway"
                and settings.get("auction_pool_enabled", "1") == "1"
                and day not in self._pool_attempted
                and clock_time(9, 18, 0) <= now.time() <= clock_time(9, 19, 40)
            ):
                self._prepare_infoway_pool(now)
                now = self._now()
            for label, target in SAMPLE_SCHEDULE:
                if label not in REQUIRED_PATH:
                    continue
                if label in self._completed[day]:
                    continue
                delta = (datetime.combine(now.date(), target) - now).total_seconds()
                # Allow enough time for a full-market request while refusing post-open backfill.
                grace = 40 if label == "09:25" else 20
                if -grace <= delta <= 1:
                    self._capture(label, now)
                    break
            if self.latest_decisions and settings.get("auction_track_outcomes", "1") == "1":
                for label, target in OUTCOME_SCHEDULE:
                    if label in self._outcome_completed[day]:
                        continue
                    delta = (datetime.combine(now.date(), target) - now).total_seconds()
                    if -60 <= delta <= 1:
                        self._capture_outcome(label, now)
                        break
            self._wake.wait(0.5 if clock_time(9, 14, 50) <= now.time() <= clock_time(9, 25, 40) else 20)
            self._wake.clear()
