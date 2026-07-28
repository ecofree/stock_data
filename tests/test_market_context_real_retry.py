from __future__ import annotations

from base import DuckDBStore
from collect_daily import collect_daily
from collect_market import collect_market_rise_fall
from schema import init_schema


class DailyClient:
    def get(self, endpoint, params=None):
        assert endpoint == "/daily"
        return {
            "limit_up_count": 10,
            "limit_down_count": 2,
            "rise_count": 3000,
            "fall_count": 1800,
            "consecutive_count": 5,
        }


class RiseFallClient:
    def get(self, endpoint, params=None):
        assert endpoint == "/market/rise-fall"
        return {
            "raw_data": [[10, 2, 3, 7, 30.0, 2, "2026-07-27"]]
        }


class PreviousRiseFallClient:
    def get(self, endpoint, params=None):
        assert endpoint == "/market/rise-fall"
        return {
            "raw_data": [[14, 2, 3, 11, 21.4, 2, "2026-07-24"]]
        }


def test_real_daily_retry_replaces_same_date_fallback(tmp_path):
    store = DuckDBStore(tmp_path / "daily-retry.duckdb")
    try:
        init_schema(store.conn)
        store.conn.execute(
            "INSERT INTO daily_summary("
            "date,limit_up_count,source_kind) VALUES ('2026-07-27',1,'fallback')"
        )
        assert collect_daily(DailyClient(), store, "2026-07-27") == 1
        assert store.conn.execute(
            "SELECT limit_up_count,source_kind FROM daily_summary "
            "WHERE date='2026-07-27'"
        ).fetchone() == (10, "real")
    finally:
        store.close()


def test_real_rise_fall_retry_replaces_fallback_source_markers(tmp_path):
    store = DuckDBStore(tmp_path / "rise-fall-retry.duckdb")
    try:
        init_schema(store.conn)
        store.conn.execute(
            "INSERT INTO market_rise_fall("
            "date,limit_up_count,source_kind) VALUES ('2026-07-27',1,'fallback')"
        )
        store.conn.execute(
            "INSERT INTO daily_summary("
            "date,limit_up_count,source_kind) VALUES ('2026-07-27',1,'fallback')"
        )
        assert collect_market_rise_fall(
            RiseFallClient(), store, "2026-07-27"
        ) == 1
        assert store.conn.execute(
            "SELECT limit_up_count,source_kind FROM market_rise_fall "
            "WHERE date='2026-07-27'"
        ).fetchone() == (10, "real")
        assert store.conn.execute(
            "SELECT limit_up_count,source_kind FROM daily_summary "
            "WHERE date='2026-07-27'"
        ).fetchone() == (10, "real_cross_source")
    finally:
        store.close()


def test_previous_session_kpl_row_is_stored_at_source_date_without_relabelling(tmp_path):
    store = DuckDBStore(tmp_path / "previous-market.duckdb")
    try:
        init_schema(store.conn)
        store.conn.execute(
            "INSERT INTO daily_summary("
            "date,limit_up_count,rise_count,fall_count,raw_json,source_kind) "
            "VALUES ('2026-07-24',1,763,4185,'{\"fallback\":true}','fallback')"
        )
        assert collect_market_rise_fall(
            PreviousRiseFallClient(), store, "2026-07-27"
        ) == 1
        assert store.conn.execute(
            "SELECT count(*) FROM daily_summary WHERE date='2026-07-27'"
        ).fetchone()[0] == 0
        assert store.conn.execute(
            "SELECT limit_up_count,rise_count,fall_count,source_kind "
            "FROM daily_summary WHERE date='2026-07-24'"
        ).fetchone() == (14, 763, 4185, "real_cross_source")
        assert store.conn.execute(
            "SELECT source_kind FROM market_rise_fall WHERE date='2026-07-24'"
        ).fetchone()[0] == "real"
    finally:
        store.close()


