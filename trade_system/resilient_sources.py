# -*- coding: utf-8 -*-
"""Shared, source-bound market-data acquisition and receipt reuse.

Provider plans preserve product identity and priority. A valid cache receipt
avoids another request; failed refreshes may return explicitly stale data.
Consumers must reject stale or incomplete receipts where current facts are
required. Empty responses are not certified zero observations.

Bars reuse verified session coverage; financial reports reuse one revision's
period subset. Pool products remain separate receipts with 60-second freshness.
Derived sentiment reads those inputs without becoming another fact writer.

Use get(datatype, code=None, **kwargs) for acquisition, warm(codes) for bounded
prefetch, and status() for local cache/health information. Transport and source
budgets are shared with the other retained collectors.
"""
from __future__ import annotations
import logging
import os, json, time, sqlite3, threading, datetime
from concurrent.futures import ThreadPoolExecutor
from trade_system.http_transport import request_budget, request_deadline


logger = logging.getLogger(__name__)

# ---- 路径 / 环境 ----
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(WORKSPACE, ".stock_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
DB_PATH = os.path.join(CACHE_DIR, "resilient.db")
LOCAL_MODE = os.environ.get("STOCK_DATA_LOCAL") == "1"

# ---- 低层源（来自 stock_data.py，已含多源与超时） ----
from trade_system.adapters.kline_sources import _from_baostock, _from_pytdx, _from_tencent, _from_sina, _from_eastmoney, _from_baidu, _from_xiaodefa, _from_xiaodefa_moneyflow, _from_xiaodefa_sector_flow, _from_xiaodefa_basic, _norm_code, _norm_date, XIAODEFA_TOKEN, _xiaodefa_query, _from_pytdx_minutes
from trade_system.adapters.sina_sources import _from_sina_fund_flow, get_financial_statements, _from_sina_option_tquote, _from_sina_option_greeks
from trade_system.quote_transport import _from_tencent_valuation, _from_tencent_bid_ask
from trade_system.adapters.ths_sources import _from_ths_northbound, _from_ths_hot_reason, _from_ths_eps_forecast, _from_ths_limit_up, _from_ths_hot_list, _from_ths_margin_trading, _from_ths_holder_num
from trade_system.adapters.eastmoney_dc import get_financials, get_fund_flow, _from_em_dragon_tiger, _from_em_dragon_tiger_daily, _from_em_margin, _from_em_holder, _from_em_lockup, _from_em_dividend, _from_em_block_trade, _from_em_fund_flow_120d, _from_em_stock_info, _from_em_industry_rank, _from_em_sector_flow, _from_em_zt_pool, _from_em_zb_pool, _from_em_dt_pool, _from_em_yzt_pool, _from_em_reports, _from_em_stock_news, _from_em_flash, _from_cls_telegraph, _from_em_hot_rank, _from_em_hot_concept, _from_em_trends, _from_em_all_stocks, _from_em_northbound, _from_em_index_kline, _from_em_index_spot, _from_em_etf_kline, _from_em_etf_info, _from_em_cb_kline, _from_em_cb_quote, _from_em_disclosure, _from_em_northbound_hist, _from_em_bid_ask, _from_em_irm, _from_em_announcements, _from_em_option_tquote, _from_em_ipo, _from_em_macro
from trade_system.adapters.cninfo_sources import _from_cninfo_announcements, _from_cninfo_irm, _from_cninfo_dividend, _from_cninfo_lockup, _from_cninfo_block_trade
from trade_system.host_limiter import shared_host_limiter

# =====================================================================
# 1) 持久化（缓存 + 健康度，共用一个 SQLite）
# =====================================================================
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("""CREATE TABLE IF NOT EXISTS cache(
    key TEXT PRIMARY KEY, value TEXT, ts REAL)""")
_conn.execute("""CREATE TABLE IF NOT EXISTS health(
    source TEXT, datatype TEXT, success INT DEFAULT 0, fail INT DEFAULT 0,
    streak_fail INT DEFAULT 0, last_ok REAL DEFAULT 0, last_fail REAL DEFAULT 0,
    PRIMARY KEY(source, datatype))""")
_conn.execute("""CREATE TABLE IF NOT EXISTS health_event(
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, datatype TEXT, ok INTEGER NOT NULL,
    latency REAL DEFAULT 0, ts REAL NOT NULL)""")
_conn.execute("CREATE INDEX IF NOT EXISTS ix_health_event_lookup ON health_event(source, datatype, ts)")
_conn.commit()


class CacheStore:
    """Request-bound receipts; source identity and receipt times are preserved."""

    def __init__(self, db):
        self.db = db
        # RLock: record()/score() hold the lock and call _get(), which locks
        # again — a plain Lock would self-deadlock.
        self.lock = threading.RLock()

    def get(self, key):
        with self.lock:
            r = self.db.execute("SELECT value,ts FROM cache WHERE key=?", (key,)).fetchone()
        if r:
            try:
                return json.loads(r[0]), r[1]
            except Exception:
                return None, 0
        return None, 0

    def put(self, key, value):
        with self.lock:
            self.db.execute(
                """INSERT INTO cache(key,value,ts) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts""",
                (key, json.dumps(value, ensure_ascii=False), time.time()))
            self.db.commit()

    def matching(self, prefix):
        with self.lock:
            keys = self.db.execute(
                "SELECT key FROM cache WHERE key>=? AND key<? ORDER BY ts DESC LIMIT 64",
                (prefix, prefix + "\uffff")).fetchall()
        return [(key, *self.get(key)) for (key,) in keys]

    def stat(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]


class HealthRegistry:
    """(源 × 数据类型) 健康度，落盘；提供排序/冷却/记录。"""

    def __init__(self, db):
        self.db = db
        # RLock: record()/score() hold the lock and call _get(), which locks
        # again — a plain Lock would self-deadlock.
        self.lock = threading.RLock()

    def _get(self, source, datatype):
        with self.lock:
            r = self.db.execute(
                "SELECT success,fail,streak_fail,last_ok,last_fail FROM health WHERE source=? AND datatype=?",
                (source, datatype)).fetchone()
        if r:
            return {"success": r[0], "fail": r[1], "streak_fail": r[2],
                    "last_ok": r[3], "last_fail": r[4]}
        return {"success": 0, "fail": 0, "streak_fail": 0, "last_ok": 0.0, "last_fail": 0.0}

    def record(self, source, datatype, ok, latency=0.0):
        with self.lock:
            now = time.time()
            s = self._get(source, datatype)
            if ok:
                s["success"] += 1
                s["streak_fail"] = 0
                s["last_ok"] = now
            else:
                s["fail"] += 1
                s["streak_fail"] += 1
                s["last_fail"] = now
            self.db.execute(
                "INSERT INTO health_event(source,datatype,ok,latency,ts) VALUES(?,?,?,?,?)",
                (source, datatype, int(bool(ok)), float(latency or 0), now),
            )
            # Keep a bounded rolling history.  The cumulative health columns
            # remain useful for diagnostics, while ranking uses this window.
            self.db.execute("DELETE FROM health_event WHERE ts < ?", (now - 7 * 86400,))
            self.db.execute(
                """INSERT INTO health(source,datatype,success,fail,streak_fail,last_ok,last_fail)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(source,datatype) DO UPDATE SET
                     success=excluded.success, fail=excluded.fail,
                     streak_fail=excluded.streak_fail, last_ok=excluded.last_ok, last_fail=excluded.last_fail""",
                (source, datatype, s["success"], s["fail"], s["streak_fail"], s["last_ok"], s["last_fail"]))
            self.db.commit()

    def score(self, source, datatype):
        now = time.time()
        with self.lock:
            s = self._get(source, datatype)
            recent = self.db.execute(
                "SELECT coalesce(sum(ok),0), count(*), max(ts) FROM health_event "
                "WHERE source=? AND datatype=? AND ts>=?",
                (source, datatype, now - 3600),
            ).fetchone()
        success, total, last_event = recent or (0, 0, 0)
        if total:
            base = float(success) / float(total)
            recency = max(0.0, 1 - (now - float(last_event or now)) / 3600)
            return base * 0.8 + recency * 0.2
        # No recent observations: decay old history toward neutral instead of
        # allowing a months-old success ratio to dominate source ordering.
        age = (now - s["last_ok"]) if s["last_ok"] else 7 * 86400
        decay = max(0.0, 1 - age / (7 * 86400))
        cumulative = s["success"] + s["fail"]
        old_base = s["success"] / cumulative if cumulative else 0.5
        return old_base * decay + 0.5 * (1 - decay)

    def is_cooldown(self, source, datatype):
        s = self._get(source, datatype)
        now = time.time()
        # 连续失败≥3 且 最近 120s 无成功 → 冷却 300s（给对端恢复时间）
        if s["streak_fail"] >= 3 and (now - s["last_ok"]) > 120:
            return (now - s["last_fail"]) < 300
        return False

    def order(self, sources, datatype):
        return sorted(sources, key=lambda x: self.score(x[0], datatype), reverse=True)

    def all_rows(self):
        with self.lock:
            return self.db.execute(
                "SELECT source,datatype,success,fail,streak_fail,last_ok FROM health").fetchall()

    def force_cooldown(self, datatype=None):
        """测试用：把所有（或某类）源置为冷却，模拟"全部实时源阵亡"。
        会覆盖该类【所有】源（含从未探测、健康表里尚无记录的源），使模拟更严谨。"""
        with self.lock:
            now = time.time()
            if datatype and datatype in SOURCE_PLAN:
                plan = SOURCE_PLAN[datatype]
                sources = plan("600519") if callable(plan) else list(plan)
                for name, _ in sources:
                    self.db.execute(
                        """INSERT INTO health(source,datatype,success,fail,streak_fail,last_ok,last_fail)
                           VALUES(?,?,0,0,99,0,?) ON CONFLICT(source,datatype)
                           DO UPDATE SET streak_fail=99, last_ok=0, last_fail=?""",
                        (name, datatype, now, now))
            else:
                self.db.execute("UPDATE health SET streak_fail=99, last_ok=0, last_fail=?", (now,))
            self.db.commit()


cache = CacheStore(_conn)
health = HealthRegistry(_conn)


# =====================================================================
# 2) 礼貌限流（降低被对方限频/封 IP 的概率）
# =====================================================================
class RateLimiter:
    def __init__(self):
        # 每源最小请求间隔（秒）——刻意压低 burst
        self.min = {"baostock": 0.30, "pytdx": 0.08, "tencent": 0.12, "sina": 0.18,
                    "eastmoney": 0.18, "xiaodefa": 0.0, "baidu": 0.30,
                    "ths": 0.20, "cninfo": 0.20, "cls": 0.20,
                    "sina_option": 0.15, "local": 0.0}
        self.sem = threading.BoundedSemaphore(4)

    def acquire(self, source, *, deadline):
        mi = self.min.get(source, 0.15)
        remaining = deadline-time.monotonic()
        if remaining <= 0 or not self.sem.acquire(timeout=remaining):
            raise TimeoutError('source concurrency deadline exhausted')
        try:
            shared_host_limiter.acquire(source, mi, deadline=deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError('source rate limit deadline exhausted')
        except BaseException:
            self.sem.release()
            raise
        return self

    def release(self, source):
        self.sem.release()


rate = RateLimiter()


def _call(fn, timeout, pending=None):
    """One attempt; an unfinished worker remains owned by the caller."""
    if timeout <= 0:
        return None
    from trade_system.http_transport import request_deadline
    deadline = time.monotonic()+timeout
    def run():
        token = request_deadline.set(deadline)
        try:
            return fn()
        finally:
            request_deadline.reset(token)
    executor = ThreadPoolExecutor(max_workers=1)
    fut = None
    try:
        fut = executor.submit(run)
        return fut.result(timeout=max(0, deadline-time.monotonic()))
    except Exception:
        if pending is not None and fut is not None and not fut.done():
            pending.append(fut)
        return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def adaptive_ttl(datatype):
    """Source outages never extend the freshness of previously received facts."""
    return TTL.get(datatype, 3600)


# =====================================================================
# 3) 数据源计划（SOURCE_PLAN）：每个数据类型 → 有序源列表
#    优先级固定；健康状态只能暂停失败源，不能提升低优先级来源。
# =====================================================================
def _relay_daily_basic(code, trade_date=None):
    """估值冗余源：TuShare daily_basic 市值原值为万元，此兼容输出为亿元。"""
    if not XIAODEFA_TOKEN:
        return None
    mkt, pure = _norm_code(code)
    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[mkt]
    ts_code = f"{pure}.{exchange}"
    td = trade_date or datetime.date.today().strftime("%Y%m%d")
    params = {"ts_code": ts_code, "trade_date": td}
    rows = _xiaodefa_query(
        "daily_basic", params,
        fields="ts_code,trade_date,pe_ttm,pe,pb,total_mv,circ_mv,turnover_rate",
        timeout=20,
    )
    if not rows:
        return None
    row = rows[0]

    def g(f):
        try:
            v = row.get(f)
            return float(v) if v not in (None, "") else None
        except Exception:
            return None
    result = {
        "code": pure, "name": None,
        "pe_ttm": g("pe_ttm"), "pe": g("pe"), "pb": g("pb"),
        # TuShare daily_basic 的 total_mv/circ_mv 单位=万元；本统一行情
        # 结构以亿元表达，因此除以 1e4，而不是旧实现的 1e5。
        "total_mv": (g("total_mv") / 1e4) if g("total_mv") is not None else None,
        "circ_mv": (g("circ_mv") / 1e4) if g("circ_mv") is not None else None,
        "total_mv_unit": "100m_yuan",
        "circ_mv_unit": "100m_yuan",
        "turnover": g("turnover_rate"), "trade_date": td, "_src": "xiaodefa",
        "raw": row,
    }
    from trade_system.units import market_caps
    result.update(market_caps(result))
    return result


def _plan_kline(code, start="20260101", end="20500101", fq="qfq", **kw):
    srcs = [
        ("xiaodefa", lambda: _from_xiaodefa(code, start, end, fq)),
        ("baostock", lambda: _from_baostock(code, start, end, fq)),
        ("pytdx", lambda: _from_pytdx(code, start, end, fq)),
        ("tencent", lambda: _from_tencent(code, start, end, fq)),
        ("sina", lambda: _from_sina(code, start, end, fq)),
        ("baidu", lambda: _from_baidu(code, start, end, fq)),
    ]
    if LOCAL_MODE:   # 本机运行东方财富 K 线自动复活
        srcs.append(("eastmoney", lambda: _from_eastmoney(code, start, end, fq)))
    return srcs


def _plan_valuation(code, **kw):
    return [("xiaodefa", lambda: _relay_daily_basic(code)),
            ("tencent", lambda: _from_tencent_valuation(code))]


def _plan_financials(code, periods=8, **kw):
    return [("eastmoney", lambda: get_financials(code, periods))]


def _plan_fund_flow(code, periods=10, **kw):
    return [("eastmoney", lambda: get_fund_flow(code, periods)),
            ("sina", lambda: _from_sina_fund_flow(code, periods))]


def _plan_stock_basic(code=None, list_status="L", **kw):
    # 异后端双源：tushare_relay + 东财 push2 clist（不同供应商，抗封）
    return [("xiaodefa", lambda: _from_xiaodefa_basic(list_status)),
            ("eastmoney", lambda: _from_em_all_stocks())]


def _plan_northbound(code=None, **kw):
    # 全市场级（无个股代码），双源：同花顺 + 东财 kamt（异供应商）
    return [("ths", lambda: _from_ths_northbound()),
            ("eastmoney", lambda: _from_em_northbound())]


def _plan_consensus_eps(code, **kw):
    # 个股级，同花顺机构一致预期 EPS / 净利润预测
    return [("ths", lambda: _from_ths_eps_forecast(code))]


# —— 龙虎榜 / 筹码 / 公告 / 打板 / 舆情 等全量接入（源名统一 eastmoney/cninfo/cls/ths/sina_option） ——
def _d(date_fmt="%Y-%m-%d"):
    return datetime.date.today().strftime(date_fmt)


def _plan_dragon_tiger(code, date=None, **kw):
    return [("eastmoney", lambda: _from_em_dragon_tiger(code, date or _d()))]

def _plan_dragon_tiger_daily(code=None, date=None, **kw):
    return [("eastmoney", lambda: _from_em_dragon_tiger_daily(date or _d()))]

def _plan_margin(code, **kw):
    # 融资融券：东财 datacenter（主） + 同花顺 dataapi（异后端备）
    return [("eastmoney", lambda: _from_em_margin(code)),
            ("ths", lambda: _from_ths_margin_trading(code))]

def _plan_holder(code, **kw):
    if "start" in kw:
        return [("eastmoney", lambda: _from_em_holder(code, **kw))]
    # 股东户数：东财 datacenter（主） + 同花顺 dataapi（异后端备）
    return [("eastmoney", lambda: _from_em_holder(code)),
            ("ths", lambda: _from_ths_holder_num(code))]

def _plan_lockup(code, **kw):
    if "start" in kw:
        return [("eastmoney", lambda: _from_em_lockup(code, **kw))]
    # 限售解禁：东财 datacenter（主） + 巨潮 webapi（异后端备）
    return [("eastmoney", lambda: _from_em_lockup(code)),
            ("cninfo", lambda: _from_cninfo_lockup(code))]

def _plan_dividend(code, **kw):
    if "start" in kw:
        return [("eastmoney", lambda: _from_em_dividend(code, **kw))]
    # 分红送转：东财 datacenter（主） + 巨潮 webapi（异后端备）
    return [("eastmoney", lambda: _from_em_dividend(code)),
            ("cninfo", lambda: _from_cninfo_dividend(code))]

def _plan_block_trade(code, **kw):
    if "start" in kw:
        return [("eastmoney", lambda: _from_em_block_trade(code, **kw))]
    # 大宗交易：东财 datacenter（主） + 巨潮 webapi（异后端备）
    return [("eastmoney", lambda: _from_em_block_trade(code)),
            ("cninfo", lambda: _from_cninfo_block_trade(code))]


def _plan_stock_info(code, **kw):
    return [("eastmoney", lambda: _from_em_stock_info(code))]

def _plan_industry_rank(code=None, **kw):
    return [("eastmoney", lambda: _from_em_industry_rank())]

def _plan_zt_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_zt_pool(d))]

