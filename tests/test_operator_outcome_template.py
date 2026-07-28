from __future__ import annotations

import csv

import duckdb

from scripts.create_operator_outcome_template import create_template
from trade_system.operator_outcomes import OUTCOME_COLUMNS


def test_create_operator_outcome_template_uses_plans(tmp_path):
    db = tmp_path / "operator.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE trade_plan (trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "setup_type VARCHAR, entry_condition VARCHAR, stop_condition VARCHAR, target_condition VARCHAR, "
        "max_position_pct DOUBLE, status VARCHAR, created_at TIMESTAMP)"
    )
    con.execute("INSERT INTO trade_plan VALUES ('2026-07-14','000001','平安银行','setup','','','',10,'planned',current_timestamp)")
    con.close()

    out = tmp_path / "outcomes.csv"
    result = create_template(db, out, "2026-07-14")
    assert result["rows"] == 1
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["stock_code"] == "000001"
    assert rows[0]["execution_status"] == "review_required"
    assert set(rows[0]) == set(OUTCOME_COLUMNS)


def test_create_operator_outcome_template_uses_actionable_stage_signals(tmp_path):
    db = tmp_path / "stage-outcomes.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE stock_candidate_stage_signal ("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "score DOUBLE, is_actionable BOOLEAN)"
    )
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-16','intraday_strength','000001','Ping An',71,true),"
        "('2026-07-16','intraday_strength','000002','Vanke',60,false)"
    )
    con.close()

    out = tmp_path / "stage-outcomes.csv"
    result = create_template(db, out)

    assert result["trade_date"] == "2026-07-16"
    assert result["rows"] == 1
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["stock_code"] for row in rows] == ["000001"]
