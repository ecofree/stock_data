from __future__ import annotations

import duckdb
import pytest

from trade_system.tushare_history import TushareHistoryCollector
from trade_system.xiaodefa_source import XiaodefaClient, XiaodefaError


class FakeClient:
    def query_rows(self, api_name, params=None, fields=""):
        assert api_name == "adj_factor"
        return [{"ts_code": "000001.SZ", "trade_date": "20260714", "adj_factor": 139.008}]


def test_large_repeated_valuation_batch_preserves_keys_clocks_and_rollback(tmp_path):
    from datetime import datetime
    from trade_system.tushare_store import bulk_replace
    with TushareHistoryCollector(tmp_path/'repeat.duckdb', client=FakeClient()) as h:
        con = h.store.conn
        columns = ['ts_code','date','pb','total_mv','circ_mv','fetched_at']
        stamp = datetime(2026,7,14,17,0)
        rows = [(f'{i:06d}.SZ','2026-07-14',None,100.,50.,stamp) for i in range(1,5601)]
        for values in (rows, [(r[0],r[1],-2.,*r[3:]) for r in rows]):
            con.execute('BEGIN')
            assert bulk_replace(con,'tushare_daily_basic',values,columns,['ts_code','date']) == 5600
            con.execute('COMMIT')
        assert con.execute('SELECT count(*),count(DISTINCT ts_code),min(pb),max(pb),'
                           'min(fetched_at),max(fetched_at) FROM tushare_daily_basic').fetchone() == (
                               5600,5600,-2.,-2.,stamp,stamp)
        con.execute('BEGIN')
        bulk_replace(con,'tushare_daily_basic',rows,columns,['ts_code','date'])
        con.execute('ROLLBACK')
        assert con.execute('SELECT count(*) FROM tushare_daily_basic WHERE pb=-2').fetchone()[0] == 5600
        con.execute('BEGIN')
        with pytest.raises(ValueError,match='duplicate keys'):
            bulk_replace(con,'tushare_daily_basic',[rows[0],rows[0]],columns,['ts_code','date'])
        con.execute('ROLLBACK')
        assert con.execute('SELECT count(*) FROM tushare_daily_basic WHERE pb=-2').fetchone()[0] == 5600


def test_partial_market_keeps_valid_facts_but_not_snapshot_success(tmp_path, monkeypatch):
    import json
    import trade_system.tushare_history as history
    class Partial:
        def query_rows(self, api, params=None, fields=''):
            assert api == 'daily_basic'
            code = params['ts_code']
            if code == '000001.SZ':
                return [dict(ts_code=code, trade_date='20260701', pb=-2, total_mv=100, circ_mv=50)]
            return [dict(ts_code='600999.SH', trade_date='20260701', pb=3, total_mv=100, circ_mv=50)]
    with TushareHistoryCollector(tmp_path/'partial.duckdb', client=Partial()) as c:
        seed_calendar(c)
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES ('000001.SZ'),('000002.SZ')")
        c.store.conn.execute("INSERT INTO tushare_daily_basic(ts_code,date,pb,total_mv,circ_mv) "
                             "VALUES ('000002.SZ','2026-07-01',9,100,50)")
        for _ in range(2):
            outcome = c.run('20260701','20260701',datasets=['daily_basic'],
                stock_codes=['000001.SZ','000002.SZ'], force=True, retry_passes=0)
            assert outcome['results'][0]['status'] == 'error'
            assert c.store.conn.execute('SELECT status FROM history_fetch_checkpoint').fetchone()[0] == 'error'
        assert c.store.conn.execute('SELECT ts_code,pb FROM tushare_daily_basic ORDER BY ts_code').fetchall() == [
            ('000001.SZ',-2),('000002.SZ',9)]
        assert c._product_counts['daily_basic']['rows_written'] == 2  # Two committed upserts, one unique key.
        quarantine = c.store.conn.execute("SELECT payload_json FROM multi_source_observation "
            "WHERE data_type='market_row_quarantine'").fetchall()
        assert all(json.loads(row[0])['rows'][0]['reason'] == 'unexpected_identity_or_session' for row in quarantine)
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily_basic WHERE ts_code='600999.SH'").fetchone()[0] == 0
        original = history.bulk_replace
        def failed_commit(con, table, *args):
            original(con, table, *args)
            raise RuntimeError('fixture failure before commit')
        monkeypatch.setattr(history, 'bulk_replace', failed_commit)
        with pytest.raises(RuntimeError,match='before commit'):
            c._collect_market('daily_basic','20260701',codes=['000001.SZ'],force=True)
        assert c._product_counts['daily_basic']['rows_written'] == 2
        assert c.store.conn.execute('SELECT pb FROM tushare_daily_basic WHERE ts_code=?',['000001.SZ']).fetchone()[0] == -2


def test_later_page_failure_retains_prior_valid_rows_without_certification(tmp_path, monkeypatch):
    class Pages(XiaodefaClient):
        def __init__(self):
            super().__init__(token='fixture')
        def query_all(self, api, *, on_page, **kwargs):
            assert api == 'daily'
            on_page(0,[dict(ts_code='000001.SZ',trade_date='20260701',close=10)])
            raise XiaodefaError('fixture later page failed')
    with TushareHistoryCollector(tmp_path/'page-partial.duckdb',client=Pages()) as c:
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES ('000001.SZ'),('000002.SZ')")
        monkeypatch.setattr(c,'_is_production_source',lambda: True)
        with pytest.raises(XiaodefaError,match='later page failed'):
            c._collect_daily('20260701')
        assert c.store.conn.execute('SELECT ts_code,close FROM tushare_daily').fetchall() == [('000001.SZ',10)]
        assert c._product_counts['daily']['rows_written'] == 1
        assert c.store.conn.execute("SELECT count(*) FROM close_snapshot_certification WHERE status='certified'").fetchone()[0] == 0


@pytest.mark.parametrize('dataset,field,value', [
    ('index_daily','close','nan'), ('index_daily','close',float('nan')),
    ('index_daily','vol','inf'), ('index_daily','vol',True),
    ('index_daily','vol','not_a_number'), ('adj_factor','adj_factor','nan'),
])
def test_invalid_market_numbers_still_quarantined(tmp_path, dataset, field, value):
    class Invalid:
        def query_rows(self, api, params=None, fields=''):
            return [dict(ts_code=params['ts_code'], trade_date='20260701',
                         close=10, adj_factor=1, **{field:value})] if field not in {'close','adj_factor'} else [
                dict(ts_code=params['ts_code'],trade_date='20260701',**{field:value})]
    code = '000001.SH' if dataset == 'index_daily' else '000001.SZ'
    with TushareHistoryCollector(tmp_path/'invalid-number.duckdb',client=Invalid()) as c:
        with pytest.raises(XiaodefaError,match='source rows quarantined'):
            c._collect_market(dataset,'20260701',codes=[code])
        assert c.store.conn.execute('SELECT count(*) FROM tushare_'+dataset).fetchone()[0] == 0
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation "
            "WHERE data_type='market_row_quarantine'").fetchone()[0] == 1


