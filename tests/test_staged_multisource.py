from __future__ import annotations

from datetime import date

from trade_system.staged_multisource import STAGE_ORDER, StageScheduler


def test_stage_plan_is_ordered_and_does_not_mix_all_sources(tmp_path):
    calls = []

    def fake_fetcher(data_type, code=None, **kwargs):
        calls.append((data_type, code))
        return [], {"source": "fake", "status": "live"}

    with StageScheduler(tmp_path / "plan.duckdb", "2026-07-13", ["000001"], fetcher=fake_fetcher) as scheduler:
        assert STAGE_ORDER == ("premarket", "open", "midday", "close", "after_close")
        planned = scheduler.plan("close")
        assert [task.data_type for task in planned] == [
            "kline", "index_kline", "index_kline", "index_kline", "stock_flow", "sector_flow"
        ]
        assert all(task.kwargs.get("full_history") is False for task in planned if task.data_type == "kline")
        assert calls == []


def test_stage_run_saves_each_task_and_resumes_from_checkpoint(tmp_path):
    db = tmp_path / "run.duckdb"
    calls = []

    def fake_fetcher(data_type, code=None, **kwargs):
        calls.append((data_type, code, kwargs))
        if data_type in {"kline", "index_kline"}:
            return ([{"date": "2026-07-13", "open": 10, "high": 11, "low": 9,
                      "close": 10.5, "volume": 10, "amount": 100}],
                    {"source": "fake", "status": "live"})
        if data_type == "stock_flow":
            return ([{"date": "2026-07-13", "main_net": 4}], {"source": "fake", "status": "live"})
        if data_type == "sector_flow":
            return ([{"sector_code": "BK0001", "main_net": 7}], {"source": "fake", "status": "live"})
        return ([{"date": "2026-07-13"}], {"source": "fake", "status": "live"})

    with StageScheduler(db, date.today().isoformat(), ["000001"], fetcher=fake_fetcher) as scheduler:
        first = scheduler.run("close")
        first_call_count = len(calls)
        assert first["failed"] == 0
        assert first["completed"] == 6
        assert scheduler.store.con.execute(
            "select count(*) from multi_source_task_checkpoint where status='success'"
        ).fetchone()[0] == 6
        assert scheduler.store.con.execute("select count(*) from multi_source_stock_flow").fetchone()[0] == 1
        assert scheduler.store.con.execute("select count(*) from multi_source_sector_flow").fetchone()[0] == 1
        assert scheduler.store.con.execute("select count(*) from multi_source_kline").fetchone()[0] == 4
        second = scheduler.run("close")
        assert second["failed"] == 0
        assert second["skipped"] == 6
        assert len(calls) == first_call_count


def test_current_snapshot_tasks_are_not_relabelled_for_historical_dates(tmp_path):
    calls = []

    def fake_fetcher(data_type, code=None, **kwargs):
        calls.append((data_type, code))
        return [], {"source": "fake", "status": "live"}

    with StageScheduler(tmp_path / "historical.duckdb", "2026-07-10", ["000001"], fetcher=fake_fetcher) as scheduler:
        result = scheduler.run("open")
        assert result["failed"] == 0
        assert result["completed"] == 0
        assert result["skipped"] == len(scheduler.plan("open"))
        assert calls == []


def test_dry_run_never_calls_fetcher(tmp_path):
    calls = []

    def fake_fetcher(data_type, code=None, **kwargs):
        calls.append(data_type)
        return [], {"source": "fake", "status": "live"}

    with StageScheduler(tmp_path / "dry.duckdb", "2026-07-13", ["000001"], fetcher=fake_fetcher) as scheduler:
        result = scheduler.run(["premarket", "close"], dry_run=True)
        assert result["dry_run"] is True
        assert result["planned"] == len(scheduler.plan("premarket")) + len(scheduler.plan("close"))
        assert calls == []
