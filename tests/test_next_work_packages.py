from datetime import datetime,timedelta
import json

import duckdb
import pytest

from scripts.export_qlib_features import apply_calendar_overlay
from tools.v2.probe_native_gaps import calendar_overlay
from trade_system.v2.daily_session import seal,review_learning
from trade_system.v2.domain import identity,file_hash
from trade_system.v2.gap_evidence import write_json


def calendar_rows():
    return [{'exchange':'SSE','cal_date':(datetime(2024,1,1)+timedelta(days=i)).strftime('%Y%m%d'),
             'is_open':int((datetime(2024,1,1)+timedelta(days=i)).weekday()<5),'pretrade_date':None} for i in range(366)]


def test_calendar_repair_is_read_only_and_binds_receipts(tmp_path):
    rows=calendar_rows();receipt={'provider':'xiaodefa_relay','api':'trade_cal',
        'params':{'exchange':'SSE','start_date':'20240101','end_date':'20241231'},'rows':rows,
        'received_at':'2026-09-11T00:00:00+00:00','scope':'synthetic_test'}
    folder=tmp_path/'receipt';folder.mkdir()
    write_json(folder/'calendar-relay-receipt.json',receipt)
    write_json(folder/'calendar-overlay.json',{**calendar_overlay(rows),'receipt_sha256':identity(receipt)})
    seal(folder)
    path=tmp_path/'source.db'
    with duckdb.connect(str(path)) as c:
        c.execute("CREATE TABLE tushare_trade_cal(cal_date DATE,is_open BOOLEAN); INSERT INTO tushare_trade_cal VALUES ('2024-01-02',true),('2025-01-02',true)")
    before=file_hash(path)
    with duckdb.connect(str(path),read_only=True) as c:
        result=apply_calendar_overlay(c,folder)
        assert not result['source_database_modified'] and not result['historical_PIT_qualified']
        assert c.execute("SELECT count(*) FROM verified_calendar_overlay WHERE cal_date='2024-01-03'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM tushare_trade_cal').fetchone()[0]==2
    assert file_hash(path)==before
    (folder/'calendar-overlay.json').write_text('{}')
    with duckdb.connect(str(path),read_only=True) as c,pytest.raises(ValueError,match='changed'):
        apply_calendar_overlay(c,folder)


@pytest.mark.parametrize('bad',['missing','duplicate','implicit_status'])
def test_calendar_repair_rejects_incomplete_or_ambiguous_evidence(bad):
    rows=calendar_rows()
    if bad=='missing':rows.pop()
    if bad=='duplicate':rows[-1]=rows[0]
    if bad=='implicit_status':rows[0]['is_open']='1'
    with pytest.raises(ValueError):calendar_overlay(rows)


def test_learning_keeps_late_conflicting_missing_and_rejected_cases():
    def note(who,intent,time,timing='recorded_before_next_open'):
        return {'note':{'operator':who,'intent':intent},'received_at':'2026-09-11T'+time+'+08:00','timing':timing}
    def row(notes,observed=True):
        return {'judgements':notes,'observation':{'open':'10','high':'12','low':'9','close':'11'} if observed else None,
                'next_session_open_close_pct':'10' if observed else None}
    rows=[row([note('a','observe','08:00:00'),note('a','reject','09:00:00'),
               note('a','paper_hypothesis','10:00:00','retrospective_not_prospective')]),
          row([note('a','observe','09:00:00'),note('b','reject','09:00:00')]),row([],False)]
    result=review_learning(rows)
    assert [r['review_group'] for r in rows]==['reject','conflicting_declarations','no_prior_judgement']
    assert result['groups']['reject']['observed_count']==1
    assert result['groups']['no_prior_judgement']['mean_open_close_pct'] is None
    assert sum(g['cohort_count'] for g in result['groups'].values())==3
    assert rows[0]['next_session_open_to_low_pct']=='-10.0'
    assert not result['automatic_strategy_update'] and result['actual_operator_return'] is None
    assert 'not_inferred_from_price' in rows[0]['hypothesis_verdict']


def test_same_time_conflicting_intents_are_not_resolved_by_sort_order():
    rows=[{'judgements':[{'timing':'recorded_before_next_open','received_at':'2026-09-11T09:00:00+08:00',
             'note':{'operator':'a','intent':v}} for v in ('observe','reject')],
             'observation':None,'next_session_open_close_pct':None}]
    result=review_learning(rows)
    assert result['groups']['conflicting_declarations']['cohort_count']==1
    assert json.dumps(result)
