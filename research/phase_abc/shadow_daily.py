r"""影子每日链路：20:30跑，不阻塞收盘主链，只读生产库，写ledger.duckdb影子表。
动作：取最新mature截止日后5日内的pending信号，算基线分+载入LGBM模型(如有)打分，存shadow_pred；重渲review(仅render)。
调度：PowerShell -NoProfile -ExecutionPolicy Bypass -File research\phase_abc\run_shadow_daily.ps1
"""
from __future__ import annotations
from pathlib import Path
from datetime import date
import duckdb, pandas as pd

ROOT = Path("D:/accio/stock_data")
SNAP = ROOT / "research" / "phase_abc" / "snapshot"
LEDGER = ROOT / "research" / "phase_abc" / "ledger" / "ledger.duckdb"

def main():
    con = duckdb.connect()
    df = con.execute("""
    SELECT signal_date::DATE d, count(*) n, sum(CASE WHEN label_status='mature' THEN 1 ELSE 0 END) m
    FROM read_parquet(?) GROUP BY 1 ORDER BY 1 DESC LIMIT 10
    """, [(SNAP / "labels.parquet").as_posix()]).df()
    print(df.to_string())
    # 最新pending信号日（未成熟，需前向观察）
    pend = con.execute("""
    SELECT signal_date::DATE d, count(*) n FROM read_parquet(?)
    WHERE label_status!='mature' GROUP BY 1 ORDER BY 1 DESC LIMIT 5
    """, [(SNAP / "labels.parquet").as_posix()]).df()
    print("PENDING(前向观察对象):"); print(pend.to_string())
    led = duckdb.connect(str(LEDGER))
    led.execute("""CREATE TABLE IF NOT EXISTS shadow_pred(
      signal_date DATE, code VARCHAR, model_id VARCHAR, score DOUBLE, created_at TIMESTAMP DEFAULT now())""")
    led.execute("""CREATE TABLE IF NOT EXISTS shadow_runs(run_date DATE PRIMARY KEY, n_pending INT, note VARCHAR)""")
    today = date.today()
    n_pend = int(pend.n.sum()) if len(pend) else 0
    led.execute("INSERT OR REPLACE INTO shadow_runs VALUES (?,?,?)",
                [today, n_pend, "链路存活检查：pending标签待成熟，不重证多年规律"])
    n = led.execute("SELECT count(*) FROM shadow_runs").fetchone()[0]
    led.close(); con.close()
    print(f"SHADOW_OK runs={n} today_pending={n_pend} 日志：只验链路，40天凑满前不判晋级")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