def test_unknown_valuation_core_fields_preserve_valid_facts_without_qualification(tmp_path):
    import json
    class MissingPB:
        def query_rows(self, api, params=None, fields=''):
            return [dict(ts_code=params['ts_code'],trade_date='20260701',
                         pb='nan',pe=12,total_mv=100,circ_mv=50)]
    with TushareHistoryCollector(tmp_path/'unknown-pb.duckdb',client=MissingPB()) as c:
        with pytest.raises(XiaodefaError,match='coverage incomplete'):
            c._collect_market('daily_basic','20260701',codes=['000001.SZ'])
        assert c.store.conn.execute('SELECT pb,pe,total_mv,circ_mv FROM tushare_daily_basic').fetchone() == (None,12,100,50)
        assert c._covered_codes('daily_basic','20260701') == set()
        assert c.store.conn.execute('SELECT status FROM close_snapshot_certification').fetchone()[0] == 'error'
        raw = json.loads(c.store.conn.execute("SELECT payload_json FROM multi_source_observation "
            "WHERE data_type='tushare_daily_basic'").fetchone()[0])
        assert raw['rows'][0]['pb'] == 'nan'
        assert c._product_counts['daily_basic']['rows_written'] == 1


def test_pdf_extraction_uses_exact_physical_page_and_bounded_range(tmp_path):
    import hashlib
    from copy import deepcopy
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    from trade_system.tushare_history import _validate_valuation_document
    path = tmp_path/'pages.pdf'
    writer = PdfWriter()
    for text in ('other row 123.00','parent equity -6000000.00'):
        page = writer.add_blank_page(width=200,height=200)
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),
            NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 10 100 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)
    source = dict(ts_code='000001.SZ',as_of='2026-07-01',document=dict(
        path=str(path),url='https://static.cninfo.com.cn/pages.PDF',format='pdf',
        sha256=hashlib.sha256(path.read_bytes()).hexdigest()),
        fields={'equity':dict(value=-6000000,page=1,excerpt='parent equity -6000000')})
    with pytest.raises(ValueError,match='absent from cited'):
        _validate_valuation_document(source)
    source['fields']['equity']['page'] = 2
    _validate_valuation_document(source)
    ranged = deepcopy(source)
    ranged['fields']['equity'].update(page=1,page_range=[1,2])
    _validate_valuation_document(ranged)
    ranged['fields']['equity']['page_range'] = [1,5]
    with pytest.raises(ValueError,match='bounded page range'):
        _validate_valuation_document(ranged)
    source['fields']['equity']['value'] = -123
    with pytest.raises(ValueError,match='absent from cited'):
        _validate_valuation_document(source)


