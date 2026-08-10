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


def test_capital_flow_health_surfaces_reconciliation(tmp_path):
    """P1-2: the health report must surface independent reconciliation status rather
    than silently passing on coverage alone."""
    import duckdb
    from trade_system.capital_flow_health import (
        assess_capital_flow_health,
        render_capital_flow_health_markdown,
    )

    db = tmp_path / "recon.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE intraday_stock_flow_reconciliation "
        "(trade_date DATE PRIMARY KEY, status VARCHAR, reference_rows INTEGER)")
    con.execute(
        "INSERT INTO intraday_stock_flow_reconciliation VALUES ('2026-07-28','not_run',0)")
    con.execute(
        "CREATE TABLE multi_source_stock_flow "
        "(source_date DATE, stock_code VARCHAR, provider VARCHAR, main_net DOUBLE)")
    con.commit()
    con.close()

    result = assess_capital_flow_health(str(db), "2026-07-28")
    assert result["reconciliation"]["status"] == "not_run"
    assert result["reconciliation"]["independent_reconciliation_ready"] is False
    assert result["reconciliation"]["independent_source_present"] is False
    md = render_capital_flow_health_markdown(result)
    assert "Independent reconciliation" in md
    assert "WARNING" in md  # degradation surfaced, not hidden


def test_stock_code_strips_exchange_suffix():
    """P1-1: member codes must be normalized to the bare 6-digit form regardless of
    exchange prefix or TuShare-style suffix."""
    from trade_system.ths_history import _stock_code
    assert _stock_code("000001.SZ") == "000001"
    assert _stock_code("603839.SH") == "603839"
    assert _stock_code("300898") == "300898"
    assert _stock_code("SH600519") == "600519"
    assert _stock_code("bj830799") == "830799"


def test_intraday_liquidity_gate_requires_complete_sell_side_quote():
    """An executable intraday candidate must have a positive live ask and size."""
    from trade_system.stage_signals import _row_evidence_actionable

    base_evidence = {
        "is_fallback": False,
        "stock_flow_close": 10.0,
        "stock_flow_main_net": 1234.0,
        "source_provider": "eastmoney_intraday_clist",  # live, not delayed
    }
    # Missing sell-side depth is analytics-only, never executable.
    ok, reason = _row_evidence_actionable(
        "intraday_strength", {"stage_evidence": dict(base_evidence)}, strict_tradability=True)
    assert ok is False and reason == "missing_sell_side_liquidity"

    # ask_price present but ask_volume missing -> incomplete -> blocked.
    ok2, reason2 = _row_evidence_actionable(
        "intraday_strength", {"stage_evidence": {**base_evidence, "ask_price": 10.1}},
        strict_tradability=True)
    assert ok2 is False and reason2 == "missing_sell_side_liquidity"

    ok3, reason3 = _row_evidence_actionable(
        "intraday_strength",
        {
            "stage_evidence": {
                **base_evidence,
                "ask_price": 10.1,
                "ask_volume": 5000,
            }
        },
        strict_tradability=True,
    )
    assert ok3 is True and reason3 is None

    # Delayed provider still blocks regardless of ask data.
    ok4, reason4 = _row_evidence_actionable(
        "intraday_strength",
        {"stage_evidence": {**base_evidence, "source_provider": "eastmoney_intraday_clist_delay"}},
        strict_tradability=True)
    assert ok4 is False and reason4 == "delayed_provider_not_executable"


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
        "score DOUBLE, decision VARCHAR, is_actionable BOOLEAN, "
        "is_executable BOOLEAN)")
    con.execute(
        "INSERT INTO stock_candidate_stage_signal VALUES "
        "('2026-07-28','intraday_strength','600519','x',50,'blocked_data_quality',false,false)")
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
        "('2026-07-28','intraday_strength','000001','y',80,'confirm',true,true)")
    con.commit()
    con.close()
    result2 = assess_trade_date_readiness(str(db), "2026-07-28", stage="intraday")
    assert result2["actionable_candidates"] == 1
    assert result2["execution_ready"] is True