def _plan_zb_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_zb_pool(d))]

def _plan_dt_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_dt_pool(d))]

def _plan_yzt_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_yzt_pool(d))]

def _plan_research_report(code, **kw):
    return [("eastmoney", lambda: _from_em_reports(code))]

def _plan_stock_news(code, **kw):
    return [("eastmoney", lambda: _from_em_stock_news(code))]

def _plan_news_cls(code=None, **kw):
    # 财联社电报 ↔ 东财 7x24（异后端互备）
    return [("cls", lambda: _from_cls_telegraph()),
            ("eastmoney", lambda: _from_em_flash())]

def _plan_news_em(code=None, **kw):
    # 东财 7x24 ↔ 财联社电报（异后端互备）
    return [("eastmoney", lambda: _from_em_flash()),
            ("cls", lambda: _from_cls_telegraph())]


def _plan_ths_limit_up(code=None, date=None, **kw):
    # Provider-specific fields are not interchangeable without a qualified conversion.
    return [("ths", lambda: _from_ths_limit_up(date or _d("%Y%m%d")))]


def _plan_intraday(code, date=None, **kw):
    # 分时：东财 push2his trends2（主，零依赖） + 通达信 pytdx minutes（异构 TCP 备）
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_trends(code, d)),
            ("pytdx", lambda: _from_pytdx_minutes(code, d))]


