"""Retired presentation cannot open a DB or overwrite historical output."""
import subprocess
import sys
from pathlib import Path
import pytest
from trade_system.terminal_report import build_terminal_context, render_terminal_html, write_terminal, RetiredTerminalError


@pytest.mark.parametrize('entry', [build_terminal_context, render_terminal_html, write_terminal])
def test_old_terminal_refuses_before_side_effects(tmp_path,entry):
    absent=tmp_path/'never-create.duckdb'
    with pytest.raises(RetiredTerminalError,match='retired'):
        entry(str(absent))
    assert not list(tmp_path.iterdir())


def test_old_cli_preserves_existing_artifact_and_does_not_open_missing_database(tmp_path):
    output=tmp_path/'history.html';output.write_text('retained historical snapshot')
    missing=tmp_path/'missing.duckdb'
    command=Path(__file__).resolve().parents[1]/'scripts/generate_trading_terminal.py'
    result=subprocess.run([sys.executable,str(command),'--db',str(missing),'--out',str(output)],capture_output=True)
    assert result.returncode!=0 and b'retired' in result.stderr
    assert output.read_text()=='retained historical snapshot' and not missing.exists()
