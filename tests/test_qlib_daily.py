"""Missing frozen artifacts never select a model from the historical SQL registry."""
import duckdb
import pytest

from trade_system.v2.domain import file_hash
from trade_system.v2.research_product import read_build


def test_research_without_frozen_build_preserves_registry_and_manual_facts(tmp_path):
    db = tmp_path / "historical.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE qlib_model_registry(model_id VARCHAR,status VARCHAR)")
        con.execute("INSERT INTO qlib_model_registry VALUES ('old','champion')")
        con.execute("CREATE TABLE watchlist(note VARCHAR); INSERT INTO watchlist VALUES ('human')")
    before = file_hash(db)
    with pytest.raises(FileNotFoundError):
        read_build(tmp_path)
    assert file_hash(db) == before
    assert {p.name for p in tmp_path.iterdir()} == {"historical.duckdb"}
