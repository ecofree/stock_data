from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import API_BASE, API_KEY, DB_PATH, TODAY
from trade_system.api_data_audit import audit_requirements, render_api_data_audit_report


def _first_value(db_path: str | Path, queries: list[tuple[str, list]]) -> str:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for sql, params in queries:
            try:
                row = con.execute(sql, params).fetchone()
            except Exception:
                continue
            if row and row[0]:
                return str(row[0])
    finally:
        con.close()
    return ""


def infer_stock_code(db_path: str | Path, date: str) -> str:
    return _first_value(
        db_path,
        [
            (
                """
                SELECT stock_code
                FROM stock_candidate_stage_signal
                WHERE CAST(trade_date AS VARCHAR) = ? AND stock_code != ''
                ORDER BY score DESC NULLS LAST, stock_code
                LIMIT 1
                """,
                [date],
            ),
            (
                """
                SELECT stock_code
                FROM kline
                WHERE stock_code != ''
                GROUP BY stock_code
                ORDER BY count(*) DESC, stock_code
                LIMIT 1
                """,
                [],
            ),
            (
                """
                SELECT stock_code
                FROM l2_stock_intraday
                WHERE CAST(date AS VARCHAR) = ? AND stock_code != ''
                GROUP BY stock_code
                ORDER BY count(*) DESC, stock_code
                LIMIT 1
                """,
                [date],
            ),
        ],
    ) or "000938"


def infer_sector_code(db_path: str | Path, date: str) -> str:
    return _first_value(
        db_path,
        [
            (
                """
                SELECT sector_code
                FROM sector_ranking
                WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' AND sector_code != '0'
                ORDER BY sector_code
                LIMIT 1
                """,
                [date],
            ),
            (
                """
                SELECT sector_code
                FROM sector_capital
                WHERE CAST(date AS VARCHAR) = ? AND sector_code != '' AND sector_code != '0'
                ORDER BY sector_code
                LIMIT 1
                """,
                [date],
            ),
            (
                """
                SELECT sector_code
                FROM sector_plates
                WHERE sector_code != '' AND sector_code != '0'
                ORDER BY sector_code
                LIMIT 1
                """,
                [],
            ),
        ],
    ) or "801001"


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Audit which professional data sources are obtainable from the configured API.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--stock-code", default="")
    parser.add_argument("--sector-code", default="")
    parser.add_argument("--out", default=str(project_root / "reports" / "api_data_source_audit_latest.md"))
    parser.add_argument("--docs-out", default=str(project_root / "docs" / "integration" / "api_data_gap_matrix.md"))
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    stock_code = args.stock_code or infer_stock_code(db_path, args.date)
    sector_code = args.sector_code or infer_sector_code(db_path, args.date)
    live_probe = not args.no_live

    summaries = audit_requirements(
        db_path=db_path,
        api_base=API_BASE,
        api_key=API_KEY,
        date=args.date,
        stock_code=stock_code,
        sector_code=sector_code,
        live_probe=live_probe,
        timeout=args.timeout,
    )
    report = render_api_data_audit_report(
        summaries,
        date=args.date,
        stock_code=stock_code,
        sector_code=sector_code,
        live_probe=live_probe,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    docs_out = Path(args.docs_out)
    docs_out.parent.mkdir(parents=True, exist_ok=True)
    docs_out.write_text(report, encoding="utf-8")

    print(f"api_data_source_audit={out}")
    print(f"api_data_gap_matrix={docs_out}")
    print(f"date={args.date} stock_code={stock_code} sector_code={sector_code} live_probe={str(live_probe).lower()}")
    for verdict in sorted({item.verdict for item in summaries}):
        count = sum(1 for item in summaries if item.verdict == verdict)
        print(f"{verdict}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
