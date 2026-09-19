from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class MovementDataError(RuntimeError):
    pass


@dataclass
class MovementCandidate:
    kind: str
    code: str
    name: str
    industry: str
    price: float
    pct: float
    delta_pct: float
    amount: float
    open_pct: float
    samples: int
    reason: str
    risk: str


def _number(value: object) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _is_excluded(code: str, name: str) -> bool:
    upper = name.upper()
    return (
        "ST" in upper
        or "退" in name
        or code.startswith(("688", "689", "4", "8", "92"))
    )


class MovementMarketProvider:
    """Full A-share feed for intraday movement monitoring."""

    ENDPOINTS = (
        "http://push2.eastmoney.com/api/qt/clist/get",
        "http://2.push2.eastmoney.com/api/qt/clist/get",
        "http://4.push2.eastmoney.com/api/qt/clist/get",
        "http://6.push2.eastmoney.com/api/qt/clist/get",
        "http://8.push2.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/webguest/api/qt/clist/get",
        "https://82.push2.eastmoney.com/webguest/api/qt/clist/get",
        "https://73.push2.eastmoney.com/webguest/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
    )
    FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81,m:1+t:81"
    FIELDS = "f2,f3,f6,f12,f14,f17,f18,f20,f21,f100"

    def __init__(self, timeout: float = 8.0, pages: int | None = None, page_size: int = 100, retries: int = 3, workers: int = 4):
        self.timeout = timeout
        self.pages = pages
        self.page_size = page_size
        self.retries = retries
        self.workers = workers

    def _page(self, page: int) -> tuple[list[dict[str, Any]], int]:
        query = urllib.parse.urlencode(
            {
                "pn": str(page),
                "pz": str(self.page_size),
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": self.FS,
                "fields": self.FIELDS,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "_": str(int(time.time() * 1000)),
            }
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(0.35 * attempt)
            for endpoint in self.ENDPOINTS:
                url = f"{endpoint}?{query}"
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 StockMonitor/1.0",
                        "Referer": "https://quote.eastmoney.com/",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    break
                except Exception as exc:
                    last_error = exc
                    try:
                        process = subprocess.run(
                            [
                                "curl.exe", "-s", "-L", "--compressed", "--max-time", str(int(self.timeout)),
                                "-A", "Mozilla/5.0", "-H", "Referer: https://quote.eastmoney.com/", url,
                            ],
                            capture_output=True, timeout=self.timeout + 3,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        if process.returncode == 0 and process.stdout:
                            payload = json.loads(process.stdout.decode("utf-8", errors="replace"))
                            break
                    except Exception as curl_exc:
                        last_error = curl_exc
            else:
                continue
            break
        else:
            raise MovementDataError(f"涨幅榜第{page}页获取失败：{last_error}") from last_error
        data = payload.get("data") or {}
        return list(data.get("diff") or []), int(data.get("total") or 0)

    @staticmethod
    def _normalize(row: dict[str, Any]) -> dict[str, Any] | None:
        code = str(row.get("f12") or "")
        name = str(row.get("f14") or code)
        price = _number(row.get("f2"))
        pct = _number(row.get("f3"))
        prev_close = _number(row.get("f18"))
        open_price = _number(row.get("f17"))
        if len(code) != 6 or not code.isdigit() or price is None or pct is None or not prev_close:
            return None
        open_pct = ((open_price / prev_close) - 1) * 100 if open_price and prev_close else 0.0
        return {
            "code": code,
            "name": name,
            "industry": str(row.get("f100") or "未分类"),
            "price": price,
            "pct": pct,
            "open_pct": open_pct,
            "amount": _number(row.get("f6")) or 0.0,
            "market_value": _number(row.get("f20")) or 0.0,
            "float_market_value": _number(row.get("f21")) or 0.0,
        }

    def fetch_all_stocks(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        first_rows, total = self._page(1)
        page_count = self.pages or max(1, math.ceil(total / self.page_size))
        page_rows: dict[int, list[dict[str, Any]]] = {1: first_rows}
        failures: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="movement-fetch") as pool:
            futures = {pool.submit(self._page, page): page for page in range(2, page_count + 1)}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    raw_rows, _total = future.result()
                    page_rows[page] = raw_rows
                except MovementDataError as exc:
                    failures[page] = str(exc)
        if failures:
            retry_pages = sorted(failures)
            failures = {}
            for page in retry_pages:
                try:
                    raw_rows, _total = self._page(page)
                    page_rows[page] = raw_rows
                except MovementDataError as exc:
                    failures[page] = str(exc)
        if failures:
            failed_pages = ",".join(str(page) for page in sorted(failures)[:8])
            more = "..." if len(failures) > 8 else ""
            raise MovementDataError(f"全A扫描缺页（失败{len(failures)}/{page_count}页：{failed_pages}{more}）")
        for page in sorted(page_rows):
            for raw in page_rows[page]:
                item = self._normalize(raw)
                if item and item["code"] not in seen:
                    seen.add(item["code"])
                    rows.append(item)
        if not rows:
            raise MovementDataError("全A扫描没有返回有效股票")
        return rows

    def fetch_top_gainers(self) -> list[dict[str, Any]]:
        return self.fetch_all_stocks()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, self.pages + 1):
            for raw in self._page(page):
                item = self._normalize(raw)
                if item and item["code"] not in seen:
                    seen.add(item["code"])
                    rows.append(item)
        if not rows:
            raise MovementDataError("涨幅榜没有返回有效股票")
        return rows


class BigMovementMonitor:
    """Classify sudden lifts and steady movers from repeated top-gainer snapshots."""

    def __init__(self, events: queue.Queue[Any], provider: MovementMarketProvider | None = None):
        self.events = events
        self.provider = provider or MovementMarketProvider()
        self.running = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.history: dict[str, deque[tuple[datetime, float, float]]] = defaultdict(lambda: deque(maxlen=8))
        self.latest: dict[str, list[MovementCandidate]] = {"sudden": [], "steady": []}

    def start(self, interval_seconds: int = 60) -> None:
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(interval_seconds,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()

    def scan_once_async(self) -> None:
        threading.Thread(target=self._scan_and_publish, daemon=True).start()

    def _loop(self, interval_seconds: int) -> None:
        while not self._stop.is_set():
            self._scan_and_publish()
            self._stop.wait(max(15, interval_seconds))

    def _scan_and_publish(self) -> None:
        started = time.time()
        try:
            rows = self.provider.fetch_all_stocks()
            result = self.classify(rows)
            self.latest = result
            self.events.put(
                (
                    "movement_update",
                    {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "elapsed": time.time() - started,
                        "source_count": len(rows),
                        "sudden": result["sudden"],
                        "steady": result["steady"],
                    },
                )
            )
        except Exception as exc:
            self.events.put(("movement_error", str(exc)))

    def classify(self, rows: list[dict[str, Any]]) -> dict[str, list[MovementCandidate]]:
        now = datetime.now()
        sudden: list[MovementCandidate] = []
        steady: list[MovementCandidate] = []
        for row in rows:
            code = str(row["code"])
            name = str(row["name"])
            pct = float(row["pct"])
            amount = float(row["amount"])
            price = float(row["price"])
            open_pct = float(row["open_pct"])
            if _is_excluded(code, name) or amount < 20_000_000 or pct <= -1.0:
                continue
            series = self.history[code]
            previous_pct = series[-1][1] if series else pct
            delta = pct - previous_pct
            series.append((now, pct, amount))
            samples = len(series)

            if delta >= 0.7 and pct >= 1.0:
                risk = "中" if pct >= 7.0 else "低"
                sudden.append(
                    MovementCandidate(
                        "突然拉升", code, name, str(row["industry"]), price, pct, delta, amount,
                        open_pct, samples, f"本轮涨幅增加{delta:.2f}个百分点", risk
                    )
                )
                continue

            if samples >= 3:
                recent = list(series)[-4:]
                pcts = [item[1] for item in recent]
                steps = [pcts[i] - pcts[i - 1] for i in range(1, len(pcts))]
                total_delta = pcts[-1] - pcts[0]
                if (
                    pct >= 1.0
                    and total_delta >= 0.6
                    and all(step >= -0.05 for step in steps)
                    and max(steps) <= 0.8
                    and sum(steps) / len(steps) >= 0.15
                ):
                    risk = "中" if pct >= 6.5 else "低"
                    steady.append(
                        MovementCandidate(
                            "匀速上涨", code, name, str(row["industry"]), price, pct, total_delta,
                            amount, open_pct, samples, f"最近{len(recent)}次持续抬升{total_delta:.2f}个百分点", risk
                        )
                    )
        sudden.sort(key=lambda item: (-item.delta_pct, -item.amount))
        steady.sort(key=lambda item: (-item.delta_pct, -item.amount))
        return {"sudden": sudden[:30], "steady": steady[:30]}