def test_static_and_ttm_profit_periods_and_allocations_remain_independent():
    from copy import deepcopy
    from trade_system.tushare_history import derive_earnings_valuation
    code, day = '000001.SZ','2026-09-29'
    def period(year,end,profit,allocation):
        return dict(period_start=f'{year}-01-01',period_end=f'{year}-{end}',
                    parent_profit=profit,other_equity_profit_distribution=allocation)
    base = dict(qualified=True,ts_code=code,trade_date=day,definition='ordinary_shareholder_profit_v1')
    earnings = {'static':dict(base,periods=[period(2025,'12-31',100000,10000)]),
        'ttm':dict(base,selected_period='2026-06-30',periods=[period(2025,'12-31',100000,10000),
            period(2026,'06-30',70000,7000),period(2025,'06-30',40000,4000)])}
    calculate = lambda p: derive_earnings_valuation(100,p,ts_code=code,trade_date=day)
    result = calculate(earnings)
    assert result['values']['pe'] == pytest.approx(1000000/90000)
    assert result['values']['pe_ttm'] == pytest.approx(1000000/117000)
    changed = deepcopy(earnings)
    changed['ttm']['periods'][2]['period_end']='2025-03-31'
    assert 'pe_ttm' not in calculate(changed)['values'] and 'pe' in calculate(changed)['values']
    changed = deepcopy(earnings)
    changed['static']['periods'][0].pop('other_equity_profit_distribution')
    assert calculate(changed)['field_status']['pe'] == 'unknown'
    changed = deepcopy(earnings)
    changed['static']['periods'][0]['parent_profit'] = -100
    assert calculate(changed)['field_status']['pe'] == 'not_applicable_nonpositive_ordinary_profit'
    assert 'pe' not in calculate(changed)['values']
    changed = deepcopy(earnings)
    changed['ttm']['qualified'] = False
    assert calculate(changed)['field_status']['pe_ttm'] == 'unknown'


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
        collector.store.conn.execute("UPDATE tushare_moneyflow_industry SET fetched_at='2026-07-14 16:00:00'")
        collector.sync_sector_flow("20260714")
        assert str(collector.store.conn.execute("SELECT min(fetched_at) FROM multi_source_sector_flow").fetchone()[0]) == '2026-07-14 16:00:00'
        assert collector.store.conn.execute(
            "SELECT main_net,super_net,large_net,mid_net,small_net FROM multi_source_sector_flow"
        ).fetchone() == (30, 10, -3, None, None)
        collector._collect_moneyflow("20260714")
        collector.store.conn.execute("UPDATE tushare_moneyflow SET fetched_at='2026-07-14 16:05:00'")
        collector.sync_stock_flow("20260714")
        assert str(collector.store.conn.execute("SELECT min(fetched_at) FROM multi_source_stock_flow").fetchone()[0]) == '2026-07-14 16:05:00'
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
    from datetime import date

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
        monkeypatch.setattr(c, '_exchange_listing_membership', lambda exchange='SZ': {
            'as_of': date.today().isoformat(), 'listings': {'000001': '1991-04-03'}})
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
        with pytest.raises(XiaodefaError, match="1 invalid or ambiguous source rows quarantined"):
            c._collect_daily("20260714")
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 999
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily WHERE ts_code='600999.SH'").fetchone()[0] == 0
        assert c.store.conn.execute('SELECT status FROM close_snapshot_certification').fetchone()[0] == 'error'
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
        # A valid historical listing date is not current membership proof.
        c.client.rows.append(dict(ts_code='000004.SZ',list_status='L',list_date='19950101'))
        assert c.collect_stock_basic(force=True) == 4
        assert str(c.store.conn.execute("SELECT list_date FROM tushare_stock_basic WHERE ts_code='000004.SZ'").fetchone()[0]) == '1995-01-01'
        assert '000004.SZ' not in c._expected_stock_codes(date.today().isoformat(), 'daily')
        assert '000004.SZ' in c._expected_stock_codes((date.today()-timedelta(days=1)).isoformat(), 'daily')
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
    from copy import deepcopy
    from trade_system.tushare_history import derive_valuation
    code = '000001.SZ'
    inputs = {}
    for field, value, unit, semantic in (
            ('price', 10, 'yuan', 'official_close'),
            ('total_share', 100, '10000_shares', 'total_shares'),
            ('float_share', 50, '10000_shares', 'circulating_shares'),
            ('equity', 6000000, 'yuan', 'parent_equity'),
            ('other_equity', 1000000, 'yuan', 'other_equity_tools')):
        inputs[field] = dict(value=value, unit=unit, semantic=semantic, ts_code=code,
            source_date='2026-07-01', valid_through='2026-07-01',
            available_at='2026-07-01T15:00:00+08:00', received_at='2026-07-01T16:00:00+08:00',
            receipt_sha256='a'*64)
    def calculate(values):
        return derive_valuation(code, '20260701', values, observed_at='2026-07-01T17:00:00+08:00')
    result = calculate(inputs)
    assert result['values'] == dict(total_mv=1000, circ_mv=500, pb=2)
    assert result['status'] == 'derived_core_fields' and not result['certifies_daily_basic']
    assert result['field_status']['pe'] == 'unknown'
    changed = deepcopy(inputs)
    changed['equity']['value'] = 1000000
    assert calculate(changed)['field_status']['pb'] == 'undefined_zero_adjusted_equity'
    changed['equity']['value'] = -4000000
    assert calculate(changed)['values']['pb'] == -2
    assert calculate(changed)['field_status']['pb'] == 'negative_adjusted_equity'
    changed['other_equity']['value'] = None
    assert 'pb' not in calculate(changed)['values']
    changed = deepcopy(inputs)
    changed['equity']['received_at'] = '2026-07-01T18:00:00+08:00'
    assert calculate(changed)['status'] == 'incomplete'
    changed = deepcopy(inputs)
    changed['float_share']['value'] = 101
    assert 'circ_mv' not in calculate(changed)['values']
    changed = deepcopy(inputs)
    changed['price']['semantic'] = 'suspension_reference'
    changed['price']['price_date'] = '2026-06-30'
    assert 'total_mv' not in calculate(changed)['values']
    for field, status in [('suspension','full_day_suspended'), ('corporate_actions','reference_price_applicable')]:
        changed[field] = dict(ts_code=code, trade_date='2026-07-01', status=status,
                              receipt_sha256='b'*64, received_at='2026-07-01T15:01:00+08:00')
    assert calculate(changed)['values']['total_mv'] == 1000
    assert calculate(changed)['price_date'] == '2026-06-30'
    changed['equity']['valid_through'] = '2026-06-30'
    assert calculate(changed)['status'] == 'incomplete'
    changed = {k: deepcopy(inputs[k]) for k in ('equity', 'other_equity')}
    for field, value, semantic in [('total_mv',1000,'total_market_value'), ('circ_mv',500,'circulating_market_value')]:
        changed[field] = dict(inputs['price'], value=value, unit='10000_yuan', semantic=semantic)
    assert calculate(changed)['values']['pb'] == 2  # Native market value needs no duplicate price query.
    changed['other_equity']['receipt_sha256'] = 'b'*64
    assert 'pb' not in calculate(changed)['values']
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
        # Even a forced targeted refresh cannot certify PE-only input. A real
        # same-day complete valuation may retain unknown loss-making PE.
        c._read_rows = lambda *a, **k: [dict(ts_code='000002.SZ',trade_date='20260701',pe=12)]
        with pytest.raises(XiaodefaError, match='coverage incomplete'):
            c._collect_market('daily_basic','20260701',codes=['000002.SZ'],force=True)
        c._read_rows = lambda *a, **k: [dict(ts_code='000002.SZ',trade_date='20260701',
                                           pe=None,pb=-0.5,total_mv=100,circ_mv=50)]
        assert c._collect_market('daily_basic','20260701',codes=['000002.SZ'],force=True) == 1
        assert c.store.conn.execute("SELECT pe FROM tushare_daily_basic WHERE ts_code='000002.SZ'").fetchone() == (None,)
        # Completion uses raw receipt values, never a caller's same-name field
        # or a recomputation timestamp; raw daily_basic NULL remains untouched.
        import hashlib
        def receipt(api, rows):
            raw = json.dumps(dict(api=api, params={'trade_date':'20260701'}, rows=rows))
            c.store.conn.execute("INSERT INTO multi_source_observation(data_type,provider,payload_json,payload_hash,observed_at) "
                "VALUES (?,'custom',?,?,TIMESTAMP '2026-07-01 16:00:00')",
                ['tushare_'+api, raw, hashlib.sha256(raw.encode()).hexdigest()])
        receipt('daily_basic', [dict(ts_code='000001.SZ',trade_date='20260701',pb=None,total_mv=1000,circ_mv=500)])
        receipt('balancesheet', [dict(ts_code='000001.SZ',report_type='1',end_date='20260331',
            f_ann_date='20260430',total_hldr_eqy_exc_min_int=6000000,oth_eqt_tools=1000000)])
        report = c.valuation_completion_report('20260701',['000001.SZ'],observed_at='2026-07-01T17:00:00+08:00')
        assert report['rows']['000001.SZ']['values']['pb'] == 2
        assert report['rows']['000001.SZ']['native_pb_status'] == 'provider_null'
        assert report['rows']['000001.SZ']['input_received_at_min'] == '2026-07-01T16:00:00+08:00'
        assert report['market_requests'] == 0 and not report['certifies_daily_basic']
        assert c.store.conn.execute("SELECT pb FROM tushare_daily_basic WHERE ts_code='000001.SZ'").fetchone() == (None,)
        c.store.conn.execute("UPDATE multi_source_observation SET payload_hash='tampered' WHERE data_type='tushare_balancesheet'")
        report = c.valuation_completion_report('20260701',['000001.SZ'],observed_at='2026-07-01T17:00:00+08:00')
        assert 'pb' not in report['rows']['000001.SZ']['values']

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
            if api == 'balancesheet':
                assert params['report_type'] == '1'
                return [dict(ts_code=params['ts_code'],report_type='1',end_date='20260331',
                    f_ann_date='20260430',total_hldr_eqy_exc_min_int=6000000,oth_eqt_tools=1000000)]
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
        alternative.denied = False
        financial = c.collect_valuation_inputs('20260701', codes)
        assert financial['transport_budget']['attempts'] == 2
        assert len(alternative.calls) == 5
        inventories = [json.loads(r[0]) for r in c.store.conn.execute(
            "SELECT payload_json FROM multi_source_observation "
            "WHERE data_type='tushare_balancesheet_inventory_snapshot'").fetchall()]
        assert len(inventories) == 2
        assert all(v['scope'] == 'latest_consolidated_in_requested_interval'
                   and v['requested_report_types'] == ['1']
                   and v['complete_revision_inventory'] is False for v in inventories)
        reused = c.collect_valuation_inputs('20260701', codes[:2])
        assert reused['transport_budget']['attempts'] == 0 and len(alternative.calls) == 5
        assert all(r['status']=='retained_statement_reused' for r in reused['requests'].values())
        alternative.denied = True
        denied = c.collect_valuation_inputs('20260701', codes[2:])
        assert denied['transport_budget']['attempts'] == 1
        assert denied['transport_budget']['stopped'] == 'permission_denied'
        assert len(alternative.calls) == 6
        # An old successful default-type request mislabeled as all report types
        # cannot certify completeness or qualify for the new explicit cache.
        import hashlib
        for raw, in c.store.conn.execute("SELECT payload_json FROM multi_source_observation "
                "WHERE data_type='tushare_balancesheet_inventory_snapshot'").fetchall():
            old = json.loads(raw)
            old['params'].pop('report_type')
            old['scope'] = 'all_report_types_in_requested_interval'
            encoded = json.dumps(old)
            c.store.conn.execute("UPDATE multi_source_observation SET payload_json=?,payload_hash=? "
                "WHERE payload_json=?", [encoded,hashlib.sha256(encoded.encode()).hexdigest(),raw])
        refused = c.collect_valuation_inputs('20260701', codes[:2])
        assert refused['transport_budget']['attempts'] == 1
        assert refused['transport_budget']['stopped'] == 'permission_denied'