# —— P1 新增：覆盖面缺口类型 ——
def _plan_index_kline(code, start="20260101", end="20500101", fq="qfq", **kw):
    return [("eastmoney", lambda: _from_em_index_kline(code, start, end, fq))]


def _plan_index_spot(code, **kw):
    return [("eastmoney", lambda: _from_em_index_spot(code))]


def _plan_etf_kline(code, start="20260101", end="20500101", fq="qfq", **kw):
    return [("eastmoney", lambda: _from_em_etf_kline(code, start, end, fq))]


def _plan_etf_info(code, **kw):
    return [("eastmoney", lambda: _from_em_etf_info(code))]


def _plan_cb_kline(code, start="20260101", end="20500101", fq="qfq", **kw):
    return [("eastmoney", lambda: _from_em_cb_kline(code, start, end, fq))]


def _plan_cb_quote(code, **kw):
    return [("eastmoney", lambda: _from_em_cb_quote(code))]


def _plan_disclosure(product, code, page_size=20, start=None, end=None, **kw):
    return [("eastmoney", lambda: _from_em_disclosure(code, product, page_size, start, end))]


def _plan_northbound_hist(code=None, start="20250101", end="20500101", **kw):
    return [("eastmoney", lambda: _from_em_northbound_hist(start, end))]


def _plan_bid_ask(code, **kw):
    # 五档盘口：腾讯 qt（可靠五档）为主 + 东财 geo（异后端备）
    return [("tencent", lambda: _from_tencent_bid_ask(code)),
            ("eastmoney", lambda: _from_em_bid_ask(code))]


# —— P2 补缺：新股/IPO 日历 + 宏观经济 ——
def _plan_ipo_calendar(start="20260101", end="20500101", **kw):
    return [("eastmoney", lambda: _from_em_ipo(start, end))]


def _plan_macro(indicators=None, **kw):
    return [("eastmoney", lambda: _from_em_macro(indicators))]


# —— P1 新增：核心单源异后端第二源 ——
def _plan_statements(code, report_type="lrb", periods=8, **kw):
    return [("sina", lambda: get_financial_statements(code, report_type, periods))]


def _plan_irm_v2(code, **kw):
    return [("cninfo", lambda: _from_cninfo_irm(code)),
            ("eastmoney", lambda: _from_em_irm(code))]


def _plan_announcements_v2(code, page_size=30, **kw):
    if "start" in kw:
        return [("eastmoney", lambda: _from_em_announcements(code, page_size=page_size, **kw))]
    return [("cninfo", lambda: _from_cninfo_announcements(code, page_size=page_size)),
            ("eastmoney", lambda: _from_em_announcements(code, page_size=page_size))]


def _plan_option_tquote_v2(code, **kw):
    return [("sina_option", lambda: _from_sina_option_tquote(code)),
            ("eastmoney", lambda: _from_em_option_tquote(code))]


def _plan_option_greeks_v2(code, **kw):
    return [("sina_option", lambda: _from_sina_option_greeks(code))]


def _plan_hot_topics_v2(code=None, date=None, **kw):
    d = date or (kw.get("date") or datetime.date.today().strftime("%Y-%m-%d"))
    return [("ths", lambda: _from_ths_hot_reason(d))]


def _plan_ths_hot_list_v2(code=None, period=None, **kw):
    return [("ths", lambda: _from_ths_hot_list(period or "hour"))]


def _plan_fund_flow_120d_v2(code, **kw):
    return [("eastmoney", lambda: _from_em_fund_flow_120d(code)),
            ("sina", lambda: _from_sina_fund_flow(code, days=120)),
            ("xiaodefa", lambda: _from_xiaodefa_moneyflow(code, days=120))]

def _plan_sector_flow(code=None, **kw):
    """Market-wide sector capital flow, independent of the Tushare relay."""
    return [("eastmoney", lambda: _from_em_sector_flow(kw.get("top_n", 200))),
            ("xiaodefa", lambda: _from_xiaodefa_sector_flow(kw.get("date"), kw.get("top_n", 300)))]


def _plan_em_hot_rank(code=None, top=None, **kw):
    return [("eastmoney", lambda: _from_em_hot_rank(top or 50))]

def _plan_hot_concept(code, **kw):
    return [("eastmoney", lambda: _from_em_hot_concept(code))]


def _plan_valuation_metrics(code, **kw):
    return [("local", lambda: _from_local_valuation_metrics(code))]


def _from_local_valuation_metrics(code):
    """前向PE / PEG / PE消化年数 —— 获取报价和预期 EPS 后计算；仅显式估值需求调用。
    输入：腾讯实时价 + 同花顺一致预期EPS（若取不到则跳过对应指标）。"""
    val = _from_tencent_valuation(code)
    if not val:
        return None
    price = val.get("price")
    eps = _from_ths_eps_forecast(code)
    out = {"code": val.get("code"), "name": val.get("name"), "price": price,
           "pe_ttm": val.get("pe_ttm"), "pb": val.get("pb"), "_src": "local"}
    if eps and eps.get("latest_eps_mean"):
        fwd = eps["latest_eps_mean"]
        out["forward_pe"] = round(price / fwd, 2) if fwd > 0 else None
        cur = val.get("pe_ttm")
        if cur and fwd and fwd > 0:
            cagr = (fwd / (price / cur) - 1) if (price / cur) > 0 else 0  # 下年EPS/当年EPS - 1
            out["cagr"] = round(cagr, 4)
            out["peg"] = round((price / fwd) / (cagr * 100), 2) if cagr > 0 else None
            out["pe_digestion_years"] = round(__import__("math").log(cur / 30) / __import__("math").log(1 + cagr), 2) if (cur > 30 and cagr > 0) else 0.0
    return out

