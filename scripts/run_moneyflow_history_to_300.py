"""Unattended, rate-safe historical moneyflow backfill to the QLib horizon."""
from __future__ import annotations
from datetime import date
from pathlib import Path
import subprocess
import time
import duckdb

ROOT = Path(__file__).resolve().parents[1]
PYTHON = r"D:\anaconda\python.exe"
DB = ROOT / "kpl_data.duckdb"
LOG = ROOT / "reports" / "moneyflow_history_unattended.log"
START, END = date(2024, 1, 2), date(2025, 12, 31)

def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
      for pass_no in range(1, 6):
        con = duckdb.connect(str(DB), read_only=True)
        try:
            dates = [str(r[0])[:10] for r in con.execute(
                """WITH trading_days AS (
                     SELECT cal_date AS trade_date FROM tushare_trade_cal WHERE is_open=true AND cal_date BETWEEN ? AND ?
                     UNION SELECT date AS trade_date FROM tushare_daily WHERE date BETWEEN ? AND ?
                   )
                   SELECT trade_date FROM trading_days d
                   WHERE NOT EXISTS (SELECT 1 FROM history_fetch_checkpoint h
                                     WHERE h.dataset='moneyflow' AND h.trade_date=d.trade_date AND h.status='success')
                   ORDER BY trade_date""", [START.isoformat(), END.isoformat(), START.isoformat(), END.isoformat()]
            ).fetchall()]
        finally:
            con.close()
        log.write(f"START_PASS {pass_no} pending={len(dates)}\n")
        if not dates:
            break
        for day in dates:
            while (ROOT / "kpl_data.duckdb.pipeline.lock").exists():
                time.sleep(30)
            report = ROOT / "reports" / f"moneyflow_history_{day}.md"
            cmd = [PYTHON, str(ROOT / "scripts" / "backfill_2026_tushare.py"),
                   "--db", str(DB), "--start-date", day, "--end-date", day,
                   "--datasets", "moneyflow", "--max-days", "1",
                   "--budget-seconds", "240", "--request-timeout", "30",
                   "--retries", "1", "--retry-passes", "0", "--report", str(report)]
            for attempt in range(1, 4):
                completed = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
                log.write(f"{day} attempt={attempt} rc={completed.returncode} {completed.stdout[-500:]}\n")
                log.flush()
                if completed.returncode == 0:
                    break
                time.sleep(65)
            # The client already spaces every request at the account-safe
            # interval.  Do not add another full-minute delay after a
            # successful day; only failed attempts above wait for the quota
            # window to recover.
        log.write("END\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
