# -*- coding: utf-8 -*-
"""
resilient_stock_data.py —— 抗封禁 A 股数据中枢（真正"拿得到数据"的那一层）

=====================================================================
为什么这一层能解决「一要数据就被封、拿不到」？
---------------------------------------------------------------------
旧 stock_data.py 只做了「多源降级」，但每个请求都直冲网络；一旦所有实时源
同时被沙箱/对方限掉，它就空手而归——这正是你遇到的痛点。

本模块在「多源降级」之外再加四道保险：

  ① 本地缓存（SQLite，落盘）
       命中且未过期  → 0 网络，直接返回，绝不因网络被封而失败
       过期但存在    → 实时回源刷新；刷新失败 → 仍返回【过期缓存】
      （关键：只要你**曾经**取过，就永远拿得到，哪怕此刻全网被封）

  ② 健康度自适应（HealthRegistry，落盘）
       实时记录每个 (源 × 数据类型) 的成败/延迟，按分数动态排序；
       连续失败≥3次自动进入冷却（暂时跳过），复活后再启用。
       坏源不再浪费超时，好源优先。

  ③ 礼貌限流（RateLimiter）
       每源最小请求间隔 + 全局并发信号量，避免 burst 触发对方限频/封 IP——
       这正是"被封"的常见根源，从源头降低概率。

  ④ 预热（warm）
       对自选标的预先把 K线/估值/财务 灌进缓存；你"要数据"时已是本地命中，
       瞬时返回，根本不碰网络。

四者叠加 → 多源容灾（旧） + 缓存优先（新） + 自适应（新） + 限流（新）
= 即便所有实时源同时阵亡，也能从缓存稳定出数。

=====================================================================
用法：
  from resilient_stock_data import get, warm, status
  warm(["600519","000001"])                       # 先预热（可选，但强烈建议）
  bars, meta = get("kline", "600519", "20260701", "20260710")
  val,  meta = get("valuation", "600519")          # 现价/PE/PB/市值/涨跌停
  fin,  meta = get("financials", "600519")          # 业绩报表
  ff,   meta = get("fund_flow", "600519")           # 资金流
  st,   meta = get("statements", "600519", report_type="fzb")  # 资产负债表(新浪)
  basic,meta = get("stock_basic")                  # 全量股票列表(本地镜像)
  nb,   meta = get("northbound")                   # 北向资金当日分钟净买入(同花顺)
  hot,  meta = get("hot_topics")                   # 当日热点题材归因(同花顺, 全市场)
  eps,  meta = get("consensus_eps", "600519")      # 机构一致预期EPS(同花顺)
  # —— 全量已接入（零 token）——
  dt,   meta = get("dragon_tiger", "600519", date="2026-07-10")   # 个股龙虎榜+席位+机构
  dtd,  meta = get("dragon_tiger_daily", date="2026-07-10")       # 全市场龙虎榜
  mg,   meta = get("margin_trading", "600519")     # 融资融券
  hn,   meta = get("holder_num", "600519")         # 股东户数
  lk,   meta = get("lockup", "600519")             # 限售解禁
  dv,   meta = get("dividend", "600519")           # 分红送转
  bt,   meta = get("block_trade", "600519")        # 大宗交易
  f120, meta = get("fund_flow_120d", "600519")     # 个股资金流120日
  si,   meta = get("stock_info", "600519")         # 个股基本面
  ir,   meta = get("industry_rank")                # 行业板块排名
  zt,   meta = get("zt_pool", date="20260710")     # 涨停池
  zb,   meta = get("zb_pool", date="20260710")     # 炸板池
  dtp,  meta = get("dt_pool", date="20260710")     # 跌停池
  yzt,  meta = get("yzt_pool", date="20260710")    # 昨涨停池
  sus,  meta = get("limit_up_sentiment", date="20260710")  # 打板情绪(炸板率/连板高度)
  rp,   meta = get("research_report", "600519")    # 研报
  nw,   meta = get("stock_news", "600519")         # 个股新闻
  cls,  meta = get("news_cls")                     # 财联社电报
  em7,  meta = get("news_em")                      # 东财7x24
  ann,  meta = get("announcements", "600519")      # 巨潮公告
  irm,  meta = get("irm", "600519")                # 互动易问答
  tlu,  meta = get("ths_limit_up", date="20260710")# 同花顺涨停揭秘(原因/封板率)
  thl,  meta = get("ths_hot_list")                 # 同花顺热榜
  ehr,  meta = get("em_hot_rank")                  # 东财人气榜
  hc,   meta = get("hot_concept", "600519")        # 个股热门概念命中
  vt,   meta = get("valuation_metrics", "600519") # 前向PE/PEG/PE消化(本地算)
  # meta["status"]: fresh / live / refreshed / stale(降级!但拿到数据) / failed

CLI：
  python resilient_stock_data.py status
  python resilient_stock_data.py warm [--codes 600519,000001]
  python resilient_stock_data.py get <datatype> <code> [--start .. --end .. --fq .. --periods ..]
  python resilient_stock_data.py simulate-block <datatype> <code>   # 演示全源阵亡仍出数

运行环境：需在装好 pytdx/mootdx/baostock 的 venv 中执行
  （venv/Scripts/python.exe resilient_stock_data.py ...）
本机运行可设环境变量 STOCK_DATA_LOCAL=1 启用东方财富 K 线源（沙箱默认关闭）。
"""
from __future__ import annotations
import logging
import os, json, time, sqlite3, threading, datetime
from concurrent.futures import ThreadPoolExecutor


