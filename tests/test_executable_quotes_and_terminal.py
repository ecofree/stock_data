"""Live executable quotes + terminal render contract tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from trade_system.executable_quotes import (
    TENCENT_SPOT_PROVIDER,
    is_delayed_provider,
    parse_tencent_parts,
    quote_trade_date,
    upsert_executable_quotes,
)
from trade_system.normalize import build_normalized_views
from legacy_diagnostics import generate_stage_signals
from trade_system.stage_signals import (
    ensure_stage_signal_schema,
)


def test_is_delayed_provider():
    assert is_delayed_provider("eastmoney_intraday_clist_delay") is True
    assert is_delayed_provider("eastmoney_intraday_clist") is False
    assert is_delayed_provider("tencent_spot_quote") is False


def test_quote_trade_date_requires_provider_date():
    assert quote_trade_date("20260730102800") == "2026-07-30"
    assert quote_trade_date("2026-07-30 10:28:00") == "2026-07-30"
    assert quote_trade_date("10:28:00") is None


def test_parse_tencent_parts_extracts_price_and_book():
    parts = [""] * 50
    parts[1] = "测试"
    parts[2] = "000001"
    parts[3] = "10.5"
    parts[4] = "10.0"
    parts[9] = "10.4"
    parts[10] = "100"
    parts[19] = "10.6"
    parts[20] = "200"
    parts[30] = "100000"
    parts[32] = "5.0"
    parsed = parse_tencent_parts(parts)
    assert parsed is not None
    assert parsed["stock_code"] == "000001"
    assert parsed["price"] == 10.5
    assert parsed["ask1"] == 10.6
    assert parsed["provider"] == TENCENT_SPOT_PROVIDER


def _intraday_db(tmp_path: Path, *, with_tencent: bool) -> Path:
    db = tmp_path / "intraday_exec.duckdb"
    trade_date = "2026-07-30"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE daily_summary("
        "date DATE, limit_up_count INTEGER, limit_down_count INTEGER, rise_count INTEGER, "
        "fall_count INTEGER, consecutive_count INTEGER, fetched_at TIMESTAMP, source_kind VARCHAR)"
    )
    con.execute(
        "INSERT INTO daily_summary VALUES (?,70,4,2500,2200,3,? || ' 10:30:00','real')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE multi_source_stock_flow("
        "source_date DATE, stock_code VARCHAR, main_net DOUBLE, super_net DOUBLE, "
        "large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE, close DOUBLE, change_pct DOUBLE, "
        "turnover DOUBLE, provider VARCHAR, fetched_at TIMESTAMP, is_stale BOOLEAN)"
    )
    # Only delayed full-market flow — previously blocked all executables.
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "(?, '000001', 5e7, 0, 0, 0, 0, 10.2, 3.0, 1e7, "
        "'eastmoney_intraday_clist_delay', ? || ' 10:25:00', false)",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE multi_source_sector_flow("
        "source_date DATE, sector_code VARCHAR, sector_name VARCHAR, main_net DOUBLE, "
        "super_net DOUBLE, large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE, "
        "change_pct DOUBLE, main_ratio DOUBLE, provider VARCHAR, sector_type VARCHAR, "
        "amount_unit VARCHAR, fetched_at TIMESTAMP, is_stale BOOLEAN)"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES "
        "(?, 'BK1', '商业航天', 1e7, 0, 0, 0, 0, 1.0, 0.1, 'eastmoney', 'em_industry', "
        "'yuan', ? || ' 10:25:00', false)",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE sector_capital("
        "date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, "
        "big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO sector_capital VALUES (?, 'BK1', 1e7, 0, 0, 0, 0, ? || ' 10:25:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE l2_realtime_all_boards("
        "date DATE, board_level INTEGER, stock_code VARCHAR, stock_name VARCHAR, "
        "limit_up_time VARCHAR, fetched_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO l2_realtime_all_boards VALUES "
        "(?, 2, '000001', '测试股', '09:40', ? || ' 10:20:00')",
        [trade_date, trade_date],
    )
    con.execute(
        "CREATE TABLE stock_candidate_score("
        "trade_date VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, "
        "evidence_json VARCHAR, source VARCHAR)"
    )
    con.execute(
        "INSERT INTO stock_candidate_score VALUES (?,?,?,?,?,?)",
        [trade_date, "000001", "测试股", 95.0, "{}", "limit_pool"],
    )
    # Seed prior auction stage so source score stays high for follow.
    ensure_stage_signal_schema(con)
    con.execute(
        "INSERT INTO stock_candidate_stage_signal "
        "(trade_date, stage, stock_code, stock_name, score, decision, evidence_json, "
        "source_trade_date, is_actionable, feature_version) VALUES "
        "(?, 'auction_confirmation', '000001', '测试股', 90, 'confirm', '{}', ?, true, 'stage_v2_asof')",
        [trade_date, trade_date],
    )
    # Strong flow so capital_flow strength pushes composite score >= 70:
    # stage_score = source*0.45 + strength*0.55, strength ~= main_net/1e7.
    con.execute(
        "UPDATE multi_source_stock_flow SET main_net=8e8 WHERE stock_code='000001'"
    )
    if with_tencent:
        upsert_executable_quotes(
            con,
            trade_date,
            {
                "000001": {
                    "price": 10.55,
                    "pre_close": 10.0,
                    "bid1": 10.54,
                    "ask1": 10.56,
                    "bid1_vol": 100,
                    "ask1_vol": 200,
                    "change_pct": 5.5,
                    "provider": TENCENT_SPOT_PROVIDER,
                    "quote_time": "20260730102800",
                }
            },
            fetched_at=datetime.fromisoformat(f"{trade_date}T10:28:00"),
        )
    con.close()
    build_normalized_views(str(db))
    return db


def test_intraday_strict_blocks_delay_only_without_live_quote(tmp_path):
    db = _intraday_db(tmp_path, with_tencent=False)
    result = generate_stage_signals(
        str(db),
        "2026-07-30",
        "intraday_strength",
        as_of_time="2026-07-30T10:30:00",
        run_id="t1",
        limit=10,
        freshness_seconds=None,
        strict_tradability=True,
    )
    con = duckdb.connect(str(db), read_only=True)
    decision, actionable, evidence = con.execute(
        "SELECT decision, is_actionable, evidence_json FROM stock_candidate_stage_signal "
        "WHERE stage='intraday_strength'"
    ).fetchone()
    con.close()
    import json

    reason = json.loads(evidence).get("row_block_reason")
    assert result["actionable"] == 0
    assert actionable is False
    assert reason == "delayed_provider_not_executable"
    assert decision == "blocked_data_quality"


def test_intraday_strict_quote_is_tradable_but_waits_for_risk_approval(tmp_path):
    db = _intraday_db(tmp_path, with_tencent=True)
    result = generate_stage_signals(
        str(db),
        "2026-07-30",
        "intraday_strength",
        as_of_time="2026-07-30T10:30:00",
        run_id="t2",
        limit=10,
        freshness_seconds=None,
        strict_tradability=True,
    )
    con = duckdb.connect(str(db), read_only=True)
    decision, actionable, tradable, risk_approved, executable, ref, ref_type, evidence = con.execute(
        "SELECT decision, is_actionable, tradable, risk_approved, is_executable, reference_price, "
        "reference_price_type, evidence_json FROM stock_candidate_stage_signal "
        "WHERE stage='intraday_strength'"
    ).fetchone()
    con.close()
    import json

    ev = json.loads(evidence)
    stage_ev = ev.get("stage_evidence") or {}
    assert result["actionable"] >= 1
    assert decision == "follow"
    assert actionable is True
    assert tradable is True
    assert risk_approved is False
    assert executable is False
    assert float(ref) == 10.55
    assert ref_type == "tencent_spot_quote"
    assert stage_ev.get("executable_provider") == TENCENT_SPOT_PROVIDER
    assert ev.get("row_block_reason") in (None, "")


def test_wrong_date_quote_is_rejected(tmp_path):
    db = tmp_path / "wrong_date.duckdb"
    con = duckdb.connect(str(db))
    written = upsert_executable_quotes(
        con,
        "2026-07-30",
        {
            "000001": {
                "price": 10.5,
                "ask1": 10.6,
                "ask1_vol": 100,
                "provider": TENCENT_SPOT_PROVIDER,
                "quote_time": "20260729150000",
            }
        },
    )
    assert written == 0
    assert con.execute("SELECT count(*) FROM executable_quote_snapshot").fetchone()[0] == 0
    con.close()


def test_intraday_strict_blocks_quote_without_sell_side_liquidity(tmp_path):
    db = _intraday_db(tmp_path, with_tencent=True)
    con = duckdb.connect(str(db))
    con.execute(
        "UPDATE executable_quote_snapshot SET ask1=NULL, ask1_vol=NULL "
        "WHERE stock_code='000001'"
    )
    con.close()
    result = generate_stage_signals(
        str(db),
        "2026-07-30",
        "intraday_strength",
        as_of_time="2026-07-30T10:30:00",
        run_id="no_ask",
        limit=10,
        freshness_seconds=None,
        strict_tradability=True,
    )
    con = duckdb.connect(str(db), read_only=True)
    actionable, executable, evidence = con.execute(
        "SELECT is_actionable, is_executable, evidence_json "
        "FROM stock_candidate_stage_signal WHERE stage='intraday_strength'"
    ).fetchone()
    con.close()
    import json

    assert result["actionable"] == 0
    assert actionable is False
    assert executable is False
    assert json.loads(evidence)["row_block_reason"] == "missing_sell_side_liquidity"