SOURCE_PLAN = {
    "kline": _plan_kline,
    "valuation": _plan_valuation,
    "financials": _plan_financials,
    "fund_flow": _plan_fund_flow,
    "statements": _plan_statements,
    "stock_basic": _plan_stock_basic,
    "northbound": _plan_northbound,
    "hot_topics": _plan_hot_topics_v2,
    "consensus_eps": _plan_consensus_eps,
    # —— 龙虎榜 / 筹码 / 解禁 / 分红 / 大宗 ——
    "dragon_tiger": _plan_dragon_tiger,
    "dragon_tiger_daily": _plan_dragon_tiger_daily,
    "margin_trading": _plan_margin,
    "holder_num": _plan_holder,
    "lockup": _plan_lockup,
    "dividend": _plan_dividend,
    "block_trade": _plan_block_trade,
    "fund_flow_120d": _plan_fund_flow_120d_v2,   # 双源：东财 + 新浪资金流（异后端）
    "stock_flow": _plan_fund_flow_120d_v2,
    "sector_flow": _plan_sector_flow,
    "stock_info": _plan_stock_info,
    # —— 行业 / 打板 / 舆情 ——
    "industry_rank": _plan_industry_rank,
    "zt_pool": _plan_zt_pool,
    "zb_pool": _plan_zb_pool,
    "dt_pool": _plan_dt_pool,
    "yzt_pool": _plan_yzt_pool,
    "limit_up_sentiment": (),  # Derived from existing pool receipts; no provider route.
    "research_report": _plan_research_report,
    "stock_news": _plan_stock_news,
    "news_cls": _plan_news_cls,
    "news_em": _plan_news_em,
    "announcements": _plan_announcements_v2,
    "irm": _plan_irm_v2,
    "ths_limit_up": _plan_ths_limit_up,
    "ths_hot_list": _plan_ths_hot_list_v2,
    "em_hot_rank": _plan_em_hot_rank,
    "hot_concept": _plan_hot_concept,
    "option_tquote": _plan_option_tquote_v2,
    "option_greeks": _plan_option_greeks_v2,
    "valuation_metrics": _plan_valuation_metrics,
    "intraday": _plan_intraday,
    # —— P1 新增：覆盖面缺口类型 ——
    "index_kline": _plan_index_kline,
    "index_spot": _plan_index_spot,
    "etf_kline": _plan_etf_kline,
    "etf_info": _plan_etf_info,
    "cb_kline": _plan_cb_kline,
    "cb_quote": _plan_cb_quote,
    "forecast": lambda code, **kw: _plan_disclosure("forecast", code, **kw),
    "express": lambda code, **kw: _plan_disclosure("express", code, **kw),
    "top10_holders": lambda code, **kw: _plan_disclosure("top10_holders", code, **kw),
    "northbound_hist": _plan_northbound_hist,
    "bid_ask": _plan_bid_ask,
    # —— P2 补缺：新股 / 宏观 ——
    "ipo_calendar": _plan_ipo_calendar,
    "macro": _plan_macro,
}

# 各类数据缓存 TTL（秒）
TTL = {
    "kline": 6 * 3600,
    "valuation": 60,
    "financials": 7 * 24 * 3600,
    "fund_flow": 24 * 3600,
    "statements": 30 * 24 * 3600,
    "stock_basic": 7 * 24 * 3600,
    "northbound": 60,                       # 日内分钟级，缓存 1 分钟
    "hot_topics": 8 * 3600,                 # 当日题材，缓存到收盘后
    "consensus_eps": 7 * 24 * 3600,         # 机构预期日更
    # —— 新增全量类型 ——
    "dragon_tiger": 7 * 24 * 3600, "dragon_tiger_daily": 8 * 3600,
    "margin_trading": 24 * 3600, "holder_num": 7 * 24 * 3600,
    "lockup": 7 * 24 * 3600, "dividend": 30 * 24 * 3600,
    "block_trade": 7 * 24 * 3600, "fund_flow_120d": 24 * 3600,
    "stock_flow": 24 * 3600, "sector_flow": 15 * 60,
    "stock_info": 7 * 24 * 3600, "industry_rank": 8 * 3600,
    "zt_pool": 60, "zb_pool": 60, "dt_pool": 60,
    "yzt_pool": 60, "limit_up_sentiment": 60,
    "research_report": 7 * 24 * 3600, "stock_news": 6 * 3600,
    "news_cls": 6 * 3600, "news_em": 6 * 3600,
    "announcements": 7 * 24 * 3600, "irm": 7 * 24 * 3600,
    "ths_limit_up": 60, "ths_hot_list": 3600, "em_hot_rank": 3600,
    "hot_concept": 8 * 3600, "option_tquote": 60, "option_greeks": 60,
    "valuation_metrics": 60, "intraday": 60,
    # —— P1 覆盖面缺口类型（TTL）——
    "index_kline": 6 * 3600, "index_spot": 60, "etf_kline": 6 * 3600,
    "etf_info": 60, "cb_kline": 6 * 3600, "cb_quote": 60,
    "forecast": 7 * 24 * 3600, "express": 7 * 24 * 3600,
    "top10_holders": 7 * 24 * 3600, "northbound_hist": 24 * 3600,
    "bid_ask": 30,
    "ipo_calendar": 12 * 3600, "macro": 7 * 24 * 3600,
}

# 各类数据单源超时（秒）——HTML/JSONP/签名类给足余量
TIMEOUT = {
    "kline": 10, "valuation": 10, "financials": 12, "fund_flow": 12,
    "statements": 12, "stock_basic": 20,
    "northbound": 12, "hot_topics": 12, "consensus_eps": 18,
    # —— 新增全量类型 ——
    "dragon_tiger": 12, "dragon_tiger_daily": 15, "margin_trading": 12,
    "holder_num": 12, "lockup": 12, "dividend": 12, "block_trade": 12,
    "fund_flow_120d": 12, "stock_flow": 12, "sector_flow": 15,
    "stock_info": 10, "industry_rank": 12,
    "zt_pool": 12, "zb_pool": 12, "dt_pool": 12, "yzt_pool": 12,
    "limit_up_sentiment": 15, "research_report": 20, "stock_news": 12,
    "news_cls": 12, "news_em": 10, "announcements": 15, "irm": 12,
    "ths_limit_up": 12, "ths_hot_list": 10, "em_hot_rank": 10,
    "hot_concept": 10, "option_tquote": 10, "option_greeks": 10,
    "valuation_metrics": 15,
    # —— P1 覆盖面缺口类型（TIMEOUT）——
    "index_kline": 10, "index_spot": 10, "etf_kline": 10, "etf_info": 10,
    "cb_kline": 10, "cb_quote": 10, "forecast": 12, "express": 12,
    "top10_holders": 12, "northbound_hist": 12, "bid_ask": 10,
    "ipo_calendar": 15, "macro": 15,
}


# =====================================================================
# 4) 统一入口：缓存优先 + 自适应降级 + 过期兜底
# =====================================================================
def _cache_key(datatype, code, kwargs):
    """Bind all request semantics and the retained authorization scope."""
    import hashlib
    scope = hashlib.sha256(str(XIAODEFA_TOKEN or "").encode()).hexdigest()
    return json.dumps([datatype, code, kwargs, scope], sort_keys=True, default=str)