logger = logging.getLogger(__name__)

# ---- 路径 / 环境 ----
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(WORKSPACE, ".stock_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
DB_PATH = os.path.join(CACHE_DIR, "resilient.db")
LOCAL_MODE = os.environ.get("STOCK_DATA_LOCAL") == "1"

# ---- 低层源（来自 stock_data.py，已含多源与超时） ----
from .stock_data_sources import (  # noqa: E402
    _from_baostock, _from_pytdx, _from_tencent, _from_sina,
    _from_eastmoney, _from_baidu, _from_tushare_relay,
    _from_tushare_moneyflow, _from_tushare_sector_flow,
    _from_sina_fund_flow, _from_tencent_valuation, _from_tushare_basic,
    _from_ths_northbound, _from_ths_hot_reason, _from_ths_eps_forecast,
    get_financials, get_fund_flow, get_financial_statements,
    _norm_code, _norm_date, TUSHARE_TOKEN, _tushare_query,
    # —— 新增：东财数据中心 / 新闻 / 公告 / 涨停池 / 同花顺 / 期权 ——
    _from_em_dragon_tiger, _from_em_dragon_tiger_daily, _from_em_margin,
    _from_em_holder, _from_em_lockup, _from_em_dividend, _from_em_block_trade,
    _from_em_fund_flow_120d, _from_em_stock_info, _from_em_industry_rank,
    _from_em_sector_flow,
    _from_em_zt_pool, _from_em_zb_pool, _from_em_dt_pool, _from_em_yzt_pool,
    _from_em_limit_up_sentiment, _from_em_reports, _from_em_stock_news,
    _from_em_flash, _from_cls_telegraph, _from_cninfo_announcements,
    _from_cninfo_irm, _from_ths_limit_up, _from_ths_hot_list,
    _from_em_hot_rank, _from_em_hot_concept,
    _from_sina_option_tquote, _from_sina_option_greeks,
    _from_local_valuation_metrics,
    # —— P0 新增：分时双源 + 核心单源异后端第二源 ——
    _from_em_trends, _from_pytdx_minutes, _from_em_all_stocks, _from_em_northbound,
    # —— P1 新增：覆盖面缺口类型 + 核心单源异后端第二源 ——
    _from_em_index_kline, _from_em_index_spot, _from_em_etf_kline, _from_em_etf_info,
    _from_em_cb_kline, _from_em_cb_quote, _from_em_forecast, _from_em_express,
    _from_em_top10_holders, _from_em_northbound_hist, _from_tencent_bid_ask, _from_em_bid_ask,
    _from_em_irm, _from_em_announcements, _from_em_option_tquote, _from_em_option_greeks,
    _from_em_statements, _from_em_hot_topics, _from_em_hot_list,
    _from_em_ipo, _from_em_macro,
    # —— P3 新增：cninfo/ths 异后端第二源（分红/解禁/大宗→巨潮；融资融券/股东户数→同花顺）——
    _from_cninfo_dividend, _from_cninfo_lockup, _from_cninfo_block_trade,
    _from_ths_margin_trading, _from_ths_holder_num,
)
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
    """源无关的逻辑缓存：任何源取回的数据都写入同一逻辑 key，互相填充。"""

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
                    "eastmoney": 0.18, "tushare_relay": 0.65, "baidu": 0.30,
                    "ths": 0.20, "cninfo": 0.20, "cls": 0.20,
                    "sina_option": 0.15, "local": 0.0}
        self.last = {}
        self.lock = threading.Lock()
        self.sem = threading.Semaphore(4)   # 全局并发上限

    def acquire(self, source):
        mi = self.min.get(source, 0.15)
        # Coordinate with collectors started by another process.  The local
        # lock below still protects threads inside this process.
        shared_host_limiter.acquire(source, mi)
        with self.lock:
            now = time.time()
            wait = mi - (now - self.last.get(source, 0.0))
            if wait > 0:
                time.sleep(wait)
            self.last[source] = time.time()
        self.sem.acquire()
        return self

    def release(self, source):
        self.sem.release()


