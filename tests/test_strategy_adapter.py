import duckdb

from trade_system.strategy.definition import StrategyDefinition, load_default_stage_strategies
from trade_system.strategy.engine import run_strategy_scan
from trade_system.strategy.schema import (
    install_strategy_tables,
    persist_strategy_backtest_summary,
    persist_strategy_definitions,
    persist_strategy_scan_results,
)
from trade_system.strategy_stage_backtest import run_strategy_result_backtest


def test_default_stage_strategies_cover_four_operator_stages():
    strategies = load_default_stage_strategies()

    stages = {strategy.stage for strategy in strategies}

    assert {"pre_market", "auction_confirm", "intraday_strength", "closing_decision"} <= stages
    assert all(strategy.entry_rules for strategy in strategies)
    assert all(strategy.invalid_conditions for strategy in strategies)


def test_strategy_scan_returns_explainable_candidates():
    strategy = StrategyDefinition(
        strategy_id="test.pre_market",
        name="盘前强势池",
        stage="pre_market",
        min_score=70,
        entry_rules=["score >= 70", "sector_strength >= 60"],
        risk_rules=["弱市场降级"],
        invalid_conditions=["跌破竞价确认线"],
    )
    candidates = [
        {
            "trade_date": "2026-07-07",
            "stage": "pre_market",
            "stock_code": "000001",
            "stock_name": "测试股份",
            "score": 80,
            "sector_strength": 66,
        },
        {
            "trade_date": "2026-07-07",
            "stage": "pre_market",
            "stock_code": "000002",
            "stock_name": "弱势股份",
            "score": 50,
            "sector_strength": 66,
        },
    ]

    results = run_strategy_scan(candidates, [strategy])

    assert len(results) == 1
    assert results[0]["strategy_id"] == "test.pre_market"
    assert results[0]["selected_reason"]
    assert "score" in results[0]["evidence_json"]
    assert "弱市场降级" in results[0]["risk_points"]
    assert "跌破竞价确认线" in results[0]["invalid_conditions"]


def test_strategy_tables_persist_scan_results_idempotently(tmp_path):
    db_path = tmp_path / "strategy.duckdb"
    install_strategy_tables(db_path)
    assert persist_strategy_definitions(db_path, load_default_stage_strategies()) >= 4
    results = [
        {
            "trade_date": "2026-07-07",
            "strategy_id": "test.pre_market",
            "symbol": "000001",
            "stock_name": "测试股份",
            "stage": "pre_market",
            "score": 80,
            "evidence_json": "{}",
            "selected_reason": "score >= 70",
            "risk_points": "弱市场降级",
            "invalid_conditions": "跌破竞价确认线",
        }
    ]

    assert persist_strategy_scan_results(db_path, results) == 1
    assert persist_strategy_scan_results(db_path, results) == 1

    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM strategy_definition").fetchone()[0] >= 4
        assert con.execute("SELECT count(*) FROM strategy_scan_result").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM v_stage_strategy_candidates").fetchone()[0] == 1
    finally:
        con.close()


def test_persist_strategy_backtest_summary_writes_result_rows(tmp_path):
    db_path = tmp_path / "strategy_summary.duckdb"
    install_strategy_tables(db_path)

    count = persist_strategy_backtest_summary(
        db_path,
        [
            {
                "strategy_id": "test.pre_market",
                "stage": "pre_market",
                "sample_start": "2026-07-06",
                "sample_end": "2026-07-07",
                "sample_count": 1,
                "win_rate": 100.0,
                "avg_return": 10.0,
                "max_drawdown": 0.0,
                "profit_factor": None,
                "config_hash": "abc",
            }
        ],
    )

    con = duckdb.connect(str(db_path))
    try:
        assert count == 1
        assert con.execute("SELECT count(*) FROM strategy_backtest_result").fetchone()[0] == 1
    finally:
        con.close()


def test_strategy_result_backtest_uses_next_available_close(tmp_path):
    db_path = tmp_path / "backtest.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE strategy_scan_result (
                trade_date VARCHAR, strategy_id VARCHAR, symbol VARCHAR, stock_name VARCHAR,
                stage VARCHAR, score DOUBLE, evidence_json VARCHAR, selected_reason VARCHAR,
                risk_points VARCHAR, invalid_conditions VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO strategy_scan_result VALUES ('2026-07-06','test.pre_market','000001','测试股份','pre_market',80,'{}','reason','','')"
        )
        con.execute(
            "CREATE TABLE stock_candidate_stage_signal("
            "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
            "score DOUBLE, decision VARCHAR)"
        )
        con.execute(
            "INSERT INTO stock_candidate_stage_signal VALUES "
            "('2026-07-06','premarket_pool','000001','测试股份',80,'pool')"
        )
        con.execute(
            "CREATE TABLE kline(date DATE, stock_code VARCHAR, open DOUBLE, close DOUBLE, ktype VARCHAR)"
        )
        con.execute("INSERT INTO kline VALUES ('2026-07-06','000001',10.0,11.0,'D')")
        con.execute("INSERT INTO kline VALUES ('2026-07-07','000001',11.0,11.0,'D')")
    finally:
        con.close()

    result = run_strategy_result_backtest(db_path, fee_rate=0, slippage_bps=0)

    assert result["sample_count"] == 1
    assert result["summary"]["win_rate_pct"] == 100.0
    assert result["rows"][0]["net_return_pct"] == 10.0
