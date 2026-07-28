import duckdb

from trade_system.daily_review import build_daily_review_context, render_daily_review_markdown
from trade_system.operator_outcomes import ensure_operator_outcome_tables
from trade_system.risk import init_trading_tables


def test_daily_review_renders_operator_outcomes(tmp_path):
    db_path = tmp_path / "review.duckdb"
    init_trading_tables(db_path)
    ensure_operator_outcome_tables(db_path)

    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE market_regime_snapshot(trade_date VARCHAR, regime VARCHAR, regime_score DOUBLE, suggested_position_pct INTEGER, evidence_json VARCHAR, generated_at TIMESTAMP)"
        )
        con.execute("INSERT INTO market_regime_snapshot VALUES ('2026-07-06','retreat',15,5,'{}',current_timestamp)")
        con.execute(
            """
            INSERT INTO operator_trade_outcome (
                trade_date, stock_code, stock_name, execution_status, entry_time, exit_time,
                entry_price, exit_price, position_pct, gross_return_pct, net_return_pct,
                outcome_tag, mistake_tag, review_note, imported_from
            )
            VALUES ('2026-07-06','000001','Ping An Bank','skipped','09:31','15:00',
                    NULL,NULL,0,0,0,'avoided_weak_market','discipline_ok','risk gate blocked the trade','manual')
            """
        )
    finally:
        con.close()

    context = build_daily_review_context(db_path, "2026-07-06")
    markdown = render_daily_review_markdown(context)

    assert "## Outcome Review" in markdown
    assert "Ping An Bank" in markdown
    assert "avoided_weak_market" in markdown
    assert "discipline_ok" in markdown