rate = RateLimiter()


def _call(fn, timeout, retries=1, backoff=0.2):
    """在线程里跑源，超时/异常都返回 None，绝不阻塞主流程。
    源级退避重试：瞬时失败（超时/抖动）自动重试，降低误判为封禁的概率。"""
    last = None
    for attempt in range(retries + 1):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            fut = executor.submit(fn)
            last = fut.result(timeout=timeout)
            if last:
                return last
        except Exception:
            last = None
        finally:
            # Exiting a ThreadPoolExecutor context uses shutdown(wait=True),
            # which defeats the timeout when a provider socket is stuck.  Do
            # not wait for an untrusted network worker here.
            executor.shutdown(wait=False, cancel_futures=True)
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    return last


def adaptive_ttl(datatype):
    """自适应 TTL：某类数据源普遍失败时，延长缓存有效期，更依赖'过期缓存兜底'，
    避免对已挂源反复打扰（配合 HealthRegistry 冷却形成双保险）。"""
    base = TTL.get(datatype, 3600)
    rel = [r for r in health.all_rows() if r[1] == datatype]
    if not rel:
        return base
    ok = sum(r[2] for r in rel)      # success 列
    fail = sum(r[3] for r in rel)    # fail 列
    total = ok + fail
    if total == 0:
        return base
    fail_ratio = fail / total
    if fail_ratio > 0.8:
        return int(base * 10)
    if fail_ratio > 0.5:
        return int(base * 4)
    if fail_ratio > 0.2:
        return int(base * 2)
    return base


# =====================================================================
# 3) 数据源计划（SOURCE_PLAN）：每个数据类型 → 有序源列表
#    顺序只是初始顺序，运行时会被 health 动态重排。
# =====================================================================
def _relay_daily_basic(code, trade_date=None):
    """估值冗余源：TuShare daily_basic 市值原值为万元，此兼容输出为亿元。"""
    if not TUSHARE_TOKEN:
        return None
    mkt, pure = _norm_code(code)
    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[mkt]
    ts_code = f"{pure}.{exchange}"
    td = trade_date or datetime.date.today().strftime("%Y%m%d")
    params = {"ts_code": ts_code, "trade_date": td}
    rows = _tushare_query(
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
        "turnover": g("turnover_rate"), "trade_date": td, "_src": "tushare_relay",
        "raw": row,
    }
    from trade_system.units import market_caps
    result.update(market_caps(result))
    return result