def test_reviewed_valuation_intake_revalidates_receipts_and_keeps_raw_null(tmp_path):
    import hashlib
    import json
    from copy import deepcopy
    import pytest
    code = '000001.SZ'
    with TushareHistoryCollector(tmp_path/'review.duckdb', client=ScopedFixture()) as c:
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES (?)", [code])
        c.store.conn.execute("INSERT INTO tushare_daily_basic(ts_code,date,pb,total_mv,circ_mv) "
                             "VALUES (?,'2026-07-01',NULL,1000,500)", [code])
        def retain(payload):
            raw = json.dumps(payload)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            c.store.conn.execute("INSERT INTO multi_source_observation(data_type,provider,payload_json,payload_hash,observed_at) "
                "VALUES ('test_source','custom',?,?,TIMESTAMP '2026-07-01 16:00:00')", [raw,digest])
            return digest
        statement = retain(dict(rows=[dict(ts_code=code,report_type='1',end_date='20260331',ann_date='20260430',
                                           total_hldr_eqy_exc_min_int=6000000,oth_eqt_tools=1000000)]))
        market = retain(dict(rows=[dict(ts_code=code,trade_date='20260701',total_mv=1000,circ_mv=500)]))
        inventory = retain(dict(ts_code=code,as_of='2026-07-01',statements=[statement],review_scope='fixture only'))
        review = dict(schema='reviewed_valuation_inputs_v1',ts_code=code,trade_date='2026-07-01',
            reviewed_by='fixture-reviewer',reviewed_at='2026-07-01T16:30:00+08:00',
            source_receipts=[statement,market,inventory],
            financial_inventory=dict(as_of='2026-07-01',scope='all_published_consolidated_revisions',
                document_receipts=[inventory],statement_receipts=[statement],selected_statement_receipt=statement,
                selection_reason='fixture complete original and revision inventory'),inputs={})
        for field, native, value, semantic, unit, digest in (
                ('total_mv','total_mv',1000,'total_market_value','10000_yuan',market),
                ('circ_mv','circ_mv',500,'circulating_market_value','10000_yuan',market),
                ('equity','total_hldr_eqy_exc_min_int',6000000,'parent_equity','yuan',statement),
                ('other_equity','oth_eqt_tools',1000000,'other_equity_tools','yuan',statement)):
            review['inputs'][field] = dict(ts_code=code,value=value,unit=unit,semantic=semantic,
                source_date='2026-07-01',valid_through='2026-07-01',available_at='2026-07-01T15:00:00+08:00',
                received_at='2099-01-01T00:00:00+08:00',receipt_sha256=digest,value_path=['rows',0,native])
            if field in {'equity','other_equity'}:
                review['inputs'][field]['source_date'] = '2026-04-30'
        path = tmp_path/'review.json'
        def write(value):
            path.write_text(json.dumps(dict(reviews=[value])),encoding='utf-8')
            return hashlib.sha256(path.read_bytes()).hexdigest()
        before = len(c.client.calls)
        sha = write(review)
        with pytest.raises(ValueError,match='hash mismatch'):
            c.import_valuation_reviews(path,'0'*64,'20260701')
        imported = c.import_valuation_reviews(path,sha,'20260701')
        assert imported['rows'][0]['values']['pb'] == 2
        assert imported['rows'][0]['input_received_at_max'] == '2026-07-01T16:00:00+08:00'
        assert c._covered_codes('daily_basic','20260701') == {code}
        assert c.store.conn.execute('SELECT pb FROM tushare_daily_basic').fetchone() == (None,)
        report = c.valuation_completion_report('20260701',[code])
        assert report['rows'][code]['valuation_eligible']
        assert report['rows'][code]['native_pb_status'] == 'row_absent'  # Raw receipt not manufactured by intake.
        assert not report['certifies_daily_basic']
        suspended = deepcopy(review)
        suspended['inputs'].pop('total_mv')
        suspended['inputs'].pop('circ_mv')
        quote = retain({'rows':[dict(ts_code=code,trade_date='20260630',close=10)]})
        shares = retain({'rows':[dict(ts_code=code,trade_date='20260701',total_share=100,float_share=50)]})
        c._record_snapshot('suspend_d',{'params':{'trade_date':'20260701'},'rows':[
            dict(ts_code=code,trade_date='20260701',suspend_type='S',suspend_timing='')]})
        c.store.conn.execute("UPDATE multi_source_observation SET observed_at=TIMESTAMP '2026-07-01 16:00:00' "
                             "WHERE data_type='tushare_suspend_d_snapshot'")
        proof = c.store.conn.execute("SELECT payload_hash FROM multi_source_observation WHERE data_type='tushare_suspend_d_snapshot'").fetchone()[0]
        action = retain(dict(ts_code=code,as_of='2026-07-01',events=[],scope='fixture complete corporate actions'))
        suspended['source_receipts'] += [quote,shares,proof,action]
        for field, native, value, semantic, unit, digest in (
                ('price','close',10,'suspension_reference','yuan',quote),
                ('total_share','total_share',100,'total_shares','10000_shares',shares),
                ('float_share','float_share',50,'circulating_shares','10000_shares',shares)):
            suspended['inputs'][field] = dict(ts_code=code,value=value,unit=unit,semantic=semantic,
                source_date='2026-07-01',valid_through='2026-07-01',available_at='2026-07-01T15:00:00+08:00',
                receipt_sha256=digest,value_path=['rows',0,native],price_date='2026-06-30')
        for field, digest, status in [('suspension',proof,'full_day_suspended'),('corporate_actions',action,'reference_price_applicable')]:
            suspended['inputs'][field] = dict(ts_code=code,trade_date='2026-07-01',receipt_sha256=digest,
                                              status=status,review_basis='fixture documented scope and policy')
        result = c._valuation_review(suspended,'20260701','2026-07-01T17:00:00+08:00')
        assert result['valuation_eligible'] and result['values']['pb'] == 2
        assert result['price_date'] == '2026-06-30'
        no_actions = deepcopy(suspended)
        no_actions['inputs'].pop('corporate_actions')
        assert not c._valuation_review(no_actions,'20260701','2026-07-01T17:00:00+08:00')['valuation_eligible']
        bypass = deepcopy(no_actions)
        zero_quote = retain({'rows':[dict(ts_code=code,trade_date='20260701',close=10,vol=0)]})
        bypass['source_receipts'].append(zero_quote)
        bypass['inputs']['price'].update(semantic='official_close',receipt_sha256=zero_quote)
        with pytest.raises(ValueError,match='requires explicit reference price policy'):
            c._valuation_review(bypass,'20260701','2026-07-01T17:00:00+08:00')
        c.store.conn.execute("INSERT INTO tushare_daily(ts_code,date,close,volume) VALUES (?,'2026-07-01',10,100)",[code])
        with pytest.raises(ValueError,match='conflicts with retained trading'):
            c._valuation_review(suspended,'20260701','2026-07-01T17:00:00+08:00')
        c.store.conn.execute('DELETE FROM tushare_daily WHERE ts_code=?',[code])
        # Whole-universe certification consumes the separate derived result;
        # the missing raw provider row is not relabelled or filled.
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) "
            "SELECT lpad(CAST(i AS VARCHAR),6,'0') || '.SZ' FROM range(2,1001) t(i)")
        c.store.conn.execute("INSERT INTO tushare_daily_basic(ts_code,date,pb,total_mv,circ_mv) "
            "SELECT ts_code,DATE '2026-07-01',1,100,50 FROM tushare_stock_basic WHERE ts_code<>?", [code])
        c._certify_close_snapshot('daily_basic','20260701',status='certified')
        assert c.store.conn.execute("SELECT distinct_codes,invalid_rows,status FROM close_snapshot_certification").fetchone() == (1000,0,'certified')
        c.import_valuation_reviews(path,sha,'20260701')
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation WHERE data_type='valuation_review'").fetchone()[0] == 1
        altered = deepcopy(review)
        altered['inputs']['equity']['value'] = 1
        with pytest.raises(ValueError,match='does not match'):
            c.import_valuation_reviews(path,write(altered),'20260701')
        altered = deepcopy(review)
        altered['financial_inventory']['document_receipts'] = []
        with pytest.raises(ValueError,match='revision inventory'):
            c.import_valuation_reviews(path,write(altered),'20260701')
        c.store.conn.execute("UPDATE multi_source_observation SET payload_json='{}' WHERE payload_hash=?",[statement])
        assert code not in c._covered_codes('daily_basic','20260701')
        c._certify_close_snapshot('daily_basic','20260701',status='certified')
        assert c.store.conn.execute("SELECT invalid_rows,status FROM close_snapshot_certification").fetchone() == (1,'incomplete')
        assert len(c.client.calls) == before


