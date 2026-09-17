import duckdb

from trade_system.risk import init_trading_tables


def test_init_trading_tables_creates_operator_tables(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    init_trading_tables(str(db_path))

    con = duckdb.connect(str(db_path))
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    con.close()
    assert {
        "watchlist",
        "trade_plan",
        "portfolio_snapshot",
        "trade_journal",
        "operator_trade_outcome",
        "risk_snapshot",
        "v_operator_plan_outcome",
    } <= tables