def _plan_kline(code, start="20260101", end="20500101", fq="qfq", **kw):
    srcs = [
        ("baostock", lambda: _from_baostock(code, start, end, fq)),
        ("pytdx", lambda: _from_pytdx(code, start, end, fq)),
        ("tencent", lambda: _from_tencent(code, start, end, fq)),
        ("sina", lambda: _from_sina(code, start, end, fq)),
        ("tushare_relay", lambda: _from_tushare_relay(code, start, end, fq)),
        ("baidu", lambda: _from_baidu(code, start, end, fq)),
    ]
    if LOCAL_MODE:   # 本机运行东方财富 K 线自动复活
        srcs.append(("eastmoney", lambda: _from_eastmoney(code, start, end, fq)))
    return srcs


def _plan_valuation(code, **kw):
    return [("tencent", lambda: _from_tencent_valuation(code)),
            ("tushare_relay", lambda: _relay_daily_basic(code))]


def _plan_financials(code, periods=8, **kw):
    return [("eastmoney", lambda: get_financials(code, periods))]


def _plan_fund_flow(code, periods=10, **kw):
    return [("eastmoney", lambda: get_fund_flow(code, periods)),
            ("sina", lambda: _from_sina_fund_flow(code, periods))]


def _plan_statements(code, report_type="lrb", periods=8, **kw):
    return [("sina", lambda: get_financial_statements(code, report_type, periods))]


def _plan_stock_basic(code=None, list_status="L", **kw):
    # 异后端双源：tushare_relay + 东财 push2 clist（不同供应商，抗封）
    return [("tushare_relay", lambda: _from_tushare_basic(list_status)),
            ("eastmoney", lambda: _from_em_all_stocks())]


def _plan_northbound(code=None, **kw):
    # 全市场级（无个股代码），双源：同花顺 + 东财 kamt（异供应商）
    return [("ths", lambda: _from_ths_northbound()),
            ("eastmoney", lambda: _from_em_northbound())]


def _plan_hot_topics(code=None, date=None, **kw):
    # 全市场级（无个股代码），同花顺当日热点题材归因；date='YYYY-MM-DD'，None=今天
    d = date or (kw.get("date") or datetime.date.today().strftime("%Y-%m-%d"))
    return [("ths", lambda: _from_ths_hot_reason(d))]


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
    # 股东户数：东财 datacenter（主） + 同花顺 dataapi（异后端备）
    return [("eastmoney", lambda: _from_em_holder(code)),
            ("ths", lambda: _from_ths_holder_num(code))]

def _plan_lockup(code, **kw):
    # 限售解禁：东财 datacenter（主） + 巨潮 webapi（异后端备）
    return [("eastmoney", lambda: _from_em_lockup(code)),
            ("cninfo", lambda: _from_cninfo_lockup(code))]

def _plan_dividend(code, **kw):
    # 分红送转：东财 datacenter（主） + 巨潮 webapi（异后端备）
    return [("eastmoney", lambda: _from_em_dividend(code)),
            ("cninfo", lambda: _from_cninfo_dividend(code))]

def _plan_block_trade(code, **kw):
    # 大宗交易：东财 datacenter（主） + 巨潮 webapi（异后端备）
    return [("eastmoney", lambda: _from_em_block_trade(code)),
            ("cninfo", lambda: _from_cninfo_block_trade(code))]

def _plan_fund_flow_120d(code, **kw):
    return [("eastmoney", lambda: _from_em_fund_flow_120d(code))]

def _plan_stock_info(code, **kw):
    return [("eastmoney", lambda: _from_em_stock_info(code))]

def _plan_industry_rank(code=None, **kw):
    return [("eastmoney", lambda: _from_em_industry_rank())]