def test_rise_fall_upsert_keeps_raw_json_bounded_across_runs(tmp_path):
    """Regression: the daily_summary audit payload must not re-embed the previous
    raw_json verbatim.  The old code re-escaped the whole blob every run
    (exponential ~2x/run growth) until the upsert OOM'd.  After the fix the stored
    raw_json reaches a bounded steady state and nesting depth stays <= 1."""
    import json

    store = DuckDBStore(tmp_path / "rise-fall-bloat.duckdb")
    try:
        init_schema(store.conn)
        client = PreviousRiseFallClient()  # always returns the 2026-07-24 row
        lengths = []
        for _ in range(9):
            assert collect_market_rise_fall(client, store, "2026-07-27") == 1
            lengths.append(
                store.conn.execute(
                    "SELECT length(raw_json) FROM daily_summary WHERE date='2026-07-24'"
                ).fetchone()[0]
            )
        # Steady state from the 3rd run onward must be flat (no exponential growth)
        # and far below the megabyte/gigabyte scale that triggered the OOM.
        assert lengths[-1] == lengths[2]
        assert lengths[-1] < 100_000
        # Nesting depth <= 1: a preserved payload must not itself carry a preserved blob.
        raw = store.conn.execute(
            "SELECT raw_json FROM daily_summary WHERE date='2026-07-24'"
        ).fetchone()[0]
        preserved = json.loads(raw).get("preserved_daily_summary")
        if isinstance(preserved, dict):
            assert "preserved_daily_summary" not in preserved
    finally:
        store.close()


def test_ths_membership_snapshot_age(tmp_path):
    """A4: detect the THS concept membership snapshot date and its age in days so a
    stale membership (> THS_MEMBERSHIP_MAX_AGE_DAYS) can be flagged instead of being
    certified as a fully-ready concept taxonomy."""
    import duckdb
    from scripts.collect_intraday_sector_flow_full import (
        _ths_membership_snapshot,
        THS_MEMBERSHIP_MAX_AGE_DAYS,
    )

    con = duckdb.connect(str(tmp_path / "ths.duckdb"))
    try:
        con.execute(
            "CREATE TABLE ths_concept_stock_history ("
            "trade_date DATE, concept_code VARCHAR, concept_name VARCHAR, stock_code VARCHAR)")
        # Membership snapshot 13 days before the trade date -> stale (> 10d).
        con.execute(
            "INSERT INTO ths_concept_stock_history VALUES "
            "('2026-07-15','c1','concept','000001')")
        snap, age = _ths_membership_snapshot(con, "2026-07-28")
        assert str(snap) == "2026-07-15"
        assert age == 13
        assert age > THS_MEMBERSHIP_MAX_AGE_DAYS
        # A fresh snapshot is not stale.
        con.execute(
            "INSERT INTO ths_concept_stock_history VALUES "
            "('2026-07-27','c1','concept','000001')")
        snap2, age2 = _ths_membership_snapshot(con, "2026-07-28")
        assert str(snap2) == "2026-07-27"
        assert age2 == 1
        assert age2 <= THS_MEMBERSHIP_MAX_AGE_DAYS
        # No snapshot at/before the trade date -> (None, None).
        assert _ths_membership_snapshot(con, "2026-07-01") == (None, None)
    finally:
        con.close()


def test_kpl_stale_tracker_accumulates_and_resets(tmp_path):
    """WP5 regression: the stale counter must actually persist.  The original used a
    literal ``current_timestamp`` inside ``ON CONFLICT DO UPDATE SET``, which DuckDB
    binds as a *column name* (BinderException) and the swallowed error left the table
    empty, so the consecutive-stale alert never fired."""
    from collect_market import _track_kpl_stale

    store = DuckDBStore(tmp_path / "stale.duckdb")
    try:
        # Consecutive stale runs accumulate.
        assert _track_kpl_stale(store, "2026-07-28", got_same_date=False) == 1
        assert _track_kpl_stale(store, "2026-07-28", got_same_date=False) == 2
        assert _track_kpl_stale(store, "2026-07-28", got_same_date=False) == 3
        # A same-date hit resets the counter.
        assert _track_kpl_stale(store, "2026-07-28", got_same_date=True) == 0
        # Rows are actually persisted (the bug left the table empty).
        assert store.conn.execute(
            "SELECT count(*) FROM kpl_stale_tracker").fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT consecutive_stale FROM kpl_stale_tracker WHERE date='2026-07-28'"
        ).fetchone()[0] == 0
        # updated_at is populated (not NULL).
        assert store.conn.execute(
            "SELECT updated_at IS NOT NULL FROM kpl_stale_tracker WHERE date='2026-07-28'"
        ).fetchone()[0]
    finally:
        store.close()