@request_budget(60)
def get(datatype, code=None, ttl=None, timeout_per=None, **kwargs):
    """One acquisition owner per demand, including timed-out workers still running."""
    import hashlib
    from pathlib import Path
    from trade_system.file_lock import FileLock, FileLockBusy

    # Run ids belong to receipts, never to the identity of economic data.
    kwargs.pop("run_id", None)
    if datatype == "stock_flow":
        datatype = "fund_flow_120d"
    if datatype in {"financials", "statements"}:
        kwargs.setdefault("periods", 8)
        if type(kwargs["periods"]) is not int or not 1 <= kwargs["periods"] <= 120:
            raise ValueError("financial periods must be an integer in [1,120]")
    if datatype in _EVENT_COUNTS:
        kwargs.setdefault("page_size", _EVENT_COUNTS[datatype])
        if type(kwargs["page_size"]) is not int or not 1 <= kwargs["page_size"] <= 120:
            raise ValueError("event page_size must be an integer in [1,120]")
    event_range = datatype in _EVENT_RANGES and ("start" in kwargs or "end" in kwargs)
    if event_range:
        if set(kwargs) - {"start", "end", "page_size", "offline"} or not {"start", "end"} <= kwargs.keys():
            raise ValueError("event ranges require explicit start/end and bounded page_size")
        for key in ("start", "end"):
            kwargs[key] = datetime.datetime.strptime(_norm_date(kwargs[key]), "%Y%m%d").date().isoformat()
        kwargs.setdefault("page_size", 100)
        if kwargs["start"] > kwargs["end"] or type(kwargs["page_size"]) is not int or not 1 <= kwargs["page_size"] <= 120:
            raise ValueError("invalid event date range or page_size")
    elif datatype in _EVENT_RANGES:
        if any(key in kwargs for key in ("start", "end", "full_history", "page_num")):
            raise ValueError("this event source has no qualified historical range contract")
    if datatype == "statements":
        kwargs.setdefault("report_type", "lrb")
        if kwargs["report_type"] not in {"lrb", "fzb", "llb"}:
            raise ValueError("unknown financial statement type")
    if datatype in {"kline", "index_kline", "etf_kline", "cb_kline"}:
        today = datetime.date.today()
        kwargs.setdefault("fq", "qfq")
        kwargs.setdefault("start", (today-datetime.timedelta(days=10)).strftime("%Y%m%d"))
        kwargs.setdefault("end", today.strftime("%Y%m%d"))
        kwargs["start"], kwargs["end"] = _norm_date(kwargs["start"]), _norm_date(kwargs["end"])
        kwargs.setdefault("full_history", False)
    # 日期型 / 周期型类型：默认取今天/本周期，并把参数固化进 kwargs，
    # 保证缓存 key 与取数一致（否则 warm 与 get 的 key 不一致会命中失败）。
    _DATE_TYPES = ("hot_topics", "dragon_tiger", "dragon_tiger_daily",
                   "zt_pool", "zb_pool", "dt_pool", "yzt_pool", "ths_limit_up",
                   "limit_up_sentiment", "intraday")
    _PERIOD_TYPES = ("ths_hot_list",)
    if datatype in _DATE_TYPES and "date" not in kwargs:
        # 打板池 / 同花顺涨停 / 分时 用 YYYYMMDD；其余用 YYYY-MM-DD
        fmt = "%Y%m%d" if datatype in ("zt_pool", "zb_pool", "dt_pool",
                                       "yzt_pool", "ths_limit_up", "limit_up_sentiment",
                                       "intraday") else "%Y-%m-%d"
        kwargs["date"] = datetime.date.today().strftime(fmt)
    if datatype in {"zt_pool", "zb_pool", "dt_pool", "yzt_pool", "limit_up_sentiment", "ths_limit_up"}:
        # Old receipts may contain a different pool or incomplete zero-filled sentiment.
        kwargs["_pool_contract"] = "provider_specific_complete_v2"
        kwargs["date"] = datetime.datetime.strptime(_norm_date(kwargs["date"]), "%Y%m%d").strftime("%Y%m%d")
    if datatype in _PERIOD_TYPES and "period" not in kwargs:
        kwargs["period"] = "hour"
    sessions = kwargs.pop("expected_sessions", None)
    offline = bool(kwargs.pop("offline", False))
    if datatype == "limit_up_sentiment":
        if code is not None or sessions is not None or set(kwargs) - {"date", "_pool_contract"}:
            raise ValueError("sentiment accepts a market date, not a security or alternate product scope")
        return _get_sentiment(kwargs["date"], ttl, timeout_per, offline)
    # Overlapping windows share ownership until timed-out workers have exited.
    varying = {"start", "end", "page_size"} if event_range else {"start", "end"} if datatype in _BAR_TYPES else {"periods", "page_size"} if datatype in {"financials", "statements", *_EVENT_COUNTS} else set()
    identity_kwargs = {k: v for k, v in kwargs.items() if k not in varying}
    identity = _cache_key(datatype, code, identity_kwargs)
    # A bounded set of lock carriers avoids a new file for every stock/session.
    bucket = int(hashlib.sha256(identity.encode()).hexdigest()[:4], 16) % 64
    lock = FileLock(Path(CACHE_DIR) / f"acquisition-{bucket:02d}.guard")
    try:
        lock.__enter__()
    except FileLockBusy:
        return None, {"source": None, "status": "in_progress", "error": "acquisition already owned; retry from cache after completion"}
    pending = []
    try:
        if event_range or datatype in {"financials", "statements", *_EVENT_COUNTS}:
            return _get_subset(datatype, code, ttl, timeout_per, offline, pending, kwargs)
        if sessions is not None and not kwargs.get("full_history") and datatype in _BAR_TYPES:
            return _get_window(datatype, code, ttl, timeout_per, sessions, offline, pending, kwargs)
        return _get_owned(datatype, code, ttl, timeout_per, _pending=pending, _offline=offline, **kwargs)
    finally:
        remaining = set(pending)
        if not remaining:
            lock.__exit__()
        else:
            mutex = threading.Lock()
            def completed(future):
                with mutex:
                    remaining.discard(future)
                    if not remaining:
                        lock.__exit__()
            for future in pending:
                future.add_done_callback(completed)


def _get_sentiment(day, ttl, timeout, offline):
    """Pure aggregation: original pool receipts own acquisition and freshness."""
    pools, inputs = {}, []
    for product in ("zt_pool", "zb_pool", "dt_pool"):
        rows, meta = get(product, date=day, ttl=ttl, timeout_per=timeout, offline=offline)
        inputs.append(dict(meta, product=product))
        # An empty unqualified pool is unknown, not an observed zero.
        if (not isinstance(rows, list) or not rows or meta.get("source") != "eastmoney"
                or meta.get("status") not in {"fresh", "live", "refreshed"}):
            return None, {"status": "failed", "source": None, "derived": True,
                          "error": "qualified pool coverage missing", "inputs": inputs}
        codes = [r.get("code") if isinstance(r, dict) else None for r in rows]
        if any(not isinstance(code, str) or not code for code in codes) or len(set(codes)) != len(codes):
            return None, {"status": "failed", "source": None, "derived": True,
                          "error": "pool identity missing or duplicated", "inputs": inputs}
        pools[product] = rows
    heights = [r.get("limit_days") for r in pools["zt_pool"]]
    if any(type(height) is not int or height < 1 for height in heights):
        return None, {"status": "failed", "source": None, "derived": True,
                      "error": "observed ladder heights missing", "inputs": inputs}
    from collections import Counter
    zt, zb, dt = (len(pools[p]) for p in ("zt_pool", "zb_pool", "dt_pool"))
    received = min(m["received_at"] for m in inputs)
    data = {"date": day, "zt_count": zt, "zb_count": zb, "dt_count": dt,
            "break_rate": round(zb/(zt+zb)*100, 1), "max_height": max(heights),
            "ladder": dict(sorted(Counter(heights).items())), "_src": "derived:eastmoney"}
    cached = all(m["status"] == "fresh" for m in inputs)
    return data, {"status": "fresh" if cached else "live", "cache_hit": cached,
                  "source": "derived:eastmoney", "derived": True,
                  "received_at": received, "cached_at": received, "inputs": inputs,
                  "scope": "independent_pool_receipts_not_atomic_snapshot", "execution_ready": False}


_EVENT_COUNTS = {"announcements":30, "forecast":20, "express":10, "top10_holders":10}
_EVENT_RANGES = {"holder_num", "lockup", "dividend", "block_trade", *_EVENT_COUNTS}
_BAR_TYPES = {"kline", "index_kline", "etf_kline", "cb_kline"}