def test_official_absence_review_requires_dated_inventory_and_revalidates_file(tmp_path):
    import hashlib
    import json
    from copy import deepcopy
    from pathlib import Path
    import pytest
    from trade_system.tushare_history import _json
    code, day = '000001.SZ','2026-07-01'
    document_path = tmp_path/'official-fixture.html'
    document_path.write_text('fixture original consolidated equity -6000000 and instrument disclosure',encoding='utf-8')
    with TushareHistoryCollector(tmp_path/'official-review.duckdb', client=ScopedFixture()) as c:
        def receipt(payload):
            encoded = _json(payload)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            c.store.conn.execute("INSERT INTO multi_source_observation(data_type,provider,payload_json,payload_hash,observed_at) "
                "VALUES ('test_source','custom',?,?,TIMESTAMP '2026-07-01 16:00:00')",[encoded,digest])
            return digest
        source = dict(schema='official_valuation_document_v1',ts_code=code,as_of=day,
            received_at='2026-07-01T16:00:00+08:00',
            document=dict(url='https://static.cninfo.com.cn/fixture.html',path=str(document_path),format='html',
                          sha256=hashlib.sha256(document_path.read_bytes()).hexdigest()),
            statement=dict(period='20260331',announcement_date='20260430',basis='consolidated'),
            fields=dict(equity=dict(value=-6000000,page=1,excerpt='fixture parent equity -6000000'),
                        other_equity=dict(value=None,page=1,excerpt='fixture blank instrument cell')),
            other_equity_absence_review=dict(no_instrument_disclosure='fixture explicit no instruments note',
                equity_changes_review='fixture complete instruments and equity changes',
                equity_components=[dict(value=-6000000,page=1,excerpt='fixture full equity breakdown')]))
        digest = hashlib.sha256(_json(source).encode()).hexdigest()
        catalogue_pages = []
        for number in (1,2):
            page_path = tmp_path/f'catalogue-page-{number}.json'
            body = dict(totalAnnouncement=2,totalpages=1,hasMore=number == 1,
                announcements=[dict(secCode='000001',orgId='fixture-org',announcementId=str(number))])
            page_path.write_text(_json(body),encoding='utf-8')
            catalogue_pages.append(dict(path=str(page_path),sha256=hashlib.sha256(page_path.read_bytes()).hexdigest(),
                http_status=200,params=dict(stock='000001,fixture-org',tabName='fulltext',pageNum=str(number),
                    seDate='2026-03-31~'+day)))
        catalogue_path = tmp_path/'catalogue-manifest.json'
        catalogue_path.write_text(_json(dict(schema='official_disclosure_page_set_v1',ts_code=code,
            source_pages=catalogue_pages)),encoding='utf-8')
        catalogue = dict(schema='official_valuation_document_v1',ts_code=code,as_of=day,
            received_at='2026-07-01T16:05:00+08:00',kind='disclosure_inventory',window_from='2026-03-31',
            window_through=day,catalogue_complete=True,
            document=dict(url='https://www.cninfo.com.cn/new/hisAnnouncement/query',path=str(catalogue_path),
                format='json',sha256=hashlib.sha256(catalogue_path.read_bytes()).hexdigest()))
        catalogue_digest = hashlib.sha256(_json(catalogue).encode()).hexdigest()
        market = receipt(dict(rows=[dict(ts_code=code,trade_date='20260701',total_mv=1000,circ_mv=500)]))
        review = dict(schema='reviewed_valuation_inputs_v2',ts_code=code,trade_date=day,
            reviewed_by='fixture_review_not_real_financial_evidence',reviewed_at='2026-07-01T16:30:00+08:00',
            source_receipts=[digest,catalogue_digest,market],financial_inventory=dict(as_of=day,
                scope='latest_applicable_consolidated_statement_and_corrections',selected_period='20260331',
                document_receipts=[catalogue_digest],statement_receipts=[digest],selected_statement_receipt=digest,
                selection_reason='fixture dated report and correction list'),inputs={})
        for f,v,semantic,unit in [('equity',-6000000,'parent_equity','yuan'),('other_equity',0,'other_equity_tools','yuan'),
                                 ('total_mv',1000,'total_market_value','10000_yuan'),('circ_mv',500,'circulating_market_value','10000_yuan')]:
            review['inputs'][f] = dict(ts_code=code,value=v,semantic=semantic,unit=unit,
                source_date='2026-04-30' if f in {'equity','other_equity'} else day, valid_through=day,
                available_at='2026-07-01T15:00:00+08:00',receipt_sha256=digest if f in {'equity','other_equity'} else market,
                value_path=['rows',0,f])
        review['inputs']['other_equity']['evidence_kind']='evidenced_absence_zero'
        path = tmp_path/'review-bundle.json'
        def write(r, docs):
            path.write_text(json.dumps(dict(reviews=[r],reviewed_source_documents=docs)),encoding='utf-8')
            return hashlib.sha256(path.read_bytes()).hexdigest()
        result = c.import_valuation_reviews(path,write(review,[source,catalogue]),day)['rows'][0]
        assert result['valuation_eligible'] and result['values']['pb'] < 0
        assert result['input_received_at_min'] == '2026-07-01T16:00:00+08:00'
        assert result['input_received_at_max'] == catalogue['received_at']
        assert not result['value_screen_eligible'] and result['valuation_risk'] == 'negative_equity'
        assert result['field_status']['pe'] == 'unknown' and result['field_status']['pe_ttm'] == 'unknown'
        # A bare qualified assertion cannot make equity into annual earnings.
        forged = deepcopy(review)
        forged['earnings_reviews'] = {'static': dict(qualified=True,
            definition='ordinary_shareholder_profit_v1',periods=[dict(period_start='2025-01-01',
                period_end='2025-12-31',parent_profit=6000000,other_equity_profit_distribution=0)])}
        forged_result = c._valuation_review(forged,day,'2026-07-02T17:00:00+08:00')
        assert forged_result['valuation_eligible'] and forged_result['field_status']['pe'] == 'unknown'
        calls_before = len(c.client.calls)
        worklist = c.prepare_valuation_reviews('2026-07-02',[code,'002731.SZ'],
                                               observed_at='2026-07-02T17:00:00+08:00')
        assert worklist['market_requests'] == worklist['business_rows_written'] == 0
        assert not worklist['permits_requests'] and not worklist['certifies_daily_basic']
        assert not worklist['rows'][code]['qualified_core']
        reusable = worklist['rows'][code]['reusable_financial_evidence']
        assert reusable['previous_session'] == day
        assert reusable['reusable_financial_inputs']['equity']['receipt_sha256'] == digest
        assert reusable['input_received_at_min'] == '2026-07-01T16:00:00+08:00'
        assert worklist['rows']['002731.SZ']['status'] == 'awaiting_inputs_and_review'
        assert len(c.client.calls) == calls_before
        original = c.store.conn.execute('SELECT payload_json FROM multi_source_observation WHERE payload_hash=?',[digest]).fetchone()[0]
        assert json.loads(original)['fields']['other_equity']['value'] is None
        altered = deepcopy(review)
        altered['inputs']['other_equity'].pop('evidence_kind')
        with pytest.raises(ValueError,match='does not match'):
            c.import_valuation_reviews(path,write(altered,[source,catalogue]),day)
        incomplete = deepcopy(catalogue)
        incomplete['catalogue_complete']=False
        altered = deepcopy(review)
        altered['financial_inventory']['document_receipts']=[digest]
        with pytest.raises(ValueError,match='correction catalogue'):
            c.import_valuation_reviews(path,write(altered,[source,catalogue]),day)
        # A totalpages value of one did not hide the second page. Conversely,
        # editing any original page revokes the already imported qualification.
        first_page = Path(catalogue_pages[0]['path'])
        original_page = first_page.read_bytes()
        first_page.write_text('{}',encoding='utf-8')
        assert c.qualified_valuation_reviews(day) == {}
        first_page.write_bytes(original_page)
        assert code in c.qualified_valuation_reviews(day)
        first_page.unlink()
        assert c.qualified_valuation_reviews(day) == {}
        first_page.write_bytes(original_page)
        from trade_system.tushare_history import _validate_valuation_document
        manifest_bytes = catalogue_path.read_bytes()
        second_page = Path(catalogue_pages[1]['path'])
        second_bytes = second_page.read_bytes()
        for change, message in [('missing_page','pagination'),('duplicate_id','duplicate'),
                                ('wrong_security','identity'),('filtered_query','scope')]:
            manifest = json.loads(manifest_bytes)
            if change == 'missing_page':
                manifest['source_pages'].pop()
            elif change == 'filtered_query':
                manifest['source_pages'][0]['params']['category']='half_year_only'
            else:
                page = json.loads(second_bytes)
                page['announcements'][0]['announcementId' if change == 'duplicate_id' else 'secCode'] = (
                    '1' if change == 'duplicate_id' else '000002')
                second_page.write_text(_json(page),encoding='utf-8')
                manifest['source_pages'][1]['sha256']=hashlib.sha256(second_page.read_bytes()).hexdigest()
            catalogue_path.write_text(_json(manifest),encoding='utf-8')
            invalid = deepcopy(catalogue)
            invalid['document']['sha256']=hashlib.sha256(catalogue_path.read_bytes()).hexdigest()
            with pytest.raises(ValueError,match=message):
                _validate_valuation_document(invalid)
            second_page.write_bytes(second_bytes)
        catalogue_path.write_bytes(manifest_bytes)
        invalid = deepcopy(catalogue)
        invalid['document']['url']='https://www.cninfo.com.cn/unrelated'
        with pytest.raises(ValueError,match='origin'):
            c.import_valuation_reviews(path,write(review,[source,invalid]),day)
        # Related action originals participate in integrity and arrival checks,
        # even when the dated catalogue pages themselves remain unchanged.
        action_path = tmp_path/'action.pdf'
        action_bytes = b'%PDF-fixture corporate action original'
        action_path.write_bytes(action_bytes)
        action = deepcopy(catalogue)
        action['coverage']='all_price_and_share_affecting_actions'
        action['related_original_document_receipts']=[dict(code=code,
            url='https://static.cninfo.com.cn/action.pdf',path=str(action_path),
            sha256=hashlib.sha256(action_bytes).hexdigest(),announcement=dict(secCode='000001'),
            received_at=action['received_at'])]
        _validate_valuation_document(action)
        action_digest = hashlib.sha256(_json(action).encode()).hexdigest()
        action_review = deepcopy(review)
        action_review['source_receipts']=[action_digest if h == catalogue_digest else h
                                         for h in review['source_receipts']]
        action_review['financial_inventory']['document_receipts']=[action_digest]
        c.import_valuation_reviews(path,write(action_review,[source,action]),day)
        action_path.write_bytes(b'%PDF-changed corporate action')
        assert c.qualified_valuation_reviews(day) == {}
        action_path.write_bytes(action_bytes)
        assert code in c.qualified_valuation_reviews(day)
        for change in ('late_arrival','missing_arrival','missing_original','wrong_security'):
            invalid = deepcopy(action)
            original = invalid['related_original_document_receipts'][0]
            if change == 'late_arrival':
                original['received_at']='2026-07-01T16:06:00+08:00'
            elif change == 'missing_arrival':
                original.pop('received_at')
            elif change == 'missing_original':
                invalid['related_original_document_receipts']=[]
            else:
                original['announcement']['secCode']='000002'
            with pytest.raises(ValueError):
                _validate_valuation_document(invalid)
        document_path.write_text('changed source',encoding='utf-8')
        assert c.qualified_valuation_reviews(day) == {}
        assert c.client.calls == []