def test_self_heal_purges_raw_json_via_column_rebuild(tmp_path):
    """WP0 in-pipeline self-heal: purge the frozen oversized daily_summary.raw_json
    blob by dropping and re-adding the column -- a columnar metadata op that never
    materializes the giant value (a multi-GB single value cannot be allocated, so it
    must not be read).  Scalar data is preserved and the purge runs once (guarded)."""
    from collect_market import _self_heal_daily_summary_bloat

    store = DuckDBStore(tmp_path / "self-heal.duckdb")
    try:
        init_schema(store.conn)
        store.conn.execute(
            "INSERT INTO daily_summary(date,limit_up_count,raw_json,source_kind) "
            "VALUES ('2026-07-24', 14, '{\"bloated\":\"...\"}', 'real_cross_source')")
        store.conn.execute(
            "INSERT INTO daily_summary(date,limit_up_count,raw_json,source_kind) "
            "VALUES ('2026-07-27', 10, '{\"ok\":true}', 'real')")

        _self_heal_daily_summary_bloat(store)

        # raw_json is purged for every row (column dropped + recreated => NULL).
        assert store.conn.execute(
            "SELECT count(*) FROM daily_summary WHERE raw_json IS NOT NULL"
        ).fetchone()[0] == 0
        # Scalar data and source_kind on the healed row are preserved.
        assert store.conn.execute(
            "SELECT limit_up_count,source_kind FROM daily_summary WHERE date='2026-07-24'"
        ).fetchone() == (14, "real_cross_source")
        # Guarded: a second call is a no-op and later inserts still carry raw_json.
        _self_heal_daily_summary_bloat(store)
        store.conn.execute(
            "INSERT INTO daily_summary(date,limit_up_count,raw_json,source_kind) "
            "VALUES ('2026-07-28', 5, '{\"new\":true}', 'real')")
        assert store.conn.execute(
            "SELECT raw_json FROM daily_summary WHERE date='2026-07-28'"
        ).fetchone()[0] == '{"new":true}'
    finally:
        store.close()


def test_readiness_splits_analytics_and_execution(tmp_path):
    """P0#3: readiness must report analytics_ready (data present) separately from
    execution_ready (an executable candidate exists) and actionable_candidates, so a
    delayed-only snapshot is not shown as a green 'ready to trade' state."""
    import duckdb
    from schema import init_schema
    from trade_system.readiness import assess_trade_date_readiness

    db = tmp_path / "readiness.duckdb"
    con = duckdb.connect(str(db))
    init_schema(con)
    con.execute(
        "CREATE TABLE IF NOT EXISTS stock_candidate_stage_signal ("
        "trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, "
        "score DOUBLE, decision VARCHAR, is_actionable BOOLEAN)")
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-28','intraday_strength','600519','x',50,'blocked_data_quality',false)")
    con.commit()
    con.close()

    # Only a blocked candidate -> not execution-ready, 0 actionable.
    result = assess_trade_date_readiness(str(db), "2026-07-28", stage="intraday")
    assert "analytics_ready" in result and "execution_ready" in result
    assert result["actionable_candidates"] == 0
    assert result["execution_ready"] is False

    # Add an executable candidate -> execution-ready, 1 actionable.
    con = duckdb.connect(str(db))
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-28','intraday_strength','000001','y',80,'confirm',true)")
    con.commit()
    con.close()
    result2 = assess_trade_date_readiness(str(db), "2026-07-28", stage="intraday")
    assert result2["actionable_candidates"] == 1
    assert result2["execution_ready"] is True
