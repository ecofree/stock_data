"""Resumable pre-TuShare historical backfill using BaoStock.

BaoStock is used only for dates before the canonical TuShare history starts.
Rows are normalized to the project's TuShare-shaped tables (volume/amount in
the same 10,000-unit convention) and never overwrite an existing row.  A
separate checkpoint records the actual provider so a BaoStock row is not
mistaken for a successful TuShare response.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore
from schema import init_schema


FIELDS = "date,open,high,low,close,volume,amount,turn,pctChg,peTTM,pbMRQ"


def _market(code: str) -> str:
    value = "".join(ch for ch in str(code or "") if ch.isdigit()).zfill(6)[-6:]
    if value.startswith(("6", "9")):
        return f"sh.{value}"
    if value.startswith(("4", "8")):
        return f"bj.{value}"
    return f"sz.{value}"


def _num(value):
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _date(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        raise ValueError(f"invalid date: {value}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _ensure_checkpoint(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS baostock_history_checkpoint (
            source VARCHAR NOT NULL,
            dataset VARCHAR NOT NULL,
            stock_code VARCHAR NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            status VARCHAR,
            rows_written INTEGER DEFAULT 0,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (source, dataset, stock_code, start_date, end_date)
        )
        """
    )


def backfill(
    db_path: str | Path,
    *,
    start_date: str,
    end_date: str,
    max_stocks: int = 20,
    offset: int = 0,
    force: bool = False,
    timeout_seconds: float = 300.0,
) -> dict[str, object]:
    start = _date(start_date)
    end = _date(end_date)
    if end < start:
        raise ValueError("end_date must not precede start_date")
    store = DuckDBStore(str(db_path))
    init_schema(store.conn)
    _ensure_checkpoint(store.conn)
    started = time.monotonic()
    try:
        codes = [str(row[0]) for row in store.conn.execute(
            "SELECT DISTINCT stock_code FROM tushare_stock_basic "
            "WHERE stock_code IS NOT NULL ORDER BY stock_code"
        ).fetchall()]
        selected = codes[max(0, int(offset)):]
        if max_stocks > 0:
            selected = selected[: int(max_stocks)]
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError("baostock is not installed in D:\\anaconda") from exc
        login = bs.login()
        if getattr(login, "error_code", "1") != "0":
            raise RuntimeError(f"baostock login failed: {getattr(login, 'error_msg', '')}")
        results = []
        try:
            for code in selected:
                if time.monotonic() - started >= timeout_seconds:
                    break
                exists = store.conn.execute(
                    "SELECT status FROM baostock_history_checkpoint WHERE source='baostock' AND dataset='daily' AND stock_code=? AND start_date=? AND end_date=?",
                    [code, start, end],
                ).fetchone()
                if exists and exists[0] in ("success", "empty") and not force:
                    continue
                try:
                    rs = bs.query_history_k_data_plus(
                        _market(code), FIELDS, start_date=start, end_date=end,
                        frequency="d", adjustflag="3",
                    )
                    if getattr(rs, "error_code", "1") != "0":
                        raise RuntimeError(getattr(rs, "error_msg", "query failed"))
                    rows = []
                    while rs.next():
                        values = rs.get_row_data()
                        if values and values[0]:
                            rows.append(values)
                    if not rows:
                        raise RuntimeError("empty BaoStock history response")
                    daily_rows = []
                    basic_rows = []
                    ts_code = _market(code).replace(".", ".").upper()
                    for row in rows:
                        day = row[0]
                        daily_rows.append((
                            ts_code, code, day, _num(row[1]), _num(row[2]), _num(row[3]),
                            _num(row[4]), (_num(row[5]) or 0.0) / 10000.0,
                            (_num(row[6]) or 0.0) / 10000.0, _num(row[8]),
                        ))
                        basic_rows.append((
                            ts_code, code, day, _num(row[7]), None, _num(row[9]),
                            _num(row[10]), None, None,
                        ))
                    store.conn.executemany(
                        "INSERT INTO tushare_daily (ts_code,stock_code,date,open,high,low,close,volume,turnover,change_pct) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (date,ts_code) DO NOTHING",
                        daily_rows,
                    )
                    store.conn.executemany(
                        "INSERT INTO tushare_daily_basic (ts_code,stock_code,date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (date,ts_code) DO NOTHING",
                        basic_rows,
                    )
                    store.conn.execute(
                        "INSERT INTO baostock_history_checkpoint VALUES ('baostock','daily',?,?,?,?,?,?,current_timestamp) ON CONFLICT (source,dataset,stock_code,start_date,end_date) DO UPDATE SET status=excluded.status,rows_written=excluded.rows_written,last_error=excluded.last_error,updated_at=excluded.updated_at",
                        [code, start, end, "success", len(daily_rows), ""],
                    )
                    store.conn.execute(
                        "INSERT INTO baostock_history_checkpoint VALUES ('baostock','daily_basic',?,?,?,?,?,?,current_timestamp) ON CONFLICT (source,dataset,stock_code,start_date,end_date) DO UPDATE SET status=excluded.status,rows_written=excluded.rows_written,last_error=excluded.last_error,updated_at=excluded.updated_at",
                        [code, start, end, "success", len(basic_rows), "valuation fields limited to pe/pb/turnover; market caps unavailable"],
                    )
                    store.conn.commit()
                    results.append({"stock_code": code, "status": "success", "rows": len(daily_rows)})
                except Exception as exc:
                    try:
                        store.conn.rollback()
                    except Exception:
                        pass
                    message = str(exc)[:500]
                    terminal_empty = message == "empty BaoStock history response"
                    terminal_status = "empty" if terminal_empty else "error"
                    for dataset in ("daily", "daily_basic"):
                        store.conn.execute(
                            "INSERT INTO baostock_history_checkpoint VALUES ('baostock',?,?,?,?,?,?,?,current_timestamp) ON CONFLICT (source,dataset,stock_code,start_date,end_date) DO UPDATE SET status=excluded.status,last_error=excluded.last_error,updated_at=excluded.updated_at",
                            [dataset, code, start, end, terminal_status, 0, message],
                        )
                    store.conn.commit()
                    results.append({"stock_code": code, "status": terminal_status, "error": message})
        finally:
            try:
                bs.logout()
            except Exception:
                pass
        return {
            "source": "baostock",
            "start_date": start,
            "end_date": end,
            "requested": len(selected),
            "success": sum(row["status"] == "success" for row in results),
            "errors": sum(row["status"] == "error" for row in results),
            "results": results,
        }
    finally:
        store.close()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Resumable BaoStock pre-TuShare history backfill")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--start-date", default="20240101")
    parser.add_argument("--end-date", default="20241231")
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    result = backfill(args.db, start_date=args.start_date, end_date=args.end_date,
                      max_stocks=args.max_stocks, offset=args.offset,
                      force=args.force, timeout_seconds=args.timeout_seconds)
    print(result)
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