def test_valuation_cli_is_scoped_and_holds_pipeline_lock(tmp_path, monkeypatch):
    import json
    import sys
    from scripts import backfill_2026_tushare as cli
    calls = []
    class Collector:
        def __init__(self, db, **kwargs):
            assert kwargs['retries'] == 1
            assert (tmp_path/'cli.duckdb.pipeline.lock').exists()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def collect_valuation_inputs(self, day, codes):
            calls.append((day,codes))
            return dict(completion={'rows': {codes[0]: {'valuation_eligible': False}}})
    monkeypatch.setattr(cli,'TushareHistoryCollector',Collector)
    args = ['backfill','--db',str(tmp_path/'cli.duckdb'),'--start-date','20260701','--end-date','20260701',
            '--stock-codes','000001.SZ','--valuation-diagnostic','--report',str(tmp_path/'diagnostic.json')]
    monkeypatch.setattr(sys,'argv',args)
    assert cli.main() == 2
    assert calls == [('20260701',['000001.SZ'])]
    assert json.loads((tmp_path/'diagnostic.json').read_text())['completion']['rows']['000001.SZ']['valuation_eligible'] is False
    assert not (tmp_path/'cli.duckdb.pipeline.lock').exists()
    assert (tmp_path/'cli.duckdb.pipeline.lock.guard').exists()
    monkeypatch.setattr(sys,'argv',args+['--plan-only'])
    with pytest.raises(SystemExit):
        cli.main()
    assert len(calls) == 1
    class Preparation:
        def __init__(self, db, **kwargs):
            assert kwargs == {'offline': True}
            assert not (tmp_path/'cli.duckdb.pipeline.lock').exists()
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def prepare_valuation_reviews(self,day,codes):
            return dict(rows={codes[0]:dict(qualified_core=False)},market_requests=0,business_rows_written=0)
    monkeypatch.setattr(cli,'TushareHistoryCollector',Preparation)
    monkeypatch.setattr(sys,'argv',[('--valuation-prepare' if value=='--valuation-diagnostic' else value) for value in args])
    assert cli.main() == 2
    assert json.loads((tmp_path/'diagnostic.json').read_text())['market_requests'] == 0
    assert len(calls) == 1