def _get_subset(datatype, code, ttl, timeout, offline, pending, kwargs):
    """Subset one current receipt, never stitch event identities or revision snapshots."""
    ranged = datatype in _EVENT_RANGES and "start" in kwargs
    events = ranged or datatype in _EVENT_COUNTS
    count_key = "page_size" if events else "periods"
    count = kwargs[count_key]
    coverage = "complete_event_range" if ranged else "bounded_event_records" if events else "reported_periods"
    ttl = adaptive_ttl(datatype) if ttl is None else ttl
    scope = {k: v for k, v in kwargs.items() if k not in ({"start", "end", count_key} if ranged else {count_key})}
    prefix = ("event-range-v2:" if ranged else "event-records-v2:" if events else "periods-v1:") + _cache_key(datatype, code, scope) + ":"
    spec = SOURCE_PLAN[datatype]
    allowed = {name for name, _ in (spec(code, **kwargs) if callable(spec) else spec)}

    def subset(payload):
        rows = payload
        if ranged:
            if (not isinstance(payload, dict) or payload.get("complete") is not True
                    or not isinstance(payload.get("rows"), list) or type(payload.get("total")) is not int
                    or payload["total"] != len(payload["rows"])
                    or not all(isinstance(payload.get(k), str) for k in ("start", "end"))
                    or not payload["start"] <= kwargs["start"] <= kwargs["end"] <= payload["end"]):
                return None
            rows = payload["rows"]
        if not isinstance(rows, list):
            return None
        try:
            field = "报告期" if datatype == "statements" else "date"
            dates = [datetime.date.fromisoformat(str(row[field])[:10] if events else row[field]).isoformat() for row in rows]
            identities = [str(row.get("id") or "") for row in rows] if events else dates
            if not all(identities) or len(set(identities)) != len(identities):
                return None
            if ranged and any(not payload["start"] <= day <= payload["end"] for day in dates):
                return None
        except (ValueError, KeyError, TypeError):
            return None
        ordered = [row for day, row in sorted(zip(dates, rows), key=lambda item: item[0], reverse=True)
                   if not ranged or kwargs["start"] <= day <= kwargs["end"]]
        return ordered if ranged else ordered[:count] if len(ordered) >= count else None

    def result(payload, meta):
        selected = subset(payload)
        status = meta["status"]
        if selected is None and status in {"fresh", "live", "refreshed"}:
            status = "partial"
        extra = {"start": kwargs["start"], "end": kwargs["end"]} if ranged else {
            "records_requested" if events else "periods_requested": count,
            "records_observed" if events else "periods_observed": len(payload) if isinstance(payload, list) else 0}
        return (selected if selected is not None else None if ranged else payload), dict(meta, status=status,
            coverage=coverage, **extra, receipts=[{"data": payload, "meta": meta}] if payload is not None else [])

    candidates = []
    for key, receipt, _ in cache.matching(prefix):
        if not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("source") not in allowed:
            continue
        received = receipt.get("received_at")
        if isinstance(received, (int, float)) and 0 <= time.time()-received < ttl:
            candidates.append((received, key, receipt))
    if candidates:
        received, key, receipt = max(candidates, key=lambda item: item[0])
        if subset(receipt.get("data")) is not None:
            return result(receipt["data"], {"source": receipt["source"], "status": "fresh", "cache_hit": True,
                "received_at": received, "cached_at": received, "receipt_key": key})
        if offline:
            return None, {"status": "failed", "source": None, "error": "offline current receipt coverage missing"}
    suffix = kwargs["start"]+":"+kwargs["end"] if ranged else str(count)
    payload, meta = _get_owned(datatype, code, 0 if candidates else ttl, timeout,
        _pending=pending, _offline=offline, _receipt_key=prefix+suffix, **kwargs)
    return result(payload, meta)


def _get_window(datatype, code, ttl, timeout, sessions, offline, pending, kwargs):
    """Reuse qualified sessions from same-source receipts; never infer missing bars."""
    import math
    expected = sorted({_norm_date(day) for day in sessions})
    sd, ed = kwargs["start"], kwargs["end"]
    for day in expected:
        datetime.datetime.strptime(day, "%Y%m%d")
        if not sd <= day <= ed:
            raise ValueError("session outside requested window")
    if not expected:
        return [], {"source": None, "status": "not_applicable", "receipts": []}
    ttl = adaptive_ttl(datatype) if ttl is None else ttl
    spec = SOURCE_PLAN[datatype]
    allowed = {name for name, _ in (spec(code, **kwargs) if callable(spec) else spec)}
    scope = {k: v for k, v in kwargs.items() if k not in {"start", "end", "full_history"}}
    prefix = "window-v1:" + _cache_key(datatype, code, scope) + ":"
    adjustment = {"qfq": "qfq", "hfq": "hfq", "": "none", "bfq": "none"}.get(str(kwargs.get("fq") or ""), "unknown")

    def bar_identity(value):
        value = str(value).strip().upper().replace(".", "")
        for market in ("SH", "SZ", "BJ"):
            if value.startswith(market):
                return value[2:], market
            if value.endswith(market):
                return value[:-2], market
        return value, None

    requested_code, requested_market = bar_identity(code)
    if requested_market is None and datatype == "kline":
        requested_market = _norm_code(requested_code)[0].upper()

    def qualified(rows):
        if not isinstance(rows, list):
            return {}
        selected, duplicate, seen = {}, set(), set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = _norm_date(row.get("date", ""))
            if day in seen:
                duplicate.add(day)
            seen.add(day)
            try:
                good = (row.get("adjustment") == adjustment
                        and row.get("volume_unit") in {"shares", "hands"}
                        and row.get("amount_unit") in {"yuan", "thousand_yuan", "not_provided"}
                        and math.isfinite(float(row.get("close"))) and float(row["close"]) > 0)
            except (TypeError, ValueError):
                good = False
            identities = [bar_identity(row[k]) for k in ("code", "stock_code", "ts_code") if row.get(k)]
            if good and day in expected and all(
                number == requested_code and (market is None or requested_market is None or market == requested_market)
                for number, market in identities
            ):
                selected[day] = row
        return {day: row for day, row in selected.items() if day not in duplicate}

    receipts, rows, origin = [], {}, None
    for key, receipt, ts in cache.matching(prefix):
        if not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("source") not in allowed:
            continue
        received = receipt.get("received_at")
        if not isinstance(received, (int, float)) or not 0 <= time.time() - received < ttl:
            continue
        found = qualified(receipt.get("data"))
        if not found or (origin and origin != receipt["source"]):
            continue
        # Adjusted series have a snapshot-specific anchor. Reuse one complete
        # receipt only; stitching separately adjusted windows is not equivalent.
        if adjustment != "none" and set(found) != set(expected):
            continue
        origin = receipt["source"]
        new = set(found) - rows.keys()
        if new:
            rows.update({day: found[day] for day in new})
            receipts.append({"data": receipt["data"], "meta": {"source": origin,
                "status": "fresh", "received_at": received, "receipt_key": key, "qualified_dates": sorted(found)}})
        if set(rows) == set(expected):
            break
    missing = [day for day in expected if day not in rows]
    groups, previous = [], -2
    for index, day in enumerate(expected):
        if day not in missing:
            continue
        if index != previous + 1:
            groups.append([])
        groups[-1].append(day)
        previous = index
    for group in groups:
        request = dict(kwargs, start=group[0], end=group[-1])
        key = prefix + group[0] + ":" + group[-1]
        data, meta = _get_owned(datatype, code, ttl, timeout, _pending=pending,
            _offline=offline, _provider=origin, _receipt_key=key, **request)
        if data is not None:
            original, _ = cache.get(key)
            original_data = original["data"] if isinstance(original, dict) and meta["status"] in {"fresh", "live", "refreshed"} else data
            meta = dict(meta, qualified_dates=sorted(qualified(original_data)))
            receipts.append({"data": original_data, "meta": meta})
        if meta["status"] not in {"fresh", "live", "refreshed"}:
            break
        origin = meta.get("source")
        rows.update(qualified(data))
    missing = sorted(set(expected) - rows.keys())
    status = "partial" if missing else "live" if any(r["meta"]["status"] in {"live", "refreshed"} for r in receipts) else "fresh"
    times = [r["meta"].get("received_at") for r in receipts if r["meta"].get("received_at")]
    return [rows[day] for day in sorted(rows)], {"source": origin, "status": status,
        "received_at": min(times) if times else None, "receipts": receipts,
        "missing_sessions": missing, "cache_hit": status == "fresh"}


