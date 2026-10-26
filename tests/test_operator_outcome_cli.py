import hashlib
import subprocess
import sys
from pathlib import Path

import duckdb

from trade_system.operator_outcomes import init_trading_tables


ROOT = Path(__file__).resolve().parents[1]


def test_import_operator_trade_outcomes_cli(tmp_path):
    db_path = tmp_path / "operator_cli.duckdb"
    init_trading_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO trade_plan (
                trade_date, stock_code, stock_name, setup_type, entry_condition, stop_condition,
                target_condition, max_position_pct, status
            )
            VALUES ('2026-07-06', '000002', 'Test Stock', 'manual_shortline_plan',
                    'confirm', 'stop', 'target', 3.0, 'planned')
            """
        )
    finally:
        con.close()

    csv_path = tmp_path / "outcomes.csv"
    csv_path.write_text(
        "\n".join(
            [
                "trade_date,stock_code,stock_name,execution_status,entry_price,exit_price,position_pct,outcome_tag,mistake_tag,review_note",
                "2026-07-06,000002,Test Stock,skipped,,,0,avoided_weak_market,discipline_ok,no auction confirmation",
            ]
        ),
        encoding="utf-8",
    )

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = subprocess.run(
        [sys.executable, "scripts/import_operator_trade_outcomes.py", "--db", str(db_path), "--csv", str(csv_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2, result.stderr
    assert 'RETIRED' in result.stderr
    assert hashlib.sha256(db_path.read_bytes()).hexdigest()==before
    with duckdb.connect(str(db_path),read_only=True) as con:
        assert con.execute('SELECT count(*) FROM operator_trade_outcome').fetchone()[0]==0
        assert con.execute('SELECT status FROM trade_plan').fetchone()[0]=='planned'
