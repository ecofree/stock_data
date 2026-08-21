
import duckdb

from trade_system.operator_backtest import run_operator_stage_backtest
from trade_system.operator_outcomes import import_operator_trade_outcomes
from trade_system.risk import init_trading_tables


def test_import_operator_trade_outcomes_links_plan_journal_and_backtest(tmp_path):
    db_path = tmp_path / "operator.duckdb"
    init_trading_tables(db_path)

    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO trade_plan (
                trade_date, stock_code, stock_name, setup_type, entry_condition, stop_condition,
                target_condition, max_position_pct, status
            )
            VALUES ('2026-07-06', '000001', 'Ping An Bank', 'manual_shortline_plan',
                    'auction confirms', 'breaks stop', 'review at close', 5.0, 'planned')
            """
        )
        con.execute(
            """
            CREATE TABLE stock_candidate_stage_signal(
                trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE
            )
            """
        )
        con.execute(
            "INSERT INTO stock_candidate_stage_signal VALUES ('2026-07-06','premarket_pool','000001','Ping An Bank',72.0)"
        )
        con.execute("CREATE TABLE kline(date DATE, stock_code VARCHAR, close DOUBLE, ktype VARCHAR)")
    finally:
        con.close()

    csv_path = tmp_path / "outcomes.csv"
    csv_path.write_text(
        "\n".join(
            [
                "trade_date,stock_code,stock_name,execution_status,entry_time,exit_time,entry_price,exit_price,position_pct,outcome_tag,mistake_tag,review_note",
                "2026-07-06,000001,Ping An Bank,executed,09:35,14:50,10.00,10.80,5.0,followed_plan,none,auction confirmed then held to close",
            ]
        ),
        encoding="utf-8",
    )

    result = import_operator_trade_outcomes(db_path, csv_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        outcome = con.execute(
            """
            SELECT execution_status, gross_return_pct, net_return_pct, outcome_tag, mistake_tag
            FROM operator_trade_outcome
            WHERE trade_date = '2026-07-06' AND stock_code = '000001'
            """
        ).fetchone()
        plan_status = con.execute(
            "SELECT status FROM trade_plan WHERE trade_date = '2026-07-06' AND stock_code = '000001'"
        ).fetchone()[0]
        journal = con.execute(
            """
            SELECT action, action_time, position_pct, reason, mistake_tag
            FROM trade_journal
            WHERE trade_date = '2026-07-06' AND stock_code = '000001' AND action = 'operator_outcome'
            """
        ).fetchone()
    finally:
        con.close()

    assert result == {"rows_imported": 1, "journal_rows": 1, "plans_updated": 1}
    assert outcome[0] == "executed"
    assert round(outcome[1], 2) == 8.0
    assert outcome[2] < outcome[1]
    assert outcome[3] == "followed_plan"
    assert outcome[4] == "none"
    assert plan_status == "executed"
    assert journal[0] == "operator_outcome"
    assert journal[1] == "14:50"
    assert journal[2] == 5.0
    assert "auction confirmed" in journal[3]

    backtest = run_operator_stage_backtest(db_path)
    assert backtest["sample_count"] == 1
    assert backtest["summary"]["real_outcome_count"] == 1
    assert backtest["rows"][0]["source"] == "real_outcome"
    assert backtest["rows"][0]["execution_status"] == "executed"


def test_operator_backtest_handles_empty_outcome_table_without_signal_table(tmp_path):
    db_path = tmp_path / "empty_outcome.duckdb"
    init_trading_tables(db_path)

    result = run_operator_stage_backtest(db_path)

    assert result["sample_count"] == 0
    assert result["rows"] == []


def test_unfilled_operator_outcome_is_not_counted_as_return_sample(tmp_path):
    db_path = tmp_path / "unfilled.duckdb"
    init_trading_tables(db_path)
    csv_path = tmp_path / "outcomes.csv"
    csv_path.write_text(
        "trade_date,stock_code,stock_name,execution_status,entry_price,exit_price,position_pct\n"
        "2026-07-06,000001,Ping An Bank,skipped,,,0\n",
        encoding="utf-8",
    )

    import_operator_trade_outcomes(db_path, csv_path)
    result = run_operator_stage_backtest(db_path)

    assert result["mode"] == "real_outcome_incomplete"
    assert result["sample_count"] == 0
    assert result["excluded_unfilled_count"] == 1
    assert result["readiness"]["ready"] is False