def _get_owned(datatype, code=None, ttl=None, timeout_per=None, *, _pending=None, **kwargs):
    """统一取数。返回 (data, meta)。
    meta["status"]:
      fresh     —— 缓存命中且未过期（0 网络）
      live      —— 实时取回（此前无缓存）
      refreshed —— 缓存过期，本次实时刷新成功
      stale     —— ⚠ 所有实时源失败，已降级返回【过期缓存】（仍拿到数据！）
      failed    —— 彻底失败（从未取过且无任何源可用）
    """
    offline = bool(kwargs.pop("_offline", False))
    provider = kwargs.pop("_provider", None)
    receipt_key = kwargs.pop("_receipt_key", None)
    if datatype not in SOURCE_PLAN:
        raise ValueError(f"未知数据类型: {datatype}，可选: {list(SOURCE_PLAN)}")
    timeout_per = timeout_per if timeout_per is not None else TIMEOUT.get(datatype, 10)
    # Daily requests default to a bounded slice; full history must be explicit.
    full_history = bool(kwargs.pop("full_history", False))
    # Full-history K-line payloads are canonicalized in DuckDB/Parquet.  Do
    # not duplicate hundreds of megabytes of JSON in the small SQLite
    # resilience cache; bounded daily slices may still use the cache.
    cache_allowed = not (datatype in {"kline", "index_kline", "etf_kline", "cb_kline"} and full_history)
    ttl = ttl if ttl is not None else adaptive_ttl(datatype)

    # Bounded K-line receipts bind every request parameter and date window.
    if datatype in {"kline", "index_kline", "etf_kline", "cb_kline"}:
        today = datetime.date.today()
        kwargs.setdefault("start", (today-datetime.timedelta(days=10)).strftime("%Y%m%d"))
        kwargs.setdefault("end", today.strftime("%Y%m%d"))
        fq = kwargs.get("fq", "qfq")
        sd = _norm_date(kwargs.get("start", "19900101"))
        ed = _norm_date(kwargs.get("end", "20500101"))
        key = _cache_key(datatype, code, kwargs)
        post_filter = (lambda rows: [b for b in rows
                                     if isinstance(b, dict) and sd <= _norm_date(b.get("date", "")) <= ed]
                       if isinstance(rows, list) else rows)
        fetch_kwargs = ({**kwargs, "start": "19900101", "end": "20500101"}
                        if full_history else kwargs)

        expected_adjustment = {"qfq": "qfq", "hfq": "hfq", "": "none", "bfq": "none"}.get(
            str(fq or "").strip().lower(), "unknown"
        )

        def _contract_matches(rows):
            """Reject old cache/source rows whose adjustment is not explicit."""
            if not isinstance(rows, list) or not rows:
                return False
            for row in rows:
                if not isinstance(row, dict):
                    return False
                if str(row.get("adjustment") or "unknown").lower() != expected_adjustment:
                    return False
                if str(row.get("volume_unit") or "unknown").lower() == "unknown":
                    return False
                if str(row.get("amount_unit") or "unknown").lower() not in {"yuan", "thousand_yuan", "not_provided"}:
                    # Some K-line providers legitimately do not expose
                    # amount.  That fact is explicit; a zero placeholder
                    # must not masquerade as an observed amount.
                    return False
            return True
    else:
        key = _cache_key(datatype, code, kwargs)
        post_filter = lambda x: x
        fetch_kwargs = kwargs

    # ① 缓存优先
    # Older unbound cache entries may belong to a retired channel. Keep them on
    # disk as history, but never reinterpret them as a newly qualified receipt.
    key = receipt_key or ("receipt-v2:" if datatype == "holder_num" else "receipt-v1:") + _cache_key(datatype, code, {"request_key": key})
    spec = SOURCE_PLAN[datatype]
    sources = spec(code, **fetch_kwargs) if callable(spec) else list(spec)
    allowed = {name for name, _ in sources if provider is None or name == provider}
    receipt, ts = cache.get(key) if cache_allowed else (None, 0)
    val = receipt.get("data") if isinstance(receipt, dict) and receipt.get("schema") == 1 else None
    origin = receipt.get("source") if val is not None else None
    received_at = receipt.get("received_at") if val is not None else None
    if origin not in allowed or not isinstance(received_at, (int, float)) or received_at <= 0:
        val = None
    if val is not None and datatype in _BAR_TYPES and not _contract_matches(val):
        val = None
    if val is not None and 0 <= time.time() - received_at < ttl:
        return post_filter(val), {"source": origin, "status": "fresh", "cache_hit": True,
                                  "cached_at": received_at, "received_at": received_at, "receipt_key": key}

    if offline:
        return None, {"source": origin, "status": "failed", "error": "offline qualified cache miss"}
    # Provider policy remains authoritative on cache and network paths.
    ordered = [(name, fn) for name, fn in sources if name in allowed]
    eligible = [(name, fn) for name, fn in ordered if not health.is_cooldown(name, datatype)]
    if not eligible and ordered:
        # Half-open recovery: allow one probe only.  Probing every cooled
        # source at once defeats the circuit breaker and can trigger another
        # burst against all providers.
        eligible = ordered[:1]
    for name, fn in eligible:
        deadline = time.monotonic()+timeout_per
        if request_deadline.get() is not None:
            deadline = min(deadline, request_deadline.get())
        if deadline <= time.monotonic():
            break
        t0 = time.time()
        try:
            rate.acquire(name, deadline=deadline)
        except TimeoutError:
            continue  # Local queue/cooldown exhaustion is not a provider failure.
        try:
            out = _call(fn, deadline-time.monotonic(), pending=_pending)
        finally:
            if _pending:
                _pending[-1].add_done_callback(lambda future, source=name: rate.release(source))
            else:
                rate.release(name)
        dt = time.time() - t0
        if _pending:
            return None, {"source": name, "status": "in_progress", "error": "provider timeout; acquisition ownership retained until worker exits"}
        if out:
            if datatype in {"kline", "index_kline", "etf_kline", "cb_kline"} and not _contract_matches(out):
                health.record(name, datatype, False, dt)
                logger.warning("%s returned rows without a verified K-line unit/adjustment contract", name)
                continue
            received_at = time.time()
            if cache_allowed:
                cache.put(key, {"schema": 1, "source": name, "received_at": received_at, "data": out})
            health.record(name, datatype, True, dt)
            if val is not None:
                return post_filter(out), {"source": name, "status": "refreshed",
                                          "cached_at": received_at, "received_at": received_at, "latency": round(dt, 3), "receipt_key": key}
            return post_filter(out), {"source": name, "status": "live",
                                     "cached_at": received_at, "received_at": received_at, "latency": round(dt, 3), "receipt_key": key}
        else:
            health.record(name, datatype, False, dt)

    # ③ 全源失败 → 过期缓存兜底（关键：永不空手而归）
    if val is not None:
        return post_filter(val), {"source": origin, "status": "stale", "cache_hit": True,
                                 "cached_at": received_at, "received_at": received_at,
                                 "warning": "所有实时源失败，已降级返回过期缓存"}
    return None, {"source": None, "status": "failed"}


def get_batch(datatype, codes, ttl=None, timeout_per=None, **kwargs):
    """批量取数：同一类型多只股票，返回 {code: (data, meta)}。
    各 code 独立走 get() 的完整四层保险，互不阻塞失败。用于盘前批量体检 / 指数 ETF 组合。"""
    out = {}
    for c in codes:
        try:
            out[c] = get(datatype, c, ttl=ttl, timeout_per=timeout_per, **kwargs)
        except Exception as e:
            out[c] = (None, {"source": None, "status": "failed", "error": str(e)[:120]})
    return out


def to_dataframe(data):
    """把 list[dict] 数据规整成 DataFrame（可选依赖 pandas；缺失时退化为 list）。
    统一输出入口：所有 list 型数据均为 list[dict]，可直接入表。"""
    if not isinstance(data, list):
        return data
    try:
        import pandas as pd
        return pd.DataFrame(data)
    except Exception:
        return data


# =====================================================================
# 5) 预热 / 状态 / 演示
# =====================================================================
WATCHLIST = ["600519", "000001", "300750", "601318", "000858"]


def _warm_one(datatype, code):
    try:
        data, meta = get(datatype, code)
        st = meta["status"]
        n = len(data) if isinstance(data, list) else (1 if data else 0)
        tag = code if code else "<全市场>"
        print(f"  {tag:>8} {datatype:<14} -> {st:<9} src={meta.get('source')} n={n}")
    except Exception as e:
        tag = code if code else "<全市场>"
        print(f"  {tag:>8} {datatype:<14} -> ERROR {e}")


