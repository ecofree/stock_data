import duckdb

from trade_system.normalize import build_normalized_views
from trade_system.risk import init_trading_tables
from trade_system.signals import generate_signals


def make_signal_db(path):
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, "
        "rise_count INTEGER, fall_count INTEGER, consecutive_count INTEGER, "
        "raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES "
        "('2026-07-06', 85, 3, 3600, 900, 8, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_strength("
        "date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, "
        "fengban_rate DOUBLE, dieting INTEGER, up_count INTEGER, down_count INTEGER, "
        "raw_json VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_strength VALUES "
        "('2026-07-06', '801001', 88, 12, 76, 0, 40, 5, '{}', '2026-07-06 15:00:00')"
    )
    con.execute(
        "CREATE TABLE sector_ranking("
        "date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER, "
        "fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO sector_ranking VALUES "
        "('2026-07-06', '801001', 'test sector', 20, '2026-07-06 15:00:00', '{}')"
    )
    con.close()


def test_generate_signals_creates_market_regime_and_sector_scores(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    make_signal_db(db_path)
    build_normalized_views(str(db_path))

    result = generate_signals(str(db_path), "2026-07-06")

    con = duckdb.connect(str(db_path))
    regime = con.execute(
        "SELECT regime, suggested_position_pct FROM market_regime_snapshot"
    ).fetchone()
    sector = con.execute("SELECT sector_code, score FROM sector_rotation_score").fetchone()
    con.close()
    assert result["trade_date"] == "2026-07-06"
    assert regime == ("高潮", 30)
    assert sector[0] == "801001"
    assert sector[1] > 0


def test_generate_signals_repeated_replace_keeps_one_consistent_snapshot(tmp_path):
    db_path = tmp_path / "repeated.duckdb"
    make_signal_db(db_path)
    build_normalized_views(str(db_path))

    for _ in range(20):
        generate_signals(str(db_path), "2026-07-06")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {
            table: con.execute(
                f"SELECT count(*) FROM {table} WHERE trade_date='2026-07-06'"
            ).fetchone()[0]
            for table in (
                "market_regime_snapshot",
                "sector_rotation_score",
                "stock_candidate_score",
                "alert_events",
            )
        }
        indexes = {
            row[0]
            for row in con.execute("SELECT index_name FROM duckdb_indexes() WHERE index_name LIKE 'uq_%'").fetchall()
        }
    finally:
        con.close()

    assert counts["market_regime_snapshot"] == 1
    assert counts["sector_rotation_score"] == 1
    assert counts["stock_candidate_score"] == 0
    assert "uq_market_regime_date" in indexes
    assert "uq_sector_rotation_date_code" in indexes
    assert "uq_candidate_date_code" in indexes


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
