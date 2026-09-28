from __future__ import annotations

import duckdb
import pytest

from trade_system.tushare_history import TushareHistoryCollector
from trade_system.xiaodefa_source import XiaodefaClient


class FakeClient:
    def query_rows(self, api_name, params=None, fields=""):
        assert api_name == "adj_factor"
        return [{"ts_code": "000001.SZ", "trade_date": "20260714", "adj_factor": 139.008}]


def test_adj_factor_date_snapshot_is_persisted(tmp_path):
    db = tmp_path / "history.duckdb"
    with TushareHistoryCollector(db, client=FakeClient()) as collector:
        assert collector._collect_adj_factor("20260714") == 1
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select ts_code,stock_code,cast(date as varchar),adj_factor from tushare_adj_factor").fetchone() == (
            "000001.SZ", "000001", "2026-07-14", 139.008
        )
    finally:
        con.close()





class XiaodefaDailyFixture(XiaodefaClient):
    def __init__(self):
        super().__init__(token="fixture")

    def query_rows(self, api_name, params=None, fields="", *, _deadline=None):
        assert api_name == "daily"
        return [
            {
                "ts_code": f"000{i:03d}.SZ",
                "trade_date": "20260714",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 100,
                "amount": 1000,
                "pct_chg": 1,
            }
            for i in range(1000)
        ]


def test_retained_xiaodefa_close_snapshot_is_certified(tmp_path):
    db = tmp_path / "fallback.duckdb"
    with TushareHistoryCollector(db, client=XiaodefaDailyFixture()) as collector:
        collector.store.conn.execute(
            "INSERT INTO tushare_stock_basic(ts_code,stock_code) "
            "SELECT '000' || lpad(CAST(i AS VARCHAR),3,'0') || '.SZ', "
            "'000' || lpad(CAST(i AS VARCHAR),3,'0') FROM range(1000) t(i)"
        )
        collector.store.conn.commit()
        assert collector._collect_daily("20260714") == 1000
        cert = collector.store.conn.execute(
            "SELECT provider,status,distinct_codes FROM close_snapshot_certification "
            "WHERE dataset='daily' AND trade_date='2026-07-14'"
        ).fetchone()
    assert cert == ("xiaodefa", "certified", 1000)


