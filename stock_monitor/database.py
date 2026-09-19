from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Analysis, Bar, Position, Quote


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE REFERENCES stocks(code),
    buy_price REAL NOT NULL CHECK(buy_price > 0),
    buy_date TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
    stop_loss REAL,
    target_return REAL,
    highest_price REAL NOT NULL DEFAULT 0,
    last_price REAL NOT NULL DEFAULT 0,
    risk_level INTEGER NOT NULL DEFAULT 1 CHECK(risk_level BETWEEN 1 AND 5),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES stocks(code),
    timeframe TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL, amount REAL NOT NULL DEFAULT 0,
    UNIQUE(code, timeframe, bar_time)
);

CREATE TABLE IF NOT EXISTS indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES stocks(code),
    timeframe TEXT NOT NULL,
    calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    boll_mid REAL, boll_upper REAL, boll_lower REAL,
    macd_dif REAL, macd_dea REAL, macd_hist REAL,
    kdj_k REAL, kdj_d REAL, kdj_j REAL,
    volume_ma5 REAL, volume_ma10 REAL,
    state_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES stocks(code),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    risk_level INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES stocks(code),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    old_risk INTEGER NOT NULL,
    new_risk INTEGER NOT NULL,
    price REAL NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    acknowledged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notification_state (
    code TEXT NOT NULL,
    event_type TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT,
    last_value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(code, event_type)
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    event_type TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auction_snapshots (
    trading_day TEXT NOT NULL,
    sample_label TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    industry TEXT NOT NULL DEFAULT '',
    auction_price REAL,
    auction_pct REAL,
    auction_amount REAL,
    prev_close REAL,
    open_price REAL,
    market_value REAL,
    float_market_value REAL,
    PRIMARY KEY(trading_day, sample_label, code)
);

CREATE INDEX IF NOT EXISTS idx_auction_snapshots_day_code
ON auction_snapshots(trading_day, code, sample_label);

CREATE TABLE IF NOT EXISTS auction_decisions (
    trading_day TEXT NOT NULL,
    code TEXT NOT NULL,
    rank_no INTEGER NOT NULL,
    action TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(trading_day, code)
);

CREATE TABLE IF NOT EXISTS auction_sample_runs (
    trading_day TEXT NOT NULL,
    sample_label TEXT NOT NULL,
    target_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    expected_total INTEGER NOT NULL DEFAULT 0,
    raw_count INTEGER NOT NULL DEFAULT 0,
    normalized_count INTEGER NOT NULL DEFAULT 0,
    coverage REAL NOT NULL DEFAULT 0,
    page_span_seconds REAL NOT NULL DEFAULT 0,
    start_delay_seconds REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'unknown',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(trading_day, sample_label)
);

CREATE TABLE IF NOT EXISTS auction_outcomes (
    trading_day TEXT NOT NULL,
    code TEXT NOT NULL,
    observation_label TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price REAL NOT NULL,
    high REAL NOT NULL DEFAULT 0,
    low REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0,
    source_kind TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY(trading_day, code, observation_label)
);
"""

DEFAULT_SETTINGS = {
    "auction_data_source": ("eastmoney", "竞价数据源：eastmoney/infoway"),
    "secret_infoway_key": ("", "Infoway API Key（DPAPI加密）"),
    "infoway_request_interval": ("1.05", "Infoway批量请求间隔秒数"),
    "poll_seconds": ("60", "交易时段刷新间隔（秒）"),
    "off_hours_poll_seconds": ("1800", "非交易时段刷新间隔（秒）"),
    "profit_guard_start": ("5", "进入利润保护的收益率（%）"),
    "profit_tier_1": ("10", "第一档最高浮盈（%）"),
    "drawdown_tier_1": ("5", "第一档回撤提醒（%）"),
    "profit_tier_2": ("20", "第二档最高浮盈（%）"),
    "drawdown_tier_2": ("7", "第二档回撤提醒（%）"),
    "profit_tier_3": ("30", "第三档最高浮盈（%）"),
    "drawdown_tier_3": ("8", "第三档回撤提醒（%）"),
    "startup_enabled": ("0", "是否开机启动"),
    "monitor_enabled": ("0", "是否自动开始监控"),
    "notify_windows": ("1", "启用Windows通知"),
    "notify_mobile": ("0", "启用手机推送服务"),
    "notify_mobile_service": ("bark", "手机推送服务：bark/pushplus/serverchan"),
    "notify_email": ("0", "启用邮箱通知"),
    "notify_telegram": ("0", "启用Telegram通知"),
    "notify_wecom": ("0", "启用企业微信机器人"),
    "notify_cooldown_minutes": ("30", "同类型通知冷却分钟数"),
    "notify_event_risk_change": ("1", "重要风险等级变化"),
    "notify_event_support_break": ("1", "跌破支撑"),
    "notify_event_resistance_break": ("1", "突破压力"),
    "notify_event_macd_death": ("1", "MACD死叉"),
    "notify_event_kdj_death": ("1", "KDJ死叉"),
    "notify_event_profit_drawdown": ("1", "利润回撤"),
    "notify_event_below_cost": ("1", "跌破成本"),
    "notify_event_stop_loss": ("1", "达到止损线"),
    "notify_event_target_profit": ("1", "达到止盈线"),
    "notify_event_stall": ("1", "放量滞涨"),
    "notify_event_false_break": ("1", "假突破"),
    "secret_bark_key": ("", "Bark Key（DPAPI加密）"),
    "bark_server": ("https://api.day.app", "Bark服务器"),
    "secret_pushplus_token": ("", "PushPlus Token（DPAPI加密）"),
    "secret_serverchan_key": ("", "Server酱SendKey（DPAPI加密）"),
    "secret_telegram_token": ("", "Telegram Bot Token（DPAPI加密）"),
    "telegram_chat_id": ("", "Telegram Chat ID"),
    "secret_wecom_webhook": ("", "企业微信机器人Webhook（DPAPI加密）"),
    "smtp_host": ("", "SMTP服务器"),
    "smtp_port": ("465", "SMTP端口"),
    "smtp_user": ("", "SMTP用户名"),
    "secret_smtp_password": ("", "SMTP密码（DPAPI加密）"),
    "email_to": ("", "收件邮箱"),
    "smtp_ssl": ("1", "使用SMTP SSL"),
    "web_enabled": ("0", "启用手机只读网页"),
    "web_host": ("0.0.0.0", "网页监听地址"),
    "web_port": ("8765", "网页端口"),
    "web_password_hash": ("", "网页登录密码哈希"),
    "secret_web_token": ("", "网页API Token（DPAPI加密）"),
    "auction_enabled": ("1", "自动运行9:15-9:25竞价采样"),
    "auction_pool_enabled": ("1", "Infoway免费版启用盘前核心池"),
    "auction_pool_max_size": ("500", "9:20后最多跟踪股票数"),
    "auction_pool_min_size": ("80", "盘前核心池安全下限"),
    "auction_pool_max_per_sector": ("5", "每个板块最多进入核心池数"),
    "auction_pool_min_sector_positive": ("3", "入池板块最少正竞价数"),
    "auction_pool_min_price": ("3", "入池最低股价"),
    "auction_preferred_min_price": ("8", "优先价格带下限"),
    "auction_preferred_max_price": ("45", "优先价格带上限"),
    "auction_pool_min_pct": ("0.30", "入池最低竞价涨幅"),
    "auction_pool_max_pct": ("6.50", "入池最高竞价涨幅"),
    "auction_pool_min_amount": ("5000000", "入池最低竞价额"),
    "auction_min_amount": ("20000000", "可挂入最低竞价成交额"),
    "auction_high_price": ("60", "默认剔除的高价股阈值"),
    "auction_min_retention": ("0.70", "最低锁单留存率"),
    "auction_late_drop": ("0.50", "9:24:30到9:25明显下降阈值（百分点）"),
    "auction_max_drawdown": ("2.00", "竞价最高涨幅最大回撤（百分点）"),
    "auction_include_chinext": ("0", "竞价筛选包含创业板"),
    "auction_include_star": ("0", "竞价筛选包含科创板"),
    "auction_include_bse": ("0", "竞价筛选包含北交所"),
    "auction_include_st": ("0", "竞价筛选包含ST和退市整理股票"),
    "auction_include_high_price": ("0", "竞价筛选包含高价股"),
    "auction_min_market_coverage": ("0.98", "竞价全市场最低覆盖率"),
    "auction_max_snapshot_span": ("12", "单次全市场快照最大跨页时差（秒）"),
    "auction_max_start_delay": ("3", "采样点最大启动延迟（秒）"),
    "auction_min_float_amount_ratio": ("0.0005", "竞价额占流通市值最低比例"),
    "auction_min_market_positive_ratio": ("0.35", "允许可挂入的全市场最低正竞价比例"),
    "auction_max_orderable": ("1", "每日最多可挂入候选数"),
    "auction_max_per_sector": ("1", "每个板块最多可挂入候选数"),
    "auction_track_outcomes": ("1", "跟踪开盘后表现用于规则验证"),
    "auction_require_crosscheck": ("1", "可挂入候选必须通过第二行情源校验"),
    "auction_max_crosscheck_age": ("20", "第二行情源最大时间偏差（秒）"),
    "auction_profile_name": ("保守试验版", "当前竞价筛选参数方案"),
}


def exchange_for(code: str) -> str:
    return "SSE" if code.startswith(("5", "6", "9")) else "SZSE"


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def initialize(self) -> None:
        with self._lock, self.connect() as con:
            con.executescript(SCHEMA)
            auction_columns = {row["name"] for row in con.execute("PRAGMA table_info(auction_snapshots)")}
            if "float_market_value" not in auction_columns:
                con.execute("ALTER TABLE auction_snapshots ADD COLUMN float_market_value REAL")
            run_columns = {row["name"] for row in con.execute("PRAGMA table_info(auction_sample_runs)")}
            if "source_kind" not in run_columns:
                con.execute("ALTER TABLE auction_sample_runs ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'unknown'")
            outcome_columns = {row["name"] for row in con.execute("PRAGMA table_info(auction_outcomes)")}
            if "source_kind" not in outcome_columns:
                con.execute("ALTER TABLE auction_outcomes ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'unknown'")
            con.executemany(
                "INSERT OR IGNORE INTO settings(key,value,description) VALUES(?,?,?)",
                [(key, value, desc) for key, (value, desc) in DEFAULT_SETTINGS.items()],
            )

    def add_position(self, position: Position) -> int:
        with self._lock, self.connect() as con:
            con.execute(
                "INSERT INTO stocks(code,name,exchange) VALUES(?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, updated_at=CURRENT_TIMESTAMP",
                (position.code, position.name, exchange_for(position.code)),
            )
            cur = con.execute(
                """INSERT INTO positions(code,buy_price,buy_date,quantity,stop_loss,target_return,
                   highest_price,last_price,risk_level,active) VALUES(?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(code) DO UPDATE SET buy_price=excluded.buy_price,
                   buy_date=excluded.buy_date, quantity=excluded.quantity,
                   stop_loss=excluded.stop_loss, target_return=excluded.target_return,
                   active=1, updated_at=CURRENT_TIMESTAMP RETURNING id""",
                (
                    position.code, position.buy_price, position.buy_date, position.quantity,
                    position.stop_loss, position.target_return,
                    max(position.highest_price, position.buy_price), position.last_price,
                    position.risk_level,
                ),
            )
            return int(cur.fetchone()[0])

    def update_position(self, position: Position) -> None:
        if position.id is None:
            raise ValueError("持仓ID不能为空")
        with self._lock, self.connect() as con:
            con.execute(
                "INSERT INTO stocks(code,name,exchange) VALUES(?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, updated_at=CURRENT_TIMESTAMP",
                (position.code, position.name, exchange_for(position.code)),
            )
            con.execute(
                """UPDATE positions SET buy_price=?, buy_date=?, quantity=?,
                   stop_loss=?, target_return=?,
                   highest_price=MAX(highest_price, ?),
                   active=1, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    position.buy_price,
                    position.buy_date,
                    position.quantity,
                    position.stop_loss,
                    position.target_return,
                    position.buy_price,
                    position.id,
                ),
            )

    def list_positions(self, active_only: bool = True) -> list[Position]:
        where = "WHERE p.active=1" if active_only else ""
        with self.connect() as con:
            rows = con.execute(
                f"""SELECT p.*, s.name FROM positions p JOIN stocks s ON s.code=p.code
                    {where} ORDER BY p.risk_level DESC, p.updated_at DESC"""
            ).fetchall()
        return [
            Position(
                id=r["id"], code=r["code"], name=r["name"], buy_price=r["buy_price"],
                buy_date=r["buy_date"], quantity=r["quantity"], stop_loss=r["stop_loss"],
                target_return=r["target_return"], highest_price=r["highest_price"],
                last_price=r["last_price"], risk_level=r["risk_level"], active=bool(r["active"]),
            )
            for r in rows
        ]

    def delete_position(self, position_id: int) -> None:
        with self._lock, self.connect() as con:
            con.execute("UPDATE positions SET active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (position_id,))

    def update_position_state(self, position_id: int, price: float, highest: float, risk: int) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                "UPDATE positions SET last_price=?, highest_price=?, risk_level=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (price, highest, risk, position_id),
            )

    def save_quote(self, quote: Quote) -> None:
        bar = Bar(quote.timestamp, quote.open, quote.price, quote.high, quote.low, quote.volume, quote.amount, quote.pct_change)
        self.save_bars(quote.code, "quote", [bar])

    def save_bars(self, code: str, timeframe: str, bars: list[Bar]) -> None:
        if not bars:
            return
        with self._lock, self.connect() as con:
            con.executemany(
                """INSERT INTO price_data(code,timeframe,bar_time,open,high,low,close,volume,amount)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code,timeframe,bar_time) DO UPDATE SET
                   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                   volume=excluded.volume,amount=excluded.amount""",
                [(code, timeframe, b.timestamp, b.open, b.high, b.low, b.close, b.volume, b.amount) for b in bars],
            )

    def save_analysis(self, position: Position, analysis: Analysis, old_risk: int) -> None:
        state = dict(analysis.indicators)
        state["_analysis"] = {
            "price": analysis.price,
            "return_pct": analysis.return_pct,
            "highest_profit_pct": analysis.highest_profit_pct,
            "drawdown_pct": analysis.drawdown_pct,
            "risk_level": analysis.risk_level,
            "reasons": analysis.reasons,
            "trend": analysis.trend,
            "boll_state": analysis.boll_state,
            "macd_state": analysis.macd_state,
            "kdj_state": analysis.kdj_state,
            "volume_state": analysis.volume_state,
            "support": analysis.support,
            "major_support": analysis.major_support,
            "resistance": analysis.resistance,
            "major_resistance": analysis.major_resistance,
            "explanation": analysis.explanation,
            "position_advice": analysis.indicators.get("position_advice"),
            "position_advice_detail": analysis.indicators.get("position_advice_detail"),
        }
        payload = json.dumps(state, ensure_ascii=False)
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT INTO indicators(code,timeframe,boll_mid,boll_upper,boll_lower,
                   macd_dif,macd_dea,macd_hist,kdj_k,kdj_d,kdj_j,volume_ma5,volume_ma10,state_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    position.code, "combined", analysis.indicators.get("boll_mid"),
                    analysis.indicators.get("boll_upper"), analysis.indicators.get("boll_lower"),
                    analysis.indicators.get("macd_dif"), analysis.indicators.get("macd_dea"),
                    analysis.indicators.get("macd_hist"), analysis.indicators.get("kdj_k"),
                    analysis.indicators.get("kdj_d"), analysis.indicators.get("kdj_j"),
                    analysis.indicators.get("volume_ma5"), analysis.indicators.get("volume_ma10"), payload,
                ),
            )
            for event in analysis.events:
                con.execute(
                    """INSERT INTO signals(code,risk_level,signal_type,reason,snapshot_json)
                       SELECT ?,?,?,?,? WHERE NOT EXISTS (
                         SELECT 1 FROM signals WHERE code=? AND reason=?
                         AND created_at >= datetime('now','-1 hour'))""",
                    (position.code, analysis.risk_level, "technical", event, payload, position.code, event),
                )
            if old_risk != analysis.risk_level:
                con.execute(
                    "INSERT INTO alerts(code,old_risk,new_risk,price,reason,snapshot_json) VALUES(?,?,?,?,?,?)",
                    (position.code, old_risk, analysis.risk_level, analysis.price, "；".join(analysis.reasons), payload),
                )

    def recent_alerts(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                """SELECT a.*,s.name FROM alerts a JOIN stocks s ON s.code=a.code
                   ORDER BY a.id DESC LIMIT ?""", (limit,),
            ).fetchall()

    def get_settings(self) -> dict[str, str]:
        with self.connect() as con:
            return {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def notification_state(self, code: str, event_type: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM notification_state WHERE code=? AND event_type=?",
                (code, event_type),
            ).fetchone()

    def set_notification_state(
        self, code: str, event_type: str, active: bool, value: str = "", sent: bool = False
    ) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT INTO notification_state(code,event_type,active,last_sent_at,last_value)
                   VALUES(?,?,?,CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,?)
                   ON CONFLICT(code,event_type) DO UPDATE SET active=excluded.active,
                   last_value=excluded.last_value, updated_at=CURRENT_TIMESTAMP,
                   last_sent_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE notification_state.last_sent_at END""",
                (code, event_type, int(active), int(sent), value, int(sent)),
            )

    def log_delivery(self, code: str, event_type: str, channel: str, status: str, error: str = "") -> None:
        with self._lock, self.connect() as con:
            con.execute(
                "INSERT INTO notification_deliveries(code,event_type,channel,status,error) VALUES(?,?,?,?,?)",
                (code, event_type, channel, status, error[:500]),
            )

    def latest_indicator(self, code: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM indicators WHERE code=? ORDER BY id DESC LIMIT 1", (code,)
            ).fetchone()

    def save_auction_snapshot(self, trading_day: str, sample_label: str, sampled_at: str, rows: list[dict]) -> None:
        if not rows:
            return
        values = [
            (
                trading_day, sample_label, sampled_at, str(row.get("code") or ""),
                str(row.get("name") or ""), str(row.get("industry") or "未分类"),
                row.get("auction_price"), row.get("auction_pct"), row.get("auction_amount"),
                row.get("prev_close"), row.get("open_price"), row.get("market_value"),
                row.get("float_market_value"),
            )
            for row in rows if row.get("code")
        ]
        with self._lock, self.connect() as con:
            con.executemany(
                """INSERT INTO auction_snapshots(
                   trading_day,sample_label,sampled_at,code,name,industry,auction_price,
                   auction_pct,auction_amount,prev_close,open_price,market_value,float_market_value)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trading_day,sample_label,code) DO UPDATE SET
                   sampled_at=excluded.sampled_at,name=excluded.name,industry=excluded.industry,
                   auction_price=excluded.auction_price,auction_pct=excluded.auction_pct,
                   auction_amount=excluded.auction_amount,prev_close=excluded.prev_close,
                   open_price=excluded.open_price,market_value=excluded.market_value,
                   float_market_value=excluded.float_market_value""",
                values,
            )

    def auction_snapshots(self, trading_day: str) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                """SELECT * FROM auction_snapshots WHERE trading_day=?
                   ORDER BY sampled_at, code""", (trading_day,),
            ).fetchall()

    def save_auction_decisions(self, trading_day: str, rows: list[dict]) -> None:
        with self._lock, self.connect() as con:
            con.execute("DELETE FROM auction_decisions WHERE trading_day=?", (trading_day,))
            con.executemany(
                """INSERT INTO auction_decisions(trading_day,code,rank_no,action,score,payload_json)
                   VALUES(?,?,?,?,?,?)""",
                [
                    (
                        trading_day, str(row.get("code") or ""), int(row.get("rank") or index),
                        str(row.get("action") or "放弃"), float(row.get("score") or 0),
                        json.dumps(row, ensure_ascii=False),
                    )
                    for index, row in enumerate(rows, 1)
                ],
            )

    def auction_decisions(self, trading_day: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT payload_json FROM auction_decisions WHERE trading_day=?
                   ORDER BY rank_no""", (trading_day,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                result.append(json.loads(row["payload_json"]))
            except json.JSONDecodeError:
                continue
        return result

    def update_auction_decision_payload(self, trading_day: str, row: dict) -> None:
        code = str(row.get("code") or "")
        if not code:
            raise ValueError("竞价候选缺少股票代码")
        with self._lock, self.connect() as con:
            cursor = con.execute(
                """UPDATE auction_decisions SET payload_json=?, action=?, score=?
                   WHERE trading_day=? AND code=?""",
                (
                    json.dumps(row, ensure_ascii=False), str(row.get("action") or "放弃"),
                    float(row.get("score") or 0), trading_day, code,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"未找到{trading_day}的竞价候选 {code}")

    def save_auction_sample_run(self, payload: dict) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT INTO auction_sample_runs(
                   trading_day,sample_label,target_at,started_at,finished_at,expected_total,
                   raw_count,normalized_count,coverage,page_span_seconds,start_delay_seconds,status,source_kind,error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trading_day,sample_label) DO UPDATE SET
                   target_at=excluded.target_at,started_at=excluded.started_at,finished_at=excluded.finished_at,
                   expected_total=excluded.expected_total,raw_count=excluded.raw_count,
                   normalized_count=excluded.normalized_count,coverage=excluded.coverage,
                   page_span_seconds=excluded.page_span_seconds,start_delay_seconds=excluded.start_delay_seconds,
                   status=excluded.status,source_kind=excluded.source_kind,error=excluded.error""",
                (
                    payload.get("trading_day"), payload.get("sample_label"), payload.get("target_at"),
                    payload.get("started_at"), payload.get("finished_at"), int(payload.get("expected_total") or 0),
                    int(payload.get("raw_count") or 0), int(payload.get("normalized_count") or 0),
                    float(payload.get("coverage") or 0), float(payload.get("page_span_seconds") or 0),
                    float(payload.get("start_delay_seconds") or 0), payload.get("status") or "failed",
                    str(payload.get("source_kind") or "unknown"),
                    str(payload.get("error") or "")[:500],
                ),
            )

    def auction_sample_runs(self, trading_day: str) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM auction_sample_runs WHERE trading_day=? ORDER BY target_at",
                (trading_day,),
            ).fetchall()

    def save_auction_outcomes(self, trading_day: str, label: str, observed_at: str, rows: list[dict]) -> None:
        if not rows:
            return
        with self._lock, self.connect() as con:
            con.executemany(
                """INSERT INTO auction_outcomes(
                   trading_day,code,observation_label,observed_at,price,high,low,amount,source_kind)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trading_day,code,observation_label) DO UPDATE SET
                   observed_at=excluded.observed_at,price=excluded.price,high=excluded.high,
                   low=excluded.low,amount=excluded.amount,source_kind=excluded.source_kind""",
                [
                    (
                        trading_day, str(row.get("code") or ""), label, observed_at,
                        float(row.get("auction_price") or 0), float(row.get("high") or 0),
                        float(row.get("low") or 0), float(row.get("auction_amount") or 0),
                        str(row.get("source_kind") or "unknown"),
                    )
                    for row in rows if row.get("code") and row.get("auction_price")
                ],
            )

    def auction_outcome_labels(self, trading_day: str) -> set[str]:
        with self.connect() as con:
            return {
                str(row["observation_label"])
                for row in con.execute(
                    "SELECT DISTINCT observation_label FROM auction_outcomes WHERE trading_day=?",
                    (trading_day,),
                )
            }

    def auction_days(self) -> list[str]:
        with self.connect() as con:
            return [str(row["trading_day"]) for row in con.execute(
                "SELECT DISTINCT trading_day FROM auction_snapshots ORDER BY trading_day"
            )]

    def auction_outcomes(self, trading_day: str) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM auction_outcomes WHERE trading_day=? ORDER BY code,observation_label",
                (trading_day,),
            ).fetchall()

    def auction_performance(self, limit_days: int = 30) -> dict[str, float | int]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT d.trading_day,d.code,d.payload_json,o.observation_label,o.price,o.high
                   FROM auction_decisions d JOIN auction_outcomes o
                   ON o.trading_day=d.trading_day AND o.code=d.code
                   WHERE d.action='可挂入' AND d.trading_day >= date('now', ?)
                   ORDER BY d.trading_day,d.code""",
                (f"-{max(1, limit_days)} days",),
            ).fetchall()
        grouped: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (str(row["trading_day"]), str(row["code"]))
            item = grouped.setdefault(key, {"lock": 0.0, "suggested": 0.0, "observations": {}})
            if not item["lock"]:
                try:
                    payload = json.loads(row["payload_json"])
                    item["lock"] = float(payload.get("lock_price") or 0)
                    item["suggested"] = float(payload.get("suggested_price") or 0)
                except json.JSONDecodeError:
                    pass
            item["observations"][str(row["observation_label"])] = {"price": float(row["price"]), "high": float(row["high"])}
        signal_count = len(grouped)
        filled = []
        for item in grouped.values():
            open_observation = item["observations"].get("09:30")
            if open_observation and item["suggested"] > 0 and open_observation["price"] <= item["suggested"]:
                item["simulated_fill"] = open_observation["price"]
                filled.append(item)
        sample_count = len(filled)
        samples_935 = sum(1 for item in filled if "09:35" in item["observations"])
        samples_close = sum(1 for item in filled if "15:00" in item["observations"])
        positive_935 = positive_close = hit_one_pct = 0
        for item in filled:
            entry = item["simulated_fill"]
            observations = item["observations"]
            if observations.get("09:35", {}).get("price", 0) > entry:
                positive_935 += 1
            if observations.get("15:00", {}).get("price", 0) > entry:
                positive_close += 1
            if max((obs.get("high", 0) for obs in observations.values()), default=0) >= entry * 1.01:
                hit_one_pct += 1
        return {
            "signals": signal_count,
            "samples": sample_count,
            "simulated_fills": sample_count,
            "fill_rate": sample_count / signal_count if signal_count else 0.0,
            "samples_935": samples_935,
            "samples_close": samples_close,
            "positive_935": positive_935,
            "positive_close": positive_close,
            "hit_one_pct": hit_one_pct,
        }

    def prune_auction_history(self, keep_days: int = 120) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                "DELETE FROM auction_snapshots WHERE trading_day < date('now', ?)",
                (f"-{max(1, keep_days)} days",),
            )
            con.execute(
                "DELETE FROM auction_decisions WHERE trading_day < date('now', ?)",
                (f"-{max(1, keep_days)} days",),
            )
            con.execute(
                "DELETE FROM auction_sample_runs WHERE trading_day < date('now', ?)",
                (f"-{max(1, keep_days)} days",),
            )
            con.execute(
                "DELETE FROM auction_outcomes WHERE trading_day < date('now', ?)",
                (f"-{max(1, keep_days)} days",),
            )