def _plan_zt_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_zt_pool(d)),
            ("ths", lambda: _from_ths_limit_up(d))]

def _plan_zb_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_zb_pool(d)),
            ("ths", lambda: _from_ths_limit_up(d))]

def _plan_dt_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_dt_pool(d)),
            ("ths", lambda: _from_ths_limit_up(d))]

def _plan_yzt_pool(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_yzt_pool(d)),
            ("ths", lambda: _from_ths_limit_up(d))]

def _plan_limit_up_sentiment(code=None, date=None, **kw):
    d = date or _d("%Y%m%d")
    return [("eastmoney", lambda: _from_em_limit_up_sentiment(d)),
            ("ths", lambda: _from_ths_limit_up(d))]

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

def _plan_announcements(code, **kw):
    return [("cninfo", lambda: _from_cninfo_announcements(code))]

def _plan_irm(code, **kw):
    return [("cninfo", lambda: _from_cninfo_irm(code))]

def _plan_ths_limit_up(code=None, date=None, **kw):
    # 双源：同花顺 + 东财涨停池（异供应商，东财被封切同花顺、反之亦然）
    return [("ths", lambda: _from_ths_limit_up(date or _d("%Y%m%d"))),
            ("eastmoney", lambda: _from_em_zt_pool(date or _d("%Y%m%d")))]


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


def _plan_forecast(code, **kw):
    return [("eastmoney", lambda: _from_em_forecast(code))]


def _plan_express(code, **kw):
    return [("eastmoney", lambda: _from_em_express(code))]


def _plan_top10_holders(code, **kw):
    return [("eastmoney", lambda: _from_em_top10_holders(code))]


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
def _plan_statements_v2(code, report_type="lrb", periods=8, **kw):
    return [("sina", lambda: get_financial_statements(code, report_type, periods)),
            ("eastmoney", lambda: _from_em_statements(code, report_type, periods))]


def _plan_irm_v2(code, **kw):
    return [("cninfo", lambda: _from_cninfo_irm(code)),
            ("eastmoney", lambda: _from_em_irm(code))]


def _plan_announcements_v2(code, **kw):
    return [("cninfo", lambda: _from_cninfo_announcements(code)),
            ("eastmoney", lambda: _from_em_announcements(code))]


def _plan_option_tquote_v2(code, **kw):
    return [("sina_option", lambda: _from_sina_option_tquote(code)),
            ("eastmoney", lambda: _from_em_option_tquote(code))]


def _plan_option_greeks_v2(code, **kw):
    return [("sina_option", lambda: _from_sina_option_greeks(code)),
            ("eastmoney", lambda: _from_em_option_greeks(code))]


def _plan_hot_topics_v2(code=None, date=None, **kw):
    d = date or (kw.get("date") or datetime.date.today().strftime("%Y-%m-%d"))
    return [("ths", lambda: _from_ths_hot_reason(d)),
            ("eastmoney", lambda: _from_em_hot_topics())]


def _plan_ths_hot_list_v2(code=None, period=None, **kw):
    return [("ths", lambda: _from_ths_hot_list(period or "hour")),
            ("eastmoney", lambda: _from_em_hot_list(period or "hour"))]


def _plan_fund_flow_120d_v2(code, **kw):
    return [("eastmoney", lambda: _from_em_fund_flow_120d(code)),
            ("sina", lambda: _from_sina_fund_flow(code, days=120)),
            ("tushare_relay", lambda: _from_tushare_moneyflow(code, days=120))]

def _plan_stock_flow(code, **kw):
    """Stable project-facing alias for per-stock capital flow."""
    return _plan_fund_flow_120d_v2(code, **kw)

def _plan_sector_flow(code=None, **kw):
    """Market-wide sector capital flow, independent of the Tushare relay."""
    return [("eastmoney", lambda: _from_em_sector_flow(kw.get("top_n", 200))),
            ("tushare_relay", lambda: _from_tushare_sector_flow(kw.get("date"), kw.get("top_n", 300)))]