@pytest.mark.parametrize('failure', ['empty_amounts', 'invalid_amount', 'publish_failure'])
def test_flow_normalization_preserves_missing_values_and_dc_net_definition(tmp_path, monkeypatch, capsys, failure):
    import json
    from trade_system.collection_profiles import read_product_counts
    context = {'demand_id': 'flow-rollback-test'}
    monkeypatch.setenv('STOCKDATA_REQUEST_CONTEXT', json.dumps(context))
    class FlowFixture:
        def query_rows(self, api_name, params=None, fields=""):
            if api_name == "moneyflow_ind_dc":
                assert not any(f.startswith("sell_") for f in fields.split(","))
                return [{"trade_date": "20260714", "ts_code": "BK001.DC", "close": 100,
                         "net_amount": 30, "buy_elg_amount": 10, "buy_lg_amount": -3}]
            assert api_name == "moneyflow"
            return [{"trade_date": "20260714", "ts_code": "000001.SZ",
                     "buy_elg_amount": 10, "sell_elg_amount": 4,
                     "buy_lg_amount": 8, "sell_lg_amount": 2,
                     "buy_sm_amount": 1, "sell_sm_amount": 1,
                     "buy_md_amount": 1, "sell_md_amount": 1}]
    with TushareHistoryCollector(tmp_path / "flow.duckdb", client=FlowFixture()) as collector:
        collector.store.conn.execute("INSERT INTO tushare_trade_cal(exchange,cal_date,is_open) "
            "VALUES ('SSE','2026-07-14',true),('SZSE','2026-07-14',true)")
        collector._collect_industry_flow("20260714")
        collector.sync_sector_flow("20260714")
        assert collector.store.conn.execute(
            "SELECT main_net,super_net,large_net,mid_net,small_net FROM multi_source_sector_flow"
        ).fetchone() == (30, 10, -3, None, None)
        collector._collect_moneyflow("20260714")
        collector.sync_stock_flow("20260714")
        assert collector.store.conn.execute(
            "SELECT main_net,super_net,large_net,mid_net,small_net,net_total FROM multi_source_stock_flow"
        ).fetchone() == (120000, 60000, 60000, 0, 0, None)
        query = collector.client.query_rows
        def refresh(api_name, params=None, fields=''):
            rows = query(api_name, params, fields)
            for row in rows:
                if failure == 'empty_amounts':
                    for field in list(row):
                        if 'amount' in field:
                            row[field] = None
                else:
                    row['buy_elg_amount'] = float('nan') if failure == 'invalid_amount' else 99
            return rows
        monkeypatch.setattr(collector.client, 'query_rows', refresh)
        if failure == 'publish_failure':
            original = collector._checkpoint
            def checkpoint(dataset, day, status, **kwargs):
                original(dataset, day, status, **kwargs)
                if status == 'success':
                    raise RuntimeError('failure after standard publication')
            monkeypatch.setattr(collector, '_checkpoint', checkpoint)
        tables = ['tushare_moneyflow', 'tushare_moneyflow_industry',
                  'multi_source_stock_flow', 'multi_source_sector_flow']
        before = {t: collector.store.conn.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
        receipts = collector.store.conn.execute('SELECT count(*) FROM multi_source_observation').fetchone()[0]
        result = collector.run('20260714', '20260714', datasets=['moneyflow', 'industry_flow'], force=True)
        assert {r['status'] for r in result['results']} == {'error'}
        assert {t: collector.store.conn.execute(f'SELECT * FROM {t}').fetchall() for t in tables} == before
        assert collector.store.conn.execute('SELECT count(*) FROM multi_source_observation').fetchone()[0] == receipts + 2
    counts = read_product_counts(capsys.readouterr().out, context)['scopes']
    for api in ('moneyflow', 'moneyflow_ind_dc'):
        assert counts['tushare_history.' + api]['rows_parsed'] == 2
        assert counts['tushare_history.' + api]['rows_written'] == 1
    for projection in ('stock_flow_projection', 'sector_flow_projection'):
        assert counts['tushare_history.' + projection]['rows_written'] == 2
        assert counts['tushare_history.' + projection]['rows_parsed'] == (3 if failure == 'publish_failure' else 2)


class ScopedFixture:
    def __init__(self):
        self.calls = []
        self.empty_codes = set()
        self.wrong_date = False

    def query_rows(self, api, params=None, fields=""):
        self.calls.append((api, dict(params)))
        assert api in {"daily", "daily_basic", "adj_factor", "index_daily"}
        if params["ts_code"] in self.empty_codes:
            return []
        return [{"ts_code": params["ts_code"],
                 "trade_date": "20260704" if self.wrong_date else params["trade_date"],
                 "close": 10, "adj_factor": 2, "pe": None}]


def seed_calendar(collector):
    collector.store.conn.execute("INSERT INTO tushare_trade_cal(exchange,cal_date,is_open) "
        "SELECT e,CAST(d AS DATE),opened FROM (VALUES ('SSE'),('SZSE')) x(e) CROSS JOIN "
        "(VALUES ('2026-07-01',true),('2026-07-02',true),('2026-07-03',true),('2026-07-04',false),('2026-07-05',false)) y(d,opened)")


def test_scoped_overlap_only_fetches_missing_sessions_and_preserves_old_done(tmp_path):
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "scope.duckdb", client=client) as c:
        seed_calendar(c)
        c.store.conn.execute("INSERT INTO tushare_backfill_task(task_id,status,rows_inserted) "
                             "VALUES ('old-task','done',1)")
        options = dict(datasets=["daily"], stock_codes=["1"])
        first = c.run("20260701", "20260702", **options)
        assert {r["status"] for r in first["results"]} == {"success"}
        before = c.store.conn.execute("SELECT * FROM tushare_daily ORDER BY date").fetchall()
        c.run("20260701", "20260702", **options)
        assert len(client.calls) == 2
        assert c.store.conn.execute("SELECT * FROM tushare_daily ORDER BY date").fetchall() == before
        c.run("20260702", "20260705", **options)
        assert len(client.calls) == 3
        assert client.calls[-1][1] == {"ts_code": "000001.SZ", "trade_date": "20260703"}
        # A removed fact makes a real gap even though an old checkpoint says success.
        c.store.conn.execute("DELETE FROM tushare_daily WHERE date='2026-07-02'")
        c.run("20260701", "20260702", **options)
        assert len(client.calls) == 4
        assert c.store.conn.execute("SELECT status,rows_inserted FROM tushare_backfill_task").fetchall() == [("done", 1)]