def test_dated_identity_allows_flow_publication_but_not_definition_shortcut(tmp_path, monkeypatch):
    from trade_system.flow_contract import independent_comparison_contract
    from trade_system.xiaodefa_source import XiaodefaError
    class Flow:
        def query_rows(self, api_name, params=None, fields=''):
            assert api_name == 'moneyflow'
            return [dict(ts_code='000001.SZ',trade_date='20260701',buy_elg_amount=2,
                         sell_elg_amount=1,buy_lg_amount=2,sell_lg_amount=1)]
    with TushareHistoryCollector(tmp_path/'dated-flow.duckdb',client=Flow()) as c:
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES ('000001.SZ'),('000022.SZ')")
        monkeypatch.setattr(c,'_is_production_source',lambda: True)
        monkeypatch.setattr(c,'_suspension_rows',lambda day: [])
        reference = {'membership_date':'2026-06-30','not_listed':['000022.SZ']}
        monkeypatch.setattr(c,'_reference_version',lambda *args: reference)
        # Source acquisition is fixture-only; exercise validation and atomic raw/projection commit.
        monkeypatch.setattr(c,'_read_rows',lambda api,params,fields: c.client.query_rows(api,params,fields))
        with pytest.raises(XiaodefaError,match='moneyflow coverage incomplete'):
            c._collect_moneyflow('20260701')
        assert c.store.conn.execute('SELECT count(*) FROM tushare_moneyflow').fetchone()[0] == 0
        reference['membership_date']='2026-07-01'
        assert c._collect_moneyflow('20260701') == 1
        assert c.store.conn.execute("SELECT provider,source_api,main_net FROM multi_source_stock_flow").fetchone() == ('tushare','moneyflow',20000)
        c.store.conn.execute("INSERT INTO multi_source_stock_flow(source_date,stock_code,provider,origin_provider,"
            "source_api,flow_definition,amount_unit,field_mapping_version,main_net,is_stale) "
            "VALUES ('2026-07-01','000001','relay','eastmoney','moneyflow_dc','provider_main_orders_net','yuan','v3',20000,false)")
        contract = independent_comparison_contract(c.store.conn,'2026-07-01','relay','tushare')
        assert not contract['eligible'] and contract['reason']=='definition_alignment_unproven'
        assert c.store.conn.execute("SELECT count(*) FROM tushare_stock_basic WHERE ts_code='000022.SZ'").fetchone()[0] == 1


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


