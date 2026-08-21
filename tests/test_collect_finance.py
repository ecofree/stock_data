from collect_finance import collect_finance
from base import DuckDBStore
from schema import init_schema


def test_finance_collector_persists_rows_and_checkpoint(tmp_path, monkeypatch):
    store = DuckDBStore(str(tmp_path / "finance.duckdb"))
    init_schema(store.conn)
    # Patch the implementation module (collectors.collect_finance); the root
    # collect_finance.py is only a compatibility shim.
    monkeypatch.setattr(
        "collectors.collect_finance._fetch",
        lambda code, periods: (
            [{"REPORT_DATE": "2026-03-31", "TOTAL_OPERATE_INCOME": 1000,
              "PARENT_NETPROFIT": 100, "BASIC_EPS": 0.2}],
            [{"报告期": "2025-12-31", "资产总计": 2000}],
            [{"报告期": "2025-12-31", "经营活动现金流": 300}],
            {"income": "eastmoney", "balance": "sina", "cashflow": "sina"},
        ),
    )
    result = collect_finance(store, "2026-07-14", codes="000001", max_stocks=1)
    assert result["ok"] == 1
    assert store.fetchall("SELECT count(*) FROM finance_income")[0][0] == 1
    assert store.fetchall("SELECT count(*) FROM finance_balance")[0][0] == 1
    assert store.fetchall("SELECT status FROM finance_fetch_checkpoint")[0][0] == "ok"
    store.close()