def test_partial_scope_retains_receipts_and_retries_only_uncovered_instrument(tmp_path, monkeypatch, capsys):
    import json
    from trade_system.collection_profiles import read_product_counts
    context = {'demand_id': 'partial-write-test'}
    monkeypatch.setenv('STOCKDATA_REQUEST_CONTEXT', json.dumps(context))
    client = ScopedFixture()
    client.empty_codes = {"000002.SZ"}
    with TushareHistoryCollector(tmp_path / "partial.duckdb", client=client) as c:
        seed_calendar(c)
        options = dict(datasets=["daily"], stock_codes=["1", "2"])
        first = c.run("20260701", "20260701", **options)
        assert first["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT status FROM history_fetch_checkpoint").fetchall() == [("error",)]
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 1
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation").fetchone()[0] == 2
        assert c._product_counts['daily'] == {'rows_parsed': 1, 'rows_written': 1}
        client.empty_codes.clear()
        second = c.run("20260701", "20260701", **options)
        assert second["results"][0]["status"] == "success"
        assert len(client.calls) == 3
        assert client.calls[-1][1]["ts_code"] == "000002.SZ"
        # Fully covered third run performs no extra acquisition or upsert.
        c.run("20260701", "20260701", **options)
        assert len(client.calls) == 3
    counts = read_product_counts(capsys.readouterr().out, context)['scopes']
    assert counts['tushare_history.daily']['rows_parsed'] == 2
    assert counts['tushare_history.daily']['rows_written'] == 2
    c.close()
    assert capsys.readouterr().out == ''


def test_plan_excludes_prelisting_but_does_not_infer_suspension(tmp_path):
    from trade_system.tushare_store import stock_code_to_ts_code
    assert stock_code_to_ts_code('920009') == '920009.BJ'
    assert stock_code_to_ts_code('900901') == '900901.SH'
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "plan.duckdb", client=client) as c:
        seed_calendar(c)
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code,list_date,delist_date) VALUES ('000002.SZ','2026-07-02',NULL),('000003.SZ','2020-01-01','2026-07-01')")
        # Equal row counts on the wrong session cannot cover the requested session.
        c.store.conn.execute("INSERT INTO tushare_daily(ts_code,date,close) VALUES ('000001.SZ','2026-07-04',10)")
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1", "2", "3"], plan_only=True)
        assert result["results"][0]["missing_codes"] == ["000001.SZ"]
        assert result["results"][0]["not_listed_codes"] == ["000002.SZ", "000003.SZ"]
        assert not client.calls
        assert c.store.conn.execute("SELECT count(*) FROM history_fetch_checkpoint").fetchone()[0] == 0
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code,list_date) "
                             "SELECT '00000' || i || '.SZ','2020-01-01' FROM range(4,8) t(i)")
        evidence = dict(params={'trade_date': '20260701'}, rows=[
            dict(ts_code='000004.SZ', trade_date='20260701', suspend_type='S', suspend_timing=''),
            dict(ts_code='000005.SZ', trade_date='20260701', suspend_type='S', suspend_timing='09:30-10:00'),
            dict(ts_code='000006.SZ', trade_date='20260701', suspend_type='R', suspend_timing=''),
        ])
        c._record_snapshot('suspend_d', evidence)
        # No daily placeholder exists for the independently confirmed full-day suspension.
        assert c._applicable_codes({'000004.SZ','000005.SZ','000006.SZ','000007.SZ'}, '20260701', 'daily') == {
            '000005.SZ', '000006.SZ', '000007.SZ'}
        assert '000004.SZ' in c._expected_stock_codes('20260701', 'adj_factor')
        assert '000004.SZ' in c._expected_stock_codes('20260701', 'daily_basic')
        assert '000004.SZ' not in c._expected_stock_codes('20260701', 'moneyflow')
        c.store.conn.execute("INSERT INTO tushare_daily(ts_code,date,close,volume,turnover) "
            "VALUES ('000004.SZ','2026-07-01',10,100,1000)")
        assert '000004.SZ' in c._expected_stock_codes('20260701', 'daily')


