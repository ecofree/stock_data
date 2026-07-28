"""Resumable, rate-limited financial statement collector.

The old full collector was missing, which left all ``finance_*`` tables empty.
This module deliberately works in small batches: financial statements change
quarterly, so a daily run must not fan out to every listed company.  Each
attempt is checkpointed and failures are retained for the next retry window.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from base import DuckDBStore, logger
from schema import init_schema
from trade_system.host_limiter import shared_host_limiter
from trade_system.eastmoney_finance import get_income_statement
from trade_system.stock_data_sources import get_financial_statements


ROOT = Path(__file__).resolve().parent


def _pure_code(value: object) -> str:
    text = str(value or "").strip().upper().replace(".", "")
    if text.startswith(("SH", "SZ", "BJ")):
        text = text[2:]
    return text if len(text) == 6 and text.isdigit() else ""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _number(row: dict, *keys: str):
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "-"):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _report_date(row: dict) -> str:
    value = row.get("REPORT_DATE") or row.get("report_date") or row.get("报告期")
    if not value:
        return ""
    text = str(value)[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _ensure_checkpoint(store: DuckDBStore) -> None:
    store.execute(
        """
        CREATE TABLE IF NOT EXISTS finance_fetch_checkpoint (
            stock_code VARCHAR,
            target_date DATE,
            status VARCHAR,
            source_income VARCHAR,
            source_balance VARCHAR,
            source_cashflow VARCHAR,
            income_rows INTEGER,
            balance_rows INTEGER,
            cashflow_rows INTEGER,
            last_error VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )


def stock_universe(store: DuckDBStore, limit: int = 20, codes: str = "") -> list[str]:
    explicit = [_pure_code(item) for item in codes.split(",") if item.strip()] if codes else []
    explicit = [item for item in explicit if item]
    if explicit:
        return list(dict.fromkeys(explicit))[:limit] if limit > 0 else list(dict.fromkeys(explicit))
    candidates: list[str] = []
    queries = (
        "SELECT DISTINCT stock_code FROM multi_source_stock_flow ORDER BY stock_code",
        "SELECT DISTINCT ts_code FROM tushare_daily_basic ORDER BY ts_code",
        "SELECT DISTINCT stock_code FROM sector_stocks ORDER BY stock_code",
    )
    for query in queries:
        try:
            candidates.extend(_pure_code(row[0]) for row in store.fetchall(query))
        except Exception:
            continue
        if len(set(candidates)) >= max(limit, 1):
            break
    unique = list(dict.fromkeys(code for code in candidates if code))
    return unique[:limit] if limit > 0 else unique


def _checkpoint_recent(store: DuckDBStore, code: str, refresh_days: int) -> bool:
    try:
        rows = store.fetchall(
            "SELECT status, fetched_at FROM finance_fetch_checkpoint "
            "WHERE stock_code = ? ORDER BY fetched_at DESC LIMIT 1",
            [code],
        )
        if not rows or rows[0][0] != "ok" or rows[0][1] is None:
            return False
        fetched = rows[0][1]
        if hasattr(fetched, "date"):
            return (datetime.now() - fetched).days < refresh_days
    except Exception:
        return False
    return False


def _fetch(code: str, periods: int) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Fetch income (Eastmoney) and balance/cashflow (Sina) serially."""
    shared_host_limiter.acquire("eastmoney", 0.8)
    income = get_income_statement(code, periods=periods) or []
    shared_host_limiter.acquire("sina", 0.8)
    balance = get_financial_statements(code, "fzb", periods) or []
    shared_host_limiter.acquire("sina", 0.8)
    cashflow = get_financial_statements(code, "llb", periods) or []
    return income, balance, cashflow, {
        "income": "eastmoney" if income else "missing",
        "balance": "sina" if balance else "missing",
        "cashflow": "sina" if cashflow else "missing",
    }


def collect_finance(
    store: DuckDBStore,
    target_date: str,
    *,
    codes: str = "",
    max_stocks: int = 20,
    periods: int = 8,
    refresh_days: int = 7,
    force: bool = False,
) -> dict:
    _ensure_checkpoint(store)
    selected = stock_universe(store, max_stocks, codes)
    result = {"selected": len(selected), "ok": 0, "missing": 0, "failed": 0, "skipped": 0}
    for code in selected:
        if not force and _checkpoint_recent(store, code, refresh_days):
            result["skipped"] += 1
            continue
        income = balance = cashflow = []
        sources = {"income": "missing", "balance": "missing", "cashflow": "missing"}
        error = ""
        try:
            income, balance, cashflow, sources = _fetch(code, max(1, min(periods, 12)))
            for report in income:
                rd = _report_date(report)
                if rd:
                    store.insert_rows(
                        "finance_income", [(code, rd, _json(report))],
                        ["stock_code", "report_date", "raw_json"],
                        replace_on=["stock_code", "report_date"],
                    )
            for report, table in ((balance, "finance_balance"), (cashflow, "finance_cashflow")):
                for item in report:
                    rd = _report_date(item)
                    if rd:
                        store.insert_rows(
                            table, [(code, rd, _json(item))],
                            ["stock_code", "report_date", "raw_json"],
                            replace_on=["stock_code", "report_date"],
                        )
            if income:
                latest = income[0]
                rd = _report_date(latest)
                if rd:
                    revenue = _number(latest, "TOTAL_OPERATE_INCOME", "OPERATE_INCOME")
                    net_profit = _number(latest, "PARENT_NETPROFIT", "NETPROFIT")
                    eps = _number(latest, "BASIC_EPS", "DILUTED_EPS")
                    roe = _number(latest, "WEIGHTAVG_ROE", "ROE")
                    gross_margin = _number(latest, "GROSS_PROFIT_MARGIN")
                    net_margin = _number(latest, "NET_PROFIT_MARGIN")
                    debt_ratio = _number(latest, "ASSET_LIABILITY_RATIO", "DEBT_ASSET_RATIO")
                    store.insert_rows(
                        "finance_summary",
                        [(code, rd, revenue, net_profit, eps, roe, gross_margin,
                          net_margin, debt_ratio, _json(latest))],
                        ["stock_code", "report_date", "revenue", "net_profit", "eps",
                         "roe", "gross_margin", "net_margin", "debt_ratio", "raw_json"],
                        replace_on=["stock_code", "report_date"],
                    )
            if income or balance or cashflow:
                result["ok"] += 1
            else:
                result["missing"] += 1
        except Exception as exc:  # retain a durable failure record and continue
            error = f"{type(exc).__name__}: {exc}"[:1000]
            result["failed"] += 1
            logger.warning("finance %s failed: %s", code, error)
        status = "ok" if (income or balance or cashflow) and not error else ("failed" if error else "missing")
        store.insert_rows(
            "finance_fetch_checkpoint",
            [(code, target_date, status, sources["income"], sources["balance"],
              sources["cashflow"], len(income), len(balance), len(cashflow), error)],
            ["stock_code", "target_date", "status", "source_income", "source_balance",
             "source_cashflow", "income_rows", "balance_rows", "cashflow_rows", "last_error"],
            replace_on=["stock_code", "target_date"],
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumable financial statement gap-fill")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--codes", default="", help="comma-separated codes; otherwise use persisted universe")
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--periods", type=int, default=8)
    parser.add_argument("--refresh-days", type=int, default=7)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
        result = collect_finance(
            store, args.date, codes=args.codes, max_stocks=args.max_stocks,
            periods=args.periods, refresh_days=args.refresh_days, force=args.force,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["failed"] == 0 else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
