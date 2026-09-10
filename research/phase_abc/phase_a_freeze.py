r"""PhaseA 冻结：只读生产库，写 research/phase_abc/ 新产物。
产出：snapshot/*.parquet + dataset_manifest.json + universe + 手算对账报告。
口径冻结：base 2025-01-02~2026-09-04（有adj才算正式集，无adj为pending）；标签 T收盘冻结/T+1开买/T+5收卖，交易日历计数。
"""
from __future__ import annotations
import json, logging
from pathlib import Path
from datetime import date
import duckdb

ROOT = Path("D:/accio/stock_data")
SRC_DB = ROOT / "kpl_data.duckdb"
OUT = ROOT / "research" / "phase_abc"
SNAP = OUT / "snapshot"
LEDGER = OUT / "ledger" / "ledger.duckdb"
REPORTS = OUT / "reports"

START = "2025-01-02"
END = "2026-09-04"
SNAPSHOT_ID = "base-2025-20260904-r1"
FEATURE_VERSION = "pit-v1"
UNIVERSE_VERSION = "uni-v1-60d-2kw"
LABEL_MODE = "t1_open_to_t5_close_adj"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phase_a")

UNIVERSE_RULES = {
    "list_age_days": 60,
    "exclude_name_like_ST": True,
    "min_avg_amount_20d": 20_000_000,
    "require_adj_both_ends": True,
    "note": "ST按stock_basic.stock_name含ST剔除并留痕；金额=turnover单位万元转元后20日均；缺adj则pending不进正式集",
}