def test_wrong_session_is_rejected_with_raw_receipt_retained(tmp_path):
    client = ScopedFixture()
    client.wrong_date = True
    with TushareHistoryCollector(tmp_path / "wrong.duckdb", client=client) as c:
        seed_calendar(c)
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1"])
        assert result["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 0
        assert '20260704' in c.store.conn.execute("SELECT payload_json FROM multi_source_observation").fetchone()[0]


def test_forced_empty_refresh_cannot_reuse_old_rows_as_success(tmp_path):
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "refresh.duckdb", client=client) as c:
        seed_calendar(c)
        options = dict(datasets=["daily"], stock_codes=["1"])
        c.run("20260701", "20260701", **options)
        previous = c.store.conn.execute("SELECT * FROM tushare_daily").fetchall()
        client.empty_codes.add("000001.SZ")
        result = c.run("20260701", "20260701", force=True, **options)
        assert result["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT * FROM tushare_daily").fetchall() == previous


def test_pagination_failure_keeps_all_received_pages_without_publishing(tmp_path, monkeypatch):
    class Repeating(XiaodefaClient):
        def __init__(self):
            super().__init__(token="fixture")
        def query_rows(self, api, params=None, fields="", *, _deadline=None):
            if api == 'stock_basic':
                return [] if params['list_status'] == 'D' else [dict(
                    ts_code='000001.SZ', symbol='000001', list_date='19910403', list_status='L')]
            return [{"ts_code": "000001.SZ", "trade_date": "20260701", "close": 10}] * 100
    with TushareHistoryCollector(tmp_path / "pages.duckdb", client=Repeating(), batch_limit=100) as c:
        monkeypatch.setattr(c, '_bse_listing_membership', lambda: None)
        seed_calendar(c)
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1"])
        assert result["results"][0]["status"] == "error"
        assert "repeated page" in result["results"][0]["error"]
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation WHERE data_type='tushare_daily'").fetchone()[0] == 2
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 0


def test_equal_count_with_wrong_instrument_cannot_certify_full_snapshot(tmp_path):
    import pytest
    from trade_system.xiaodefa_source import XiaodefaError
    class WrongUniverse(XiaodefaDailyFixture):
        def query_rows(self, *args, **kwargs):
            if args[0] == 'suspend_d':
                return []
            rows = super().query_rows(*args, **kwargs)
            rows[-1]["ts_code"] = "600999.SH"
            return rows
    with TushareHistoryCollector(tmp_path / "identity.duckdb", client=WrongUniverse()) as c:
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code,stock_code) "
            "SELECT '000' || lpad(CAST(i AS VARCHAR),3,'0') || '.SZ', "
            "'000' || lpad(CAST(i AS VARCHAR),3,'0') FROM range(1000) t(i)")
        with pytest.raises(XiaodefaError, match="1 missing instruments"):
            c._collect_daily("20260714")
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 0
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation WHERE data_type='tushare_daily'").fetchone()[0] == 1


def test_offline_plan_needs_no_provider_credential(tmp_path, monkeypatch):
    import hashlib
    from trade_system import xiaodefa_source
    monkeypatch.setattr(xiaodefa_source, "SETTINGS", {})
    missing = tmp_path / 'missing.duckdb'
    with pytest.raises((ValueError, duckdb.Error)):
        TushareHistoryCollector(missing, offline=True)
    assert not missing.exists()
    db = tmp_path / 'offline.duckdb'
    with TushareHistoryCollector(db, client=ScopedFixture()) as c:
        seed_calendar(c)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with TushareHistoryCollector(db, offline=True) as c:
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1"], plan_only=True)
        assert result["results"][0]["status"] == "planned"
        with pytest.raises(duckdb.Error):
            c.store.conn.execute('CREATE TABLE forbidden(i INT)')
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    empty = tmp_path / 'empty.duckdb'
    duckdb.connect(str(empty)).close()
    before = empty.read_bytes()
    with pytest.raises((ValueError, duckdb.Error)):
        TushareHistoryCollector(empty, offline=True)
    assert empty.read_bytes() == before


