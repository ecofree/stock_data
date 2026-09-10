import duckdb
import pytest
from tools.v2.backup_verify import backup_verify


def test_backup_restore_and_no_overwrite(tmp_path):
    source = tmp_path / 'source.duckdb'
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE TABLE portfolio_snapshot(position_pct DOUBLE)')
        con.execute('INSERT INTO portfolio_snapshot VALUES (60)')
    result = backup_verify(source, tmp_path / 'copy')
    assert result['restore_verified']
    assert result['protected_rows']['portfolio_snapshot'] == 1
    assert result['controls']['portfolio_position_sum'] == 60
    assert (tmp_path / 'copy' / 'portfolio_snapshot.parquet').exists()
    with pytest.raises(ValueError):
        backup_verify(source, tmp_path / 'copy')
