from __future__ import annotations

from trade_system.kpl_history import KPLHistoryCollector
from trade_system.tushare_history import TushareHistoryCollector
from trade_system.tushare_relay import TushareRelayError


class FakeTushare:
    def query_rows(self, api, params=None, fields=""):
        if api == "trade_cal":
            return [{"exchange": "SSE", "cal_date": "20260710", "is_open": 1, "pretrade_date": "20260709"}]
        if api == "stock_basic":
            return [{"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "industry": "银行"}]
        if api == "daily":
            return [{"ts_code": "000001.SZ", "trade_date": "20260710", "open": 10, "high": 11,
                     "low": 9, "close": 10.5, "vol": 100, "amount": 1000, "pct_chg": 1}]
        if api == "daily_basic":
            return [{"ts_code": "000001.SZ", "trade_date": "20260710", "turnover_rate": 2,
                     "volume_ratio": 1.2, "pe": 8, "pb": 1, "total_mv": 100000, "circ_mv": 90000}]
        if api == "moneyflow":
            return [{"ts_code": "000001.SZ", "trade_date": "20260710", "buy_sm_amount": 1,
                     "sell_sm_amount": 0, "buy_md_amount": 2, "sell_md_amount": 0,
                     "buy_lg_amount": 3, "sell_lg_amount": 1, "buy_elg_amount": 4,
                     "sell_elg_amount": 1, "net_mf_amount": 8}]
        if api in {"moneyflow_ind_dc", "moneyflow_ind_ths"}:
            return [{"trade_date": "20260710", "ts_code": "801001", "name": "银行", "pct_change": 1,
                     "close": 100, "net_amount": 10, "buy_elg_amount": 4, "sell_elg_amount": 1,
                     "buy_lg_amount": 3, "sell_lg_amount": 1, "buy_md_amount": 2, "sell_md_amount": 1,
                     "buy_sm_amount": 1, "sell_sm_amount": 0}]
        return []


def test_tushare_history_is_batched_checkpointed_and_normalized(tmp_path):
    db = tmp_path / "history.duckdb"
    with TushareHistoryCollector(db, client=FakeTushare(), budget_seconds=30) as collector:
        result = collector.run(
            "20260710", "20260710",
            datasets=["stock_basic", "daily", "daily_basic", "moneyflow", "industry_flow"],
            max_days=1,
        )
        assert not any(item["status"] == "error" for item in result["results"])
        assert collector.store.conn.execute("select count(*) from tushare_daily_basic").fetchone()[0] == 1
        assert collector.store.conn.execute("select count(*) from tushare_moneyflow").fetchone()[0] == 1
        assert collector.store.conn.execute("select count(*) from multi_source_stock_flow").fetchone()[0] == 1
        assert collector.store.conn.execute("select main_net,net_total from multi_source_stock_flow").fetchone() == (50000, 80000)
        assert collector.store.conn.execute("select count(*) from multi_source_sector_flow").fetchone()[0] == 1
        assert collector.store.conn.execute("select main_net from multi_source_sector_flow").fetchone()[0] == 10
        assert collector.store.conn.execute("select status from history_fetch_checkpoint where dataset='moneyflow'").fetchone()[0] == "success"


class ClosedDayTushare:
    def query_rows(self, api, params=None, fields=""):
        assert api == "trade_cal"
        return [{
            "exchange": "SSE",
            "cal_date": params["start_date"],
            "is_open": 0,
            "pretrade_date": "20260724",
        }]


class BrokenCalendarTushare:
    def query_rows(self, api, params=None, fields=""):
        raise RuntimeError("relay unavailable")


class TransientDailyTushare:
    def __init__(self):
        self.daily_attempts = 0

    def query_rows(self, api, params=None, fields=""):
        if api == "trade_cal":
            return [{"exchange": "SSE", "cal_date": "20260710", "is_open": 1, "pretrade_date": "20260709"}]
        if api == "daily":
            self.daily_attempts += 1
            if self.daily_attempts == 1:
                raise RuntimeError("temporary relay reset")
            return [{
                "ts_code": "000001.SZ", "trade_date": "20260710", "open": 10,
                "high": 11, "low": 9, "close": 10.5, "vol": 100,
                "amount": 1000, "pct_chg": 1,
            }]
        return []


def test_tushare_calendar_accepts_verified_closed_day_without_weekday_fallback(tmp_path):
    db = tmp_path / "closed-calendar.duckdb"
    with TushareHistoryCollector(db, client=ClosedDayTushare()) as collector:
        assert collector.ensure_calendar("20260727", "20260727") == []


def test_tushare_calendar_failure_is_fail_closed(tmp_path):
    db = tmp_path / "failed-calendar.duckdb"
    with TushareHistoryCollector(db, client=BrokenCalendarTushare()) as collector:
        try:
            collector.ensure_calendar("20260727", "20260727")
        except TushareRelayError as exc:
            assert "trade_cal fetch failed" in str(exc)
        else:
            raise AssertionError("calendar relay failure must not synthesize a weekday")


def test_tushare_failed_checkpoint_gets_one_bounded_outer_retry(tmp_path):
    db = tmp_path / "transient.duckdb"
    client = TransientDailyTushare()
    with TushareHistoryCollector(db, client=client, budget_seconds=30) as collector:
        result = collector.run(
            "20260710",
            "20260710",
            datasets=["daily"],
            retry_passes=1,
            retry_delay_seconds=0,
        )
        assert result["results"] == [{
            "dataset": "daily",
            "trade_date": "2026-07-10",
            "status": "success",
            "rows": 1,
        }]
        assert client.daily_attempts == 2
        assert collector.store.conn.execute(
            "SELECT status,attempts,rows_written FROM history_fetch_checkpoint "
            "WHERE dataset='daily' AND trade_date='2026-07-10'"
        ).fetchone() == ("success", 2, 1)


def test_tushare_industry_zero_close_rows_are_kept_raw_but_not_normalized(tmp_path):
    db = tmp_path / "industry_quality.duckdb"
    with TushareHistoryCollector(db, client=FakeTushare(), budget_seconds=30) as collector:
        collector.store.conn.execute(
            "INSERT INTO tushare_moneyflow_industry "
            "(trade_date, ts_code, sector_name, close, net_amount) VALUES "
            "('2026-07-10', 'BK9999.DC', 'theme placeholder', 0, 0)"
        )
        collector.store.conn.commit()
        collector.sync_sector_flow("20260710")
        assert collector.store.conn.execute(
            "SELECT count(*) FROM tushare_moneyflow_industry WHERE ts_code='BK9999.DC'"
        ).fetchone()[0] == 1
        assert collector.store.conn.execute(
            "SELECT count(*) FROM multi_source_sector_flow WHERE sector_code='BK9999.DC'"
        ).fetchone()[0] == 0


class FakeKPL:
    stats = {"success": 1, "error": 0}

    def get(self, endpoint, params=None, critical=False):
        assert endpoint == "/sector/ranking"
        return {"sectors": [{"sector_code": "C001", "sector_name": "银行概念", "stock_count": 1,
                              "stocks": [{"stock_code": "000001", "stock_name": "平安银行"}]}]}


def test_kpl_ranking_history_keeps_concepts_and_members(tmp_path):
    db = tmp_path / "kpl_history.duckdb"
    with KPLHistoryCollector(db) as collector:
        collector.client = FakeKPL()
        # Historical collectors require an explicitly verified session list;
        # they must not manufacture weekdays when the calendar is absent.
        collector.store.conn.execute(
            "INSERT INTO tushare_trade_cal(exchange,cal_date,is_open) "
            "VALUES ('SSE','2026-07-10',true)"
        )
        collector.store.conn.commit()
        result = collector.run("20260710", "20260710", max_days=1)
        assert result["results"][0]["status"] == "success"
        assert collector.store.conn.execute("select concept_code,concept_name from kpl_concept_daily").fetchone() == ("C001", "银行概念")
        assert collector.store.conn.execute("select stock_code from kpl_concept_stock_history").fetchone()[0] == "000001"
        assert collector.store.conn.execute("select date_verified from kpl_concept_daily").fetchone()[0] is False