def test_reference_failure_cannot_return_success_or_empty_skip(tmp_path, monkeypatch):
    import json
    class Empty:
        rows = []
        delisted = []
        calls = []
        def query_rows(self, *args, **kwargs):
            self.calls.append((args[0],dict(args[1])))
            if args[0]=='daily':return [{'ts_code':'000001.SZ','trade_date':'20260701','close':10}]
            return self.delisted if args[1].get('list_status') == 'D' else self.rows
    with TushareHistoryCollector(tmp_path / "reference.duckdb", client=Empty()) as c:
        seed_calendar(c)
        result = c.run("20260701", "20260701", datasets=["stock_basic"])
        assert result["results"][0]["status"] == "error"
        c.client.rows = [dict(ts_code='000001.SZ', symbol='000001', list_date='19910403', list_status='L')]
        assert c.collect_stock_basic() == 1
        first = c._reference_version()
        assert first and c.collect_stock_basic() == 0
        c.store.conn.execute("UPDATE multi_source_observation SET observed_at=current_timestamp - INTERVAL 2 DAY "
                             "WHERE data_type='tushare_stock_basic_snapshot'")
        c.client.rows = []
        result = c.run('20260701', '20260701', datasets=['stock_basic', 'daily'], gap_only=True)
        assert all(r['status'] == 'error' for r in result['results'])
        daily=next(r for r in result['results'] if r['dataset']=='daily')
        assert daily['received_unverified_rows']==1 and daily['publication']=='raw_receipts_only_reference_unqualified'
        assert c.store.conn.execute('SELECT count(*) FROM tushare_daily').fetchone()[0]==0
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation WHERE data_type='tushare_daily'").fetchone()[0]==1
        assert c.store.conn.execute('SELECT count(*) FROM tushare_stock_basic').fetchone()[0] == 1
        c.client.rows = [dict(ts_code='000001.SZ', symbol='000001', list_date='19910403', list_status='L'),
                         dict(ts_code='000002.SZ', symbol='000002', list_date='20260701', list_status='L')]
        assert c.collect_stock_basic() == 2
        assert c._reference_version()['version'] != first['version']
        snapshots = c.store.conn.execute("SELECT payload_json FROM multi_source_observation "
            "WHERE data_type='tushare_stock_basic_snapshot' ORDER BY observed_at").fetchall()
        assert [len(json.loads(r[0])['rows']) for r in snapshots] == [1, 2]
        c.client.delisted = [dict(c.client.rows[0], list_status='D', delist_date='20260701')]
        c.client.rows = c.client.rows[1:]
        assert c.collect_stock_basic(force=True) == 2
        assert c._expected_stock_codes('20260701', 'daily') == {'000002.SZ'}
        # A manually changed projection no longer matches its reference version.
        c.store.conn.execute("UPDATE tushare_stock_basic SET list_date='1970-01-01' WHERE ts_code='000002.SZ'")
        assert c._reference_version() is None
        # Official repair is bounded, cached and retains unknown identities.
        import time
        from datetime import date, timedelta
        from trade_system.hithink_client import HiThinkClient
        calls = []
        def official(_self, code):
            calls.append(code)
            return {'timestamp': int(time.time()*1000), 'item': [{'thscode': code,
                'asset_type': 'a-share', 'list_date': None if code == '000003.SZ'
                else (date.today()+timedelta(days=1)).isoformat()}]}
        monkeypatch.setattr(HiThinkClient, '__init__', lambda *a, **k: None)
        monkeypatch.setattr(HiThinkClient, 'stock_listing', official)
        monkeypatch.setattr(c, '_is_production_source', lambda: True)
        monkeypatch.setattr(c, '_bse_listing_membership', lambda: None)
        def query_all(api, *, on_page, **params):
            rows = [dict(r) for r in (c.client.delisted if params.get('list_status') == 'D' else c.client.rows)]
            on_page(0, rows)
            return rows
        monkeypatch.setattr(c.client, 'query_all', query_all, raising=False)
        c.client.rows[0]['list_date'] = '19700101'
        assert c.collect_stock_basic(force=True) == 2
        assert c._expected_stock_codes(date.today().isoformat(), 'daily') == set()
        assert calls == ['000002.SZ']
        c.collect_stock_basic(force=True)
        assert calls == ['000002.SZ']
        c.client.rows.append(dict(ts_code='000003.SZ', list_status='L', list_date='19700101'))
        membership = {'as_of': date.today().isoformat(), 'recordcount': 2901,
                      'listings': {'000003': '1991-01-01'}, 'xlsx_sha256': 'fixture'}
        monkeypatch.setattr(c, '_exchange_listing_membership', lambda: membership)
        assert c.collect_stock_basic(force=True) == 3
        assert str(c.store.conn.execute("SELECT list_date FROM tushare_stock_basic WHERE ts_code='000003.SZ'").fetchone()[0]) == '1991-01-01'
        assert calls == ['000002.SZ', '000003.SZ']
        c.store.conn.execute("UPDATE multi_source_observation SET observed_at=current_timestamp-INTERVAL 20 MINUTE WHERE data_type='stock_listing_reference' AND asset_code='000003.SZ'")
        assert c.collect_stock_basic(force=True) == 3
        assert calls == ['000002.SZ', '000003.SZ', '000003.SZ']
        membership['listings'] = {'000001': '1991-01-01'}
        assert c.collect_stock_basic(force=True) == 3
        assert c.store.conn.execute("SELECT list_date FROM tushare_stock_basic WHERE ts_code='000003.SZ'").fetchone() == (None,)
        assert '000003.SZ' not in c._expected_stock_codes(date.today().isoformat(), 'daily')
        assert '000003.SZ' in c._expected_stock_codes((date.today()-timedelta(days=1)).isoformat(), 'daily')
        with pytest.raises(Exception, match='conflicts with official listing membership'):
            c._validate_stock_snapshot('daily', [{'ts_code': '000003.SZ'}], date.today().isoformat())
        c.store.conn.execute("UPDATE multi_source_observation SET payload_json=json_merge_patch(payload_json,?) "
            "WHERE data_type='tushare_stock_basic_snapshot' AND json_extract(payload_json,'$.listing_membership') IS NOT NULL",
            [json.dumps({'listing_membership': {'as_of': (date.today()-timedelta(days=1)).isoformat(), 'not_listed': ['000003.SZ']}})])
        assert c._reference_version() is None
        assert '000003.SZ' in c._expected_stock_codes(date.today().isoformat(), 'daily')

    # Exercise the exchange parser and cache without network or new test cases.
    import io
    import zipfile
    from trade_system import http_transport
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    header = '<row><c r="E1" t="s"><v>0</v></c><c r="G1" t="s"><v>1</v></c></row>'
    body = ''.join(f'<row><c r="E{i+2}" t="inlineStr"><is><t>{i:06}</t></is></c>'
                   f'<c r="G{i+2}" t="inlineStr"><is><t>1991-01-01</t></is></c></row>' for i in range(1000))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('xl/sharedStrings.xml', f'<sst xmlns="{ns}"><si><t>A股代码</t></si><si><t>A股上市日期</t></si></sst>')
        archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>{header}{body}</sheetData></worksheet>')
    tab = {'metadata': {'subname': date.today().isoformat(), 'recordcount': 1000, 'pageno': 1, 'pagesize': 1},
           'data': [{'agdm': '000000', 'agssrq': '1991-01-01'}]}
    reads = []
    def transport(request, **kwargs):
        reads.append(request.full_url)
        return json.dumps([tab]).encode() if '/data?' in request.full_url else buffer.getvalue()
    monkeypatch.setattr(http_transport, 'read_verified_once', transport)
    with TushareHistoryCollector(tmp_path / 'membership.duckdb', client=Empty()) as c:
        assert len(c._exchange_listing_membership()['listings']) == 1000
        assert len(c._exchange_listing_membership()['listings']) == 1000 and len(reads) == 2
        c.store.conn.execute("UPDATE multi_source_observation SET payload_hash='corrupted' WHERE data_type='szse_listing_membership'")
        tab['metadata']['recordcount'] = 1001
        with pytest.raises(Exception, match='incomplete or conflicting'):
            c._exchange_listing_membership()
        tab['metadata']['subname'] = (date.today()-timedelta(days=1)).isoformat()
        with pytest.raises(Exception, match='undated or incomplete'):
            c._exchange_listing_membership()