def test_current_bse_membership_does_not_invent_listing_date(tmp_path, monkeypatch):
    import time
    from datetime import date, timedelta
    import pytest
    today = date.today().isoformat()
    with TushareHistoryCollector(tmp_path/'member.duckdb', client=XiaodefaClient(token='fixture')) as c:
        monkeypatch.setattr(c, '_read_rows', lambda api, params, fields: [dict(
            ts_code='000001.SZ', symbol='000001', list_date='19910403', list_status='L')]
            if params['list_status']=='L' else [])
        membership = dict(as_of=today, source_session=today, listings={'920202':today.replace('-','')})
        monkeypatch.setattr(c, '_bse_listing_membership', lambda: membership)
        monkeypatch.setattr(c, '_exchange_listing_membership', lambda exchange='SZ':
            dict(as_of=today,listings={'000001':'1991-04-03'}))
        native = dict(thscode='920202.BJ',asset_type='a-share',list_date=None,name='fixture')
        monkeypatch.setattr(c, 'stock_listing_evidence', lambda *_: dict(timestamp=int(time.time()*1000),item=[native]))
        assert c.collect_stock_basic() == 2
        assert c.store.conn.execute("SELECT list_date FROM tushare_stock_basic WHERE ts_code='920202.BJ'").fetchone() == (None,)
        assert c._reference_version()['membership_only'] == ['920202.BJ']
        assert c._expected_stock_codes(today) == {'000001.SZ','920202.BJ'}
        with pytest.raises(XiaodefaError,match='historical listing dates unavailable'):
            c._expected_stock_codes((date.today()-timedelta(days=1)).isoformat())
        native['list_date'] = (date.today()+timedelta(days=1)).isoformat()
        with pytest.raises(XiaodefaError, match='disagreement'):
            c.collect_stock_basic(force=True)
        assert c.store.conn.execute('SELECT count(*) FROM tushare_stock_basic').fetchone()[0] == 2
        native['list_date'] = None
        membership['as_of'] = (date.today()-timedelta(days=1)).isoformat()
        with pytest.raises(XiaodefaError, match='current membership evidence'):
            c.collect_stock_basic(force=True)


def test_completed_reference_acquisition_reused_without_refreshing_time(tmp_path, monkeypatch):
    client=XiaodefaClient(token='fixture')
    calls=[]
    def pages(api, **kw):
        calls.append(kw)
        rows=[dict(ts_code='000001.SZ',list_status='L',list_date='19910403')]
        kw['on_page'](0,rows)
        return rows
    monkeypatch.setattr(client,'query_all',pages)
    with TushareHistoryCollector(tmp_path/'cache.duckdb',client=client,budget_seconds=60) as c:
        params={'list_status':'L'}
        first=c._read_rows('stock_basic',params,'ts_code')
        before=c.store.conn.execute('SELECT observed_at FROM multi_source_observation ORDER BY observed_at').fetchall()
        assert c._read_rows('stock_basic',params,'ts_code') == first
        assert len(calls)==1 and 0<calls[0]['total_timeout']<=60
        assert c.store.conn.execute('SELECT observed_at FROM multi_source_observation ORDER BY observed_at').fetchall()==before
        c.store.conn.execute("UPDATE multi_source_observation SET payload_hash='bad' WHERE data_type='tushare_stock_basic_acquisition_snapshot'")
        c._read_rows('stock_basic',params,'ts_code')
        assert len(calls)==2
        # Partial page receipts are never eligible for this cache.
        c.store.conn.execute("DELETE FROM multi_source_observation WHERE data_type='tushare_stock_basic_acquisition_snapshot'")
        c._read_rows('stock_basic',params,'ts_code')
        assert len(calls)==3


def test_retained_stock_reference_binds_requested_day_without_refreshing_arrival():
    from datetime import datetime
    import hashlib
    import json
    import duckdb
    from trade_system.ths_quality import qualified_stock_reference
    with duckdb.connect(':memory:') as con:
        con.execute('''CREATE TABLE tushare_stock_basic(ts_code VARCHAR,stock_code VARCHAR,
            stock_name VARCHAR,area VARCHAR,industry VARCHAR,market VARCHAR,list_date DATE,delist_date DATE)''')
        con.execute("INSERT INTO tushare_stock_basic VALUES ('000001.SZ','000001',NULL,NULL,NULL,NULL,'1991-04-03',NULL)")
        rows=con.execute('SELECT * FROM tushare_stock_basic ORDER BY ts_code').fetchall()
        version=hashlib.sha256(json.dumps(rows,ensure_ascii=False,default=str,separators=(',',':')).encode()).hexdigest()
        payload=json.dumps(dict(version=version,scope=['L','D'],listing_membership=dict(
            as_of='2026-09-29',not_listed=['000022.SZ'])))
        con.execute('''CREATE TABLE multi_source_observation(data_type VARCHAR,status VARCHAR,
            provider VARCHAR,observed_at TIMESTAMP,payload_json VARCHAR,payload_hash VARCHAR)''')
        con.execute("INSERT INTO multi_source_observation VALUES ('tushare_stock_basic_snapshot','qualified',"
            "'xiaodefa','2026-09-29 20:00:00',?,?)",[payload,hashlib.sha256(payload.encode()).hexdigest()])
        clock=datetime(2026,9,30,10)
        assert qualified_stock_reference(con,now=clock) is None
        original=qualified_stock_reference(con,now=clock,membership_date='2026-09-29')
        assert original['not_listed']==['000022.SZ'] and original['known_at']=='2026-09-29T20:00:00'
        assert qualified_stock_reference(con,now=clock,membership_date='2026-09-30') is None
        assert qualified_stock_reference(con,now=datetime(2026,9,30,21),membership_date='2026-09-29') is None