def _plan_ths_hot_list(code=None, period=None, **kw):
    return [("ths", lambda: _from_ths_hot_list(period or "hour"))]

def _plan_em_hot_rank(code=None, top=None, **kw):
    # 东财人气榜 ↔ 同花顺人气榜（异后端互备）
    return [("eastmoney", lambda: _from_em_hot_rank(top or 50)),
            ("ths", lambda: _from_ths_hot_list("hour"))]

def _plan_hot_concept(code, **kw):
    # 东财题材 ↔ 同花顺题材（异后端互备）
    return [("eastmoney", lambda: _from_em_hot_concept(code)),
            ("ths", lambda: _from_ths_hot_list("hour"))]

def _plan_option_tquote(code, **kw):
    return [("sina_option", lambda: _from_sina_option_tquote(code))]

def _plan_option_greeks(code, **kw):
    return [("sina_option", lambda: _from_sina_option_greeks(code))]

def _plan_valuation_metrics(code, **kw):
    return [("local", lambda: _from_local_valuation_metrics(code))]


SOURCE_PLAN = {
    "kline": _plan_kline,
    "valuation": _plan_valuation,
    "financials": _plan_financials,
    "fund_flow": _plan_fund_flow,
    "statements": _plan_statements_v2,
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
    "stock_flow": _plan_stock_flow,
    "sector_flow": _plan_sector_flow,
    "stock_info": _plan_stock_info,
    # —— 行业 / 打板 / 舆情 ——
    "industry_rank": _plan_industry_rank,
    "zt_pool": _plan_zt_pool,
    "zb_pool": _plan_zb_pool,
    "dt_pool": _plan_dt_pool,
    "yzt_pool": _plan_yzt_pool,
    "limit_up_sentiment": _plan_limit_up_sentiment,
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
    "forecast": _plan_forecast,
    "express": _plan_express,
    "top10_holders": _plan_top10_holders,
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
    "zt_pool": 8 * 3600, "zb_pool": 8 * 3600, "dt_pool": 8 * 3600,
    "yzt_pool": 8 * 3600, "limit_up_sentiment": 8 * 3600,
    "research_report": 7 * 24 * 3600, "stock_news": 6 * 3600,
    "news_cls": 6 * 3600, "news_em": 6 * 3600,
    "announcements": 7 * 24 * 3600, "irm": 7 * 24 * 3600,
    "ths_limit_up": 8 * 3600, "ths_hot_list": 3600, "em_hot_rank": 3600,
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
    """数据类型相关的缓存键：纳入影响结果的参数（区间/复权/日期/周期/排名等），
    避免 key 不一致导致缓存命中失败。"""
    parts = [datatype]
    if code is not None:
        parts.append(str(code))
    # 各类型真正影响结果的参数（其余参数变化不改变结果，不进 key）
    RELEVANT = {
        "kline": ("fq",),
        "financials": ("periods", "report_type"),
        "fund_flow": ("periods",),
        "statements": ("periods", "report_type"),
        "stock_basic": ("list_status",),
        "hot_topics": ("date",),
        "dragon_tiger": ("date",),
        "dragon_tiger_daily": ("date",),
        "zt_pool": ("date",), "zb_pool": ("date",), "dt_pool": ("date",),
        "yzt_pool": ("date",), "ths_limit_up": ("date",),
        "limit_up_sentiment": ("date",),
    "ths_hot_list": ("period",), "em_hot_rank": ("top",),
    "valuation_metrics": (), "valuation": (), "intraday": ("date",),
    # —— P1 覆盖面缺口类型（RELEVANT）——
    "index_kline": ("fq",), "etf_kline": ("fq",), "cb_kline": ("fq",),
    "index_spot": (), "etf_info": (), "cb_quote": (),
    "forecast": (), "express": (), "top10_holders": (),
    "northbound_hist": (), "bid_ask": (),
    "ipo_calendar": ("start", "end"), "macro": ("indicators",),
}
    relevant = RELEVANT.get(datatype, ())
    for k in sorted(kwargs.keys()):
        if k in relevant:
            parts.append(f"{k}={kwargs[k]}")
    return ":".join(parts)


def get(datatype, code=None, ttl=None, timeout_per=None, **kwargs):
    """统一取数。返回 (data, meta)。
    meta["status"]:
      fresh     —— 缓存命中且未过期（0 网络）
      live      —— 实时取回（此前无缓存）
      refreshed —— 缓存过期，本次实时刷新成功
      stale     —— ⚠ 所有实时源失败，已降级返回【过期缓存】（仍拿到数据！）
      failed    —— 彻底失败（从未取过且无任何源可用）
    """
    if datatype not in SOURCE_PLAN:
        raise ValueError(f"未知数据类型: {datatype}，可选: {list(SOURCE_PLAN)}")
    timeout_per = timeout_per if timeout_per is not None else TIMEOUT.get(datatype, 10)
    # The default keeps a reusable full-history K-line cache.  Stage runners can
    # set full_history=False for a bounded daily slice; that key includes the
    # requested window so a short slice can never masquerade as full history.
    full_history = bool(kwargs.pop("full_history", True))
    # Full-history K-line payloads are canonicalized in DuckDB/Parquet.  Do
    # not duplicate hundreds of megabytes of JSON in the small SQLite
    # resilience cache; bounded daily slices may still use the cache.
    cache_allowed = not (datatype in {"kline", "index_kline", "etf_kline", "cb_kline"} and full_history)
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
    if datatype in _PERIOD_TYPES and "period" not in kwargs:
        kwargs["period"] = "hour"
    ttl = ttl if ttl is not None else adaptive_ttl(datatype)

    # kline 按"全量历史"缓存（key 不含起止），任意区间复用同一份 → stale 兜底才稳健
    if datatype in {"kline", "index_kline", "etf_kline", "cb_kline"}:
        fq = kwargs.get("fq", "qfq")
        sd = _norm_date(kwargs.get("start", "19900101"))
        ed = _norm_date(kwargs.get("end", "20500101"))
        key = f"{datatype}:{code}:{fq}" if full_history else f"{datatype}:{code}:{fq}:{sd}:{ed}"
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
    val, ts = cache.get(key) if cache_allowed else (None, 0)
    if val is not None and (time.time() - ts) < ttl and (
        datatype not in {"kline", "index_kline", "etf_kline", "cb_kline"}
        or _contract_matches(val)
    ):
        return post_filter(val), {"source": "cache", "status": "fresh", "cached_at": ts}

    # ② 实时降级（按健康度动态排序）
    spec = SOURCE_PLAN[datatype]
    sources = spec(code, **fetch_kwargs) if callable(spec) else list(spec)
    ordered = health.order(sources, datatype)
    eligible = [(name, fn) for name, fn in ordered if not health.is_cooldown(name, datatype)]
    if not eligible and ordered:
        # Half-open recovery: allow one probe only.  Probing every cooled
        # source at once defeats the circuit breaker and can trigger another
        # burst against all providers.
        eligible = ordered[:1]
    for name, fn in eligible:
        rate.acquire(name)
        t0 = time.time()
        try:
            out = _call(fn, timeout_per)
        finally:
            rate.release(name)
        dt = time.time() - t0
        if out:
            if datatype in {"kline", "index_kline", "etf_kline", "cb_kline"} and not _contract_matches(out):
                health.record(name, datatype, False, dt)
                logger.warning("%s returned rows without a verified K-line unit/adjustment contract", name)
                continue
            if cache_allowed:
                cache.put(key, out)
            health.record(name, datatype, True, dt)
            if val is not None:
                return post_filter(out), {"source": name, "status": "refreshed",
                                          "cached_at": ts, "latency": round(dt, 3)}
            return post_filter(out), {"source": name, "status": "live",
                                     "cached_at": time.time(), "latency": round(dt, 3)}
        else:
            health.record(name, datatype, False, dt)

    # ③ 全源失败 → 过期缓存兜底（关键：永不空手而归）
    if val is not None:
        return post_filter(val), {"source": "cache", "status": "stale", "cached_at": ts,
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


def snapshot_to_html(codes=None, date=None, out_path="snapshot_dashboard.html"):
    """把盘面快照渲染成自包含 HTML 看板（浏览器直接打开）。
    颜色：live/fresh=绿（实时/缓存命中拿到），stale=琥珀（全部源阵亡·降级缓存兜底），failed=红（彻底空手）。"""
    codes = codes or WATCHLIST
    mkt = market_snapshot(date=date)
    stocks = {c: stock_snapshot(c, date=date) for c in codes}

    def row(t, v):
        st = v.get("status") or "?"
        color = {"live": "#2e7d32", "fresh": "#2e7d32", "stale": "#ef6c00",
                 "failed": "#c62828"}.get(st, "#555")
        return (f"<tr><td>{t}</td>"
                f"<td style='color:{color};font-weight:600'>{st}</td>"
                f"<td>{v.get('source') or '-'}</td>"
                f"<td>{v.get('summary', '')}</td>"
                f"<td>{v.get('count', 0)}</td></tr>")

    mkt_rows = "".join(row(t, v) for t, v in mkt.items())
    sec = ""
    for c, snap in stocks.items():
        srows = "".join(row(t, v) for t, v in snap.items())
        sec += (f"<h3>个股体检 {c}</h3>"
                f"<table><tr><th>类型</th><th>状态</th><th>源</th>"
                f"<th>摘要</th><th>条数</th></tr>{srows}</table>")
    html = f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>A股数据中枢 · 盘面看板</title><style>
body{{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:24px;background:#fafafa}}
h1{{color:#1a237e}} h3{{color:#283593;margin-top:24px}}
table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px #0001}}
th,td{{border:1px solid #e0e0e0;padding:6px 10px;text-align:left;font-size:13px}}
th{{background:#e8eaf6}} tr:nth-child(even){{background:#f5f5f5}}
.meta{{color:#666;font-size:12px}}
</style></head><body>
<h1>抗封禁 A 股数据中枢 · 盘面看板</h1>
<p class=meta>生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜
状态图例：<span style='color:#2e7d32'>live/fresh=实时/缓存命中</span>，
<span style='color:#ef6c00'>stale=全部源阵亡·降级缓存兜底</span>，
<span style='color:#c62828'>failed=彻底空手</span></p>
<h3>全市场盘面</h3>
<table><tr><th>类型</th><th>状态</th><th>源</th><th>摘要</th><th>条数</th></tr>{mkt_rows}</table>
{sec}
</body></html>"""
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass
    return out_path


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
    pdash = sub.add_parser("dashboard", help="渲染盘面看板 HTML（snapshot_to_html）")
    pdash.add_argument("--codes", default=",".join(WATCHLIST))
    pdash.add_argument("--out", default="snapshot_dashboard.html")
    sub.add_parser("selftest", help="运行离线回归测试(_test_offline.py)")
    sub.add_parser("run", help="一次性预热+快照，可直接当定时任务体")

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
    elif a.cmd == "dashboard":
        path = snapshot_to_html(a.codes.split(","), out_path=a.out)
        print(f"看板已生成: {path}")
    elif a.cmd == "selftest":
        import subprocess, sys
        print("运行离线回归测试 (_test_offline.py) ...\n")
        rc = subprocess.run([sys.executable, "_test_offline.py"]).returncode
        print(f"\nselftest 退出码: {rc}  (0=全部通过)")
    elif a.cmd == "run":
        # 定时任务友好：先预热，再出一份盘面快照，全程不抛异常
        try:
            warm()
        except Exception as e:
            print(f"[warm] 跳过: {e}")
        snap = market_snapshot()
        _print_snapshot("盘面快照（全市场 / 定时任务）", snap)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
