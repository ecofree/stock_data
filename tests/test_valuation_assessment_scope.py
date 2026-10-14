"""One fresh evidence read per assessment, never a persistent green cache."""
from trade_system.tushare_history import TushareHistoryCollector


class NoRequests:
    def query_rows(self, *args, **kwargs):
        raise AssertionError('assessment must not acquire provider data')


def test_daily_worklist_validates_current_reviews_once_per_call(tmp_path, monkeypatch):
    with TushareHistoryCollector(tmp_path / 'single-assessment.duckdb', client=NoRequests()) as reader:
        reader.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES ('000001.SZ')")
        original = reader.qualified_valuation_reviews
        calls = []

        def counted(day, *, observed_at=None):
            calls.append((day, observed_at))
            return original(day, observed_at=observed_at)

        monkeypatch.setattr(reader, 'qualified_valuation_reviews', counted)
        first = reader.prepare_valuation_reviews('20260701', ['000001.SZ'],
                                                  observed_at='2026-07-01T17:00:00+08:00')
        assert len(calls) == 1
        assert first['rows']['000001.SZ']['qualified_core'] is False
        second = reader.prepare_valuation_reviews('20260701', ['000001.SZ'],
                                                   observed_at='2026-07-02T17:00:00+08:00')
        assert len(calls) == 2
        assert calls[0][1] != calls[1][1]
        assert first['observed_at'] != second['observed_at']
        assert second['business_rows_written'] == 0
        assert not first['permits_requests'] and not second['permits_requests']