def warm(codes=None, datatypes=None):
    codes = codes or WATCHLIST
    # 个股级类型（预热时逐只拉）；已剔除纯市场级/期权类（见 global_types）
    per_code = datatypes or ["kline", "valuation", "financials", "consensus_eps",
                             "margin_trading", "holder_num", "lockup", "dividend",
                             "block_trade", "fund_flow_120d", "stock_info",
                             "research_report", "announcements", "irm", "hot_concept",
                             "valuation_metrics", "intraday"]
    global_types = [t for t in ("northbound", "hot_topics", "dragon_tiger_daily",
                                "industry_rank", "zt_pool", "zb_pool", "dt_pool",
                                "yzt_pool", "limit_up_sentiment", "news_cls", "news_em",
                                "ths_limit_up", "ths_hot_list", "em_hot_rank",
                                "ipo_calendar", "macro") if t in SOURCE_PLAN]
    print(f"预热 {len(codes)} 只 × {len(per_code)} 类 + 全市场类型 {global_types} → 写入本地缓存 ...")
    for c in codes:
        for dt in per_code:
            _warm_one(dt, c)
    for dt in global_types:
        _warm_one(dt, None)


def status():
    print("=== 源健康度 (source × datatype) ===")
    print(f"{'source':<15}{'datatype':<14}{'ok':>5}{'fail':>6}{'streak':>8}{'score':>8}")
    for r in health.all_rows():
        src, dt, ok, fail, sf, lok = r
        print(f"{src:<15}{dt:<14}{ok:>5}{fail:>6}{sf:>8}{health.score(src, dt):>8.2f}")
    print(f"\n缓存条目数: {cache.stat()}")


# =====================================================================
# 6) CLI
# =====================================================================
def _print_result(datatype, data, meta):
    print(f"\n--- {datatype} | status={meta.get('status')} | source={meta.get('source')} ---")
    if meta.get("warning"):
        print(f"  ⚠ {meta['warning']}")
    if data is None:
        print("  (无数据)")
        return
    if isinstance(data, list):
        print(f"  共 {len(data)} 条，示例前 2：")
        for row in data[:2]:
            print("  ", row)
    else:
        print("  ", data)


# =====================================================================
# 7) 快照聚合：把多类型打包成一次调用（盘面前瞻 / 个股体检）
# =====================================================================
# 全市场级类型（无个股代码）
SNAPSHOT_MARKET = ["northbound", "hot_topics", "ths_limit_up", "ths_hot_list",
                   "zt_pool", "zb_pool", "dt_pool", "yzt_pool",
                   "limit_up_sentiment", "em_hot_rank", "industry_rank",
                   "news_cls", "news_em", "ipo_calendar", "macro"]
# 个股级类型（需代码）
SNAPSHOT_STOCK = ["valuation", "financials", "fund_flow", "margin_trading",
                  "holder_num", "lockup", "dividend", "block_trade",
                  "fund_flow_120d", "stock_info", "research_report",
                  "stock_news", "announcements", "irm", "consensus_eps",
                  "hot_concept", "valuation_metrics", "dragon_tiger", "intraday"]


def _one_line_summary(datatype, data):
    """快照用的一行摘要：尽量抽出人类可读的关键字段。"""
    if data is None:
        return "(无数据)"
    if isinstance(data, list):
        n = len(data)
        head = ""
        if n:
            row = data[0]
            if isinstance(row, dict):
                for k in ("name", "code", "rank", "title", "concept", "reason",
                          "zt_count", "latest_hgt", "pct", "heat"):
                    if k in row:
                        head += f"{k}={row[k]} "
                if not head:
                    head = str(row)[:48]
            else:
                head = str(row)[:48]
        return f"{n} 条 | {head}".strip()
    if isinstance(data, dict):
        for k in ("name", "code", "latest_hgt", "total", "forward_pe", "pe_ttm",
                  "zt_count", "title"):
            if k in data:
                return f"{k}={data[k]}"
        return str(list(data.items())[:3])
    return str(data)[:60]


def market_snapshot(date=None, datatypes=None):
    """全市场盘面快照：一次聚合多个市场级类型，返回 {type: {status,source,summary,count}}。"""
    types = datatypes or SNAPSHOT_MARKET
    out = {}
    for t in types:
        try:
            d, m = get(t, date=date)
        except Exception as e:
            out[t] = {"status": "error", "source": None, "summary": str(e)[:80], "count": 0}
            continue
        out[t] = {"status": m.get("status"), "source": m.get("source"),
                  "summary": _one_line_summary(t, d),
                  "count": len(d) if isinstance(d, list) else (1 if d else 0)}
    return out


def stock_snapshot(code, date=None, datatypes=None):
    """个股体检：一次聚合多个个股级类型。"""
    types = datatypes or SNAPSHOT_STOCK
    out = {}
    for t in types:
        try:
            d, m = get(t, code, date=date)
        except Exception as e:
            out[t] = {"status": "error", "source": None, "summary": str(e)[:80], "count": 0}
            continue
        out[t] = {"status": m.get("status"), "source": m.get("source"),
                  "summary": _one_line_summary(t, d),
                  "count": len(d) if isinstance(d, list) else (1 if d else 0)}
    return out


def _print_snapshot(title, snap):
    print(f"\n{'='*70}\n  {title}\n{'='*70}")
    print(f"{'类型':<20}{'状态':<9}{'源':<13}{'摘要'}")
    print("-" * 70)
    fresh = live = stale = failed = err = 0
    for t, v in snap.items():
        st = v.get("status") or "?"
        src = str(v.get("source") or "-")
        print(f"{t:<20}{st:<9}{src:<13}{v.get('summary', '')}")
        if st == "fresh":
            fresh += 1
        elif st == "live":
            live += 1
        elif st == "stale":
            stale += 1
        elif st == "failed":
            failed += 1
        else:
            err += 1
    print("-" * 70)
    print(f"汇总: fresh={fresh} live={live} stale(降级拿到)={stale} "
          f"failed={failed} error={err}  → 拿不到数据的类型: {failed + err}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="抗封禁 A 股数据中枢")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status")
    pw = sub.add_parser("warm")
    pw.add_argument("--codes", default=",".join(WATCHLIST))
    pg = sub.add_parser("get")
    pg.add_argument("datatype")
    pg.add_argument("code", nargs="?")
    pg.add_argument("--start", default="20260701")
    pg.add_argument("--end", default="20260710")
    pg.add_argument("--fq", default="qfq")
    pg.add_argument("--periods", type=int, default=8)
    pg.add_argument("--report_type", default="lrb")
    pg.add_argument("--date", default=None)
    pg.add_argument("--period", default=None)
    pg.add_argument("--top", type=int, default=50)
    psim = sub.add_parser("simulate-block")
    psim.add_argument("datatype")
    psim.add_argument("code", nargs="?")
    psim.add_argument("--start", default="20260701")
    psim.add_argument("--end", default="20260710")
    psim.add_argument("--fq", default="qfq")
    psim.add_argument("--date", default=None)
    psim.add_argument("--period", default=None)
    psim.add_argument("--top", type=int, default=50)
    psnap = sub.add_parser("snapshot", help="盘面快照(全市场) 或 个股体检(--code)")
    psnap.add_argument("--code", default=None)
    psnap.add_argument("--date", default=None)
    psnap.add_argument("--datatypes", default=None, help="逗号分隔的自定义类型列表")
    a = ap.parse_args()
    if a.cmd == "status":
        status()
    elif a.cmd == "warm":
        warm(a.codes.split(","))
    elif a.cmd == "get":
        if a.datatype == "stock_basic":
            data, meta = get("stock_basic")
        else:
            data, meta = get(a.datatype, a.code, start=a.start, end=a.end,
                             fq=a.fq, periods=a.periods, report_type=a.report_type,
                             date=a.date, period=a.period, top=a.top)
        _print_result(a.datatype, data, meta)
    elif a.cmd == "simulate-block":
        # 先把该类全部源置冷却，再取数 → 应走"过期缓存"兜底
        health.force_cooldown(a.datatype)
        if a.datatype == "stock_basic":
            data, meta = get("stock_basic")
        else:
            data, meta = get(a.datatype, a.code, start=a.start, end=a.end, fq=a.fq,
                             date=a.date, period=a.period, top=a.top)
        _print_result(a.datatype, data, meta)
        print(f"\n[演示] 所有实时源已冷却，status={meta.get('status')} "
              f"→ 仍拿到数据: {data is not None}")
    elif a.cmd == "snapshot":
        dts = a.datatypes.split(",") if a.datatypes else None
        if a.code:
            snap = stock_snapshot(a.code, date=a.date, datatypes=dts)
            _print_snapshot(f"个股体检 {a.code}", snap)
        else:
            snap = market_snapshot(date=a.date, datatypes=dts)
            _print_snapshot("盘面快照（全市场）", snap)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
