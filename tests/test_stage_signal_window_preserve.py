from __future__ import annotations

import duckdb

from trade_system.stage_signals import ensure_stage_signal_schema, generate_stage_signals


def test_intraday_signal_preserves_morning_snapshot_during_lunch(tmp_path):
    db = tmp_path / "preserve-lunch.duckdb"
    con = duckdb.connect(str(db))
    ensure_stage_signal_schema(con)
    con.execute(
        "INSERT INTO stock_candidate_stage_signal "
        "(trade_date,stage,stock_code,stock_name,score,decision,evidence_json,run_id,is_actionable) "
        "VALUES ('2026-07-16','intraday_strength','000001','Ping An',72,'follow','{}','morning',true)"
    )
    con.close()

    result = generate_stage_signals(
        db, "2026-07-16", "intraday_strength", as_of_time="2026-07-16T11:45:00", run_id="lunch"
    )

    assert result["within_stage_window"] is False
    assert result["preserved"] is True
    assert result["actionable"] == 1
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT is_actionable FROM stock_candidate_stage_signal").fetchone()[0] is True
    con.close()