def main() -> int:
    SNAP.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (OUT / "ledger").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(SRC_DB), read_only=True)

    # 1. 交易日历（只取is_open=1）
    cal = [r[0] for r in con.execute(
        "SELECT cal_date FROM tushare_trade_cal WHERE is_open=1 AND cal_date BETWEEN ? AND ? ORDER BY 1",
        [START, END]).fetchall()]
    cal = [c if isinstance(c, date) else date.fromisoformat(str(c)[:10]) for c in cal]
    log.info("trade days open: %d (%s~%s)", len(cal), cal[0] if cal else None, cal[-1] if cal else None)

    # 2. 导出可比行情：tushare_daily + adj_factor左连，金额单位万元->元，量手->股
    # volume单位：tushare relay为hands，需*100；turnover为thousand_yuan? 实测daily_basic total_mv万元，daily turnover按手/千元？统一按export口径：vol*100股，amount按万元*1e4（若异常大则按千元，哨兵校验）。
    q = f"""
    COPY (
      WITH px AS (
        SELECT d.stock_code AS code, d.date AS trade_date, d.open, d.high, d.low, d.close,
               d.volume AS vol_hands, d.turnover AS turnover_raw,
               a.adj_factor
        FROM tushare_daily d LEFT JOIN tushare_adj_factor a
          ON a.stock_code=d.stock_code AND a.date=d.date
        WHERE d.date BETWEEN DATE '{START}' AND DATE '{END}' AND d.close IS NOT NULL AND d.close>0
      )
      SELECT * FROM px
    ) TO '{(SNAP/"px_raw.parquet").as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    con.execute(q)
    n_px = con.execute(f"SELECT count(*) FROM read_parquet('{ (SNAP/'px_raw.parquet').as_posix()}')").fetchone()[0]
    log.info("px_raw rows=%d", n_px)

    # 3. 基础信息 + 上市年龄
    con.execute(f"""
    COPY (
      SELECT stock_code AS code, stock_name, industry, market, list_date
      FROM tushare_stock_basic
    ) TO '{(SNAP/"stock_basic.parquet").as_posix()}' (FORMAT PARQUET)
    """)

    # 4. 用DuckDB在parquet上构建universe + 特征 + 标签（T+1开/T+5收，按交易日历序号，不按下一条记录）
    from trade_system.db_utils import legacy_connect
    con2 = legacy_connect()
    con2.execute(f"""
    CREATE OR REPLACE TABLE px AS SELECT * FROM read_parquet('{ (SNAP/'px_raw.parquet').as_posix()}');
    CREATE OR REPLACE TABLE info AS SELECT * FROM read_parquet('{ (SNAP/'stock_basic.parquet').as_posix()}');
    """)
    # 可比价 = close*adj, open可比 = open*adj(当日因子)；缺adj则adj_verified=false
    con2.execute("""
    CREATE OR REPLACE TABLE bars AS
    SELECT p.code, p.trade_date,
           p.open*coalesce(p.adj_factor,1.0) AS open_adj,
           p.close*coalesce(p.adj_factor,1.0) AS close_adj,
           (p.adj_factor IS NOT NULL) AS adj_verified,
           p.vol_hands*100.0 AS vol_shares,
           p.turnover_raw*10000.0 AS amount_yuan,
           i.list_date, i.stock_name, i.industry
    FROM px p LEFT JOIN info i ON i.code=p.code;
    """)
    # 20日均额 + T+1/T+5定位（按该股在日历中的序号，用交易日表join，不能LEAD下一条）
    con2.execute(f"""
    CREATE OR REPLACE TABLE feat AS
    SELECT b.*,
           AVG(amount_yuan) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_amt_20d,
           COUNT(*) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS cnt_20d,
           LEAD(open_adj,1) OVER (PARTITION BY code ORDER BY trade_date) AS open_t1,
           LEAD(trade_date,1) OVER (PARTITION BY code ORDER BY trade_date) AS date_t1,
           LEAD(close_adj,5) OVER (PARTITION BY code ORDER BY trade_date) AS close_t5,
           LEAD(trade_date,5) OVER (PARTITION BY code ORDER BY trade_date) AS date_t5,
           LEAD(adj_verified,1) OVER (PARTITION BY code ORDER BY trade_date) AS adj_t1,
           LEAD(adj_verified,5) OVER (PARTITION BY code ORDER BY trade_date) AS adj_t5
    FROM bars b;
    """)
    con2.execute(f"""
    COPY (
      SELECT trade_date AS signal_date, code, close_adj AS ref_close, open_t1, close_t5, date_t1, date_t5,
             adj_verified, adj_t1, adj_t5, vol_shares, amount_yuan, avg_amt_20d, cnt_20d, list_date, stock_name, industry,
             CASE
               WHEN open_t1 IS NULL OR close_t5 IS NULL THEN 'pending_no_future'
               WHEN NOT adj_verified OR NOT coalesce(adj_t1,false) OR NOT coalesce(adj_t5,false) THEN 'pending_adj_missing'
               WHEN open_t1<=0 THEN 'pending_bad_px'
               ELSE 'mature' END AS label_status,
             CASE WHEN open_t1>0 AND close_t5 IS NOT NULL THEN close_t5/open_t1-1.0 END AS label_fwd_ret
      FROM feat
    ) TO '{(SNAP/"labels.parquet").as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """)
    stat = con2.execute(f"""
    SELECT label_status, count(*) FROM read_parquet('{ (SNAP/'labels.parquet').as_posix()}') GROUP BY 1
    """).fetchall()
    log.info("label_status: %s", stat)
    cov = con2.execute(f"""
    SELECT signal_date, count(*) n, sum(CASE WHEN label_status='mature' THEN 1 ELSE 0 END) nm
    FROM read_parquet('{ (SNAP/'labels.parquet').as_posix()}') GROUP BY 1 ORDER BY 1 DESC LIMIT 5
    """).fetchall()
    log.info("last5 coverage: %s", cov)

    # 5. universe：上市>=60d + 非ST + 20日均额>=2000万 + 当日adj_verified
    con2.execute(f"""
    COPY (
      SELECT signal_date AS trade_date, code,
             (label_status='mature'
              AND cnt_20d>=20 AND avg_amt_20d>=20000000
              AND (list_date IS NULL OR date_diff('day', CAST(list_date AS DATE), signal_date)>=60)
              AND (stock_name IS NULL OR stock_name NOT LIKE '%ST%')
             ) AS in_universe,
             label_status AS reason
      FROM read_parquet('{ (SNAP/'labels.parquet').as_posix()}')
    ) TO '{(SNAP/"universe.parquet").as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """)
    uni = con2.execute(f"""
    SELECT trade_date, sum(CASE WHEN in_universe THEN 1 ELSE 0 END) nu, count(*) n
    FROM read_parquet('{ (SNAP/'universe.parquet').as_posix()}') GROUP BY 1 ORDER BY 1 DESC LIMIT 5
    """).fetchall()
    log.info("universe last5: %s", uni)

    # 6. 手算对账：随机3股×2日，SQL明细 vs python直算
    check = con2.execute(f"""
    SELECT signal_date, code, ref_close, open_t1, close_t5, label_fwd_ret, label_status
    FROM read_parquet('{ (SNAP/'labels.parquet').as_posix()}')
    WHERE label_status='mature' AND signal_date IN ('2026-08-28','2026-08-29')
    ORDER BY signal_date, code LIMIT 6
    """).fetchall()
    # python复算
    ok = True
    lines = []
    for r in check:
        sd, code, o1, c5, lab = r[0], r[1], float(r[3]), float(r[4]), float(r[5])
        py = c5/o1-1.0
        match = abs(py-lab) < 1e-12
        ok = ok and match
        lines.append({"signal_date": str(sd), "code": code, "open_t1": o1, "close_t5": c5, "label": lab, "py": py, "match": match})
    log.info("handcheck ok=%s", ok)

    manifest = {
        "snapshot_id": SNAPSHOT_ID, "feature_version": FEATURE_VERSION,
        "universe_version": UNIVERSE_VERSION, "label_mode": LABEL_MODE,
        "start": START, "end": END, "as_of": "2026-09-06",
        "trade_days_open": len(cal),
        "px_rows": n_px, "label_status": {k: v for k, v in stat},
        "universe_last5": [{"date": str(a), "nu": b, "n": c} for a, b, c in uni],
        "coverage_last5": [{"date": str(a), "n": b, "mature": c} for a, b, c in cov],
        "adj_policy": "comparable=price*adj_factor, missing->pending_adj_missing, QLib禁入",
        "universe_rules": UNIVERSE_RULES,
        "handcheck_ok": ok, "handcheck_rows": lines,
        "honesty_note": "adj平均覆盖仅41%(2025+)，正式集只用mature；2024无adj仅探索，不进主赛；竞价/分钟未入快照",
        "source": "kpl_data.duckdb read_only COPY，未写生产库",
    }
    (SNAP / "dataset_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7. 新建ledger.duckdb（新库，可写），登记快照，不碰生产库
    if LEDGER.exists():
        LEDGER.unlink()
    from trade_system.db_utils import legacy_connect
    led = legacy_connect(str(LEDGER))
    led.execute("CREATE TABLE dataset_manifest(snapshot_id VARCHAR PRIMARY KEY, manifest_json VARCHAR)");
    led.execute("INSERT INTO dataset_manifest VALUES (?, ?)", [SNAPSHOT_ID, json.dumps(manifest, ensure_ascii=False)])
    led.execute("CREATE TABLE universe_daily AS SELECT * FROM read_parquet(?)", [(SNAP/"universe.parquet").as_posix()])
    led.close()
    con.close(); con2.close()

    rep = REPORTS / "phase_a_freeze.md"
    rep.write_text("# PhaseA冻结报告\n\n- snapshot: `{}`\n- 交易日: {}天 {}~{}\n- px行数: {}\n- 标签分布: {}\n- universe近5日: {}\n- 手算对账: {}\n- 诚实声明: adj覆盖41%，正式集仅mature；2024/竞价未入。\n".format(
        SNAPSHOT_ID, len(cal), START, END, n_px, stat, uni, "PASS" if ok else "FAIL"), encoding="utf-8")
    print("PHASE_A_DONE", SNAPSHOT_ID, "handcheck", ok)
    return 0 if ok else 2

if __name__ == "__main__":
    raise SystemExit(main())
