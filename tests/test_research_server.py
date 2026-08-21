"""Tests for the research console SQL guard and query execution."""
from __future__ import annotations

import duckdb
import pytest

from scripts.research_server import QueryRejected, guard_sql, run_query


@pytest.fixture()
def con():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    conn.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'z')")
    yield conn
    conn.close()


def test_guard_appends_limit():
    assert guard_sql("SELECT * FROM t").endswith(f"LIMIT 500")


def test_guard_keeps_explicit_limit():
    out = guard_sql("SELECT * FROM t LIMIT 1;")
    assert "LIMIT 1" in out and not out.endswith(";")


def test_guard_rejects_writes_and_ddl():
    for bad in (
        "DELETE FROM t", "DROP TABLE t", "INSERT INTO t VALUES (1)",
        "CREATE TABLE x(a INT)", "ATTACH 'x' AS y", "COPY t TO 'x'",
        "CALL some_procedure()", "PRAGMA metadata_info",
        "SELECT 1; DROP TABLE t",
        "-- comment\nDELETE FROM t",
    ):
        with pytest.raises(QueryRejected):
            guard_sql(bad)


def test_guard_allows_cte_and_strips_comments():
    sql = "/* hi */ WITH x AS (SELECT 1 AS v) SELECT v FROM x -- tail"
    assert guard_sql(sql).upper().startswith("WITH")


def test_run_query_roundtrip(con):
    result = run_query(con, "SELECT a, b FROM t ORDER BY a")
    assert result["columns"] == ["a", "b"]
    assert result["rows"][0] == [1, "x"]
    assert len(result["rows"]) == 3
    assert result["truncated"] is False