def test_full_basic_response_with_only_identity_is_not_complete(tmp_path):
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "unknown.duckdb", client=client) as c:
        seed_calendar(c)
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES ('000001.SZ')")
        c.store.conn.execute("INSERT INTO tushare_daily_basic(ts_code,stock_code,date,pe) "
                             "VALUES ('000001.SZ','000001','2026-07-01',10)")
        c._query_date_batch = lambda *a, **k: [{"ts_code":"000001.SZ","trade_date":"20260701"}]
        result = c.run("20260701", "20260701", datasets=["daily_basic"])
        assert result["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT status FROM history_fetch_checkpoint").fetchone()[0] == 'error'
        assert c.store.conn.execute("SELECT pe FROM tushare_daily_basic").fetchone()[0] == 10
        # A qualified partial response is useful evidence but cannot certify
        # missing suspended-stock valuation fields or manufacture zeros.
        import json
        import pytest
        from trade_system.xiaodefa_source import XiaodefaError
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES ('000002.SZ')")
        c._record_snapshot('suspend_d', dict(params={'trade_date':'20260701'}, rows=[
            dict(ts_code='000002.SZ', trade_date='20260701', suspend_type='S', suspend_timing='')]))
        c._is_production_source = lambda: True
        rows = [dict(ts_code='000001.SZ',trade_date='20260701',pe=12)]
        c._validate_stock_snapshot('daily_basic', rows, '20260701')
        c._query_date_batch = lambda *a, **k: rows
        with pytest.raises(XiaodefaError, match='lack qualified fields'):
            c._collect_daily_basic('20260701')
        assert c.store.conn.execute("SELECT pe FROM tushare_daily_basic").fetchall() == [(12,)]
        gap = json.loads(c.store.conn.execute("SELECT payload_json FROM multi_source_observation "
            "WHERE data_type='tushare_daily_basic_gaps_snapshot'").fetchone()[0])
        assert gap['full_day_suspended'] == ['000002.SZ']
        assert gap['field_status']['pe'] == 'unknown' and gap['acceptance'] == 'incomplete_not_certified'
        c.store.conn.execute("UPDATE multi_source_observation SET payload_hash='tampered' "
            "WHERE data_type='tushare_suspend_d_snapshot'")
        assert '000002.SZ' in c._expected_stock_codes('20260701', 'daily')

    from urllib.request import Request
    from trade_system.http_transport import _diagnostic_attempt, stop_diagnostic
    class AlternativeFixture:
        denied = False
        calls = []
        def query_rows(self, api, params=None, fields=''):
            _diagnostic_attempt(Request('https://t.xiaodefa.top/'))
            self.calls.append((api, params['ts_code']))
            if self.denied:
                stop_diagnostic('permission_denied')
                raise XiaodefaError('fixture denied')
            return [dict(params, total_share=100, float_share=50, pre_close=2,
                         pe=0, pb=-0.5, bvps=-4)]
    alternative = AlternativeFixture()
    with TushareHistoryCollector(tmp_path / 'alternatives.duckdb', client=alternative) as c:
        codes = [f'{i:06d}.SZ' for i in range(1, 13)]
        evidence = c._suspended_basic_evidence('20260701', codes)
        assert len(alternative.calls) == 2  # Relay products share one HTTP endpoint.
        assert evidence[codes[0]]['bak_basic']['values']['pe_dynamic'] is None
        assert evidence[codes[-1]]['bak_basic']['reason'] == 'request_budget_exhausted'
        again = c._suspended_basic_evidence('20260701', codes[:1])
        assert len(alternative.calls) == 2 and again[codes[0]]['bak_basic']['reused']
        assert again[codes[0]]['bak_basic']['received_at'] == evidence[codes[0]]['bak_basic']['received_at']
        alternative.denied = True
        stopped = c._suspended_basic_evidence('20260702', codes)
        assert len(alternative.calls) == 3
        assert stopped[codes[-1]]['bak_basic']['reason'] == 'permission_denied'


def test_stored_price_cannot_fill_an_unknown_calendar_day(tmp_path):
    import pytest
    from trade_system.xiaodefa_source import XiaodefaError
    class Unavailable:
        def query_rows(self, *a, **k):
            raise RuntimeError("calendar unavailable")
    with TushareHistoryCollector(tmp_path / "calendar.duckdb", client=Unavailable()) as c:
        c.store.conn.execute("INSERT INTO tushare_daily(ts_code,date,close) VALUES ('000001.SZ','2026-07-04',10)")
        with pytest.raises(XiaodefaError, match="trade_cal fetch failed"):
            c.ensure_calendar("20260704", "20260704")
