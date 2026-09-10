import json
from copy import deepcopy
from datetime import datetime, timezone

import duckdb
import pytest

from trade_system.v2.account_admission import inspect_account
from trade_system.v2.domain import identity
from trade_system.v2.program_supplement import collect, merge
from trade_system.v2.research_workbench import import_note, render, validate_note


def report():
    d = {'variants':{'a':{'fills':[{'instrument':'SZ.000001'}],'data_requests':[],'skips':[]}}}
    return {**d,'report_id':identity(d)}


def note():
    return {'schema':1,'report_id':report()['report_id'],'note_id':'human-note-1',
        'created_at':'2026-09-10T10:00:00Z','variant':'a','instrument':'SZ.000001','intent':'observe',
        'hypothesis':'Evidence first','invalidation':'Missing price','reflection':'',
        'scope':'research_note_no_execution'}


def test_note_archive_is_idempotent_and_immutable(tmp_path):
    n=note();raw=json.dumps(n).encode()
    assert not import_note(raw,report(),tmp_path)['execution_ready']
    import_note(raw,report(),tmp_path)
    assert len(list(tmp_path.iterdir()))==1
    n['reflection']='changed'
    with pytest.raises(ValueError,match='different content'):
        import_note(json.dumps(n).encode(),report(),tmp_path)


@pytest.mark.parametrize('key,value',[('scope','execute'),('intent','buy'),('instrument','SZ.000002'),
    ('report_id','other'),('hypothesis',''),('invalidation',''),('reflection','x'*4001)])
def test_unsafe_note_rejected(key,value):
    n=note();n[key]=value
    with pytest.raises(ValueError):validate_note(n,report())


def test_tampered_report_is_not_an_authority():
    r=report();r['variants']['a']['fills'].append({'instrument':'SZ.000002'})
    with pytest.raises(ValueError,match='fingerprint'):validate_note(note(),r)


def test_embedded_data_cannot_close_script_and_no_network():
    html=render({'malicious':'</script><script>alert(1)</script>'})
    assert '\\u003c/script\\u003e' in html
    assert 'connect-src \'none\'' in html and 'fetch(' not in html
    assert '<script>alert(1)</script>' not in html


def test_supplement_append_not_overwrite_or_expand():
    b={'bars':[],'calendar':['2026-04-23'],'identity_map':{'000609':'SZ.000609'},'predictions':{'a':[]}}
    row={'instrument':'SZ.000609','date':'2026-04-23','open_fen':1130,'close_fen':1130,'factor':6.029}
    merged=merge(b,[row]);assert b['bars']==[] and merged['predictions']==b['predictions']
    bad=deepcopy(row);bad['close_fen']=1140
    with pytest.raises(ValueError,match='overwrite'):merge(merged,[bad])
    bad=deepcopy(row);bad['date']='2026-04-24'
    with pytest.raises(ValueError,match='expand'):merge(b,[bad])


def test_exact_cache_provenance_no_inference(tmp_path):
    db=tmp_path/'cache.duckdb'
    with duckdb.connect(str(db)) as c:
        c.execute('CREATE TABLE tushare_daily(ts_code VARCHAR,stock_code VARCHAR,date DATE,open DOUBLE,close DOUBLE,provider VARCHAR,adjustment VARCHAR,fetched_at TIMESTAMP)')
        c.execute('CREATE TABLE tushare_adj_factor(ts_code VARCHAR,stock_code VARCHAR,date DATE,adj_factor DOUBLE,fetched_at TIMESTAMP)')
        c.execute("INSERT INTO tushare_daily VALUES ('000609.SZ','000609','2026-04-23',11.3,11.3,'tushare','none','2026-07-01')")
        c.execute("INSERT INTO tushare_adj_factor VALUES ('000609.SZ','000609','2026-04-23',6.029,'2026-07-01')")
    r=collect(db,[{'instrument':'SZ.000609','date':d} for d in ['2026-04-23','2026-04-24']],'2026-09-10T10:00:00Z')
    assert len(r['bars'])==len(r['missing'])==1
    assert r['bars'][0]['open_fen']==1130 and r['evidence'][0]['historical_first_known_at'] is None
    with duckdb.connect(str(db)) as c:c.execute("UPDATE tushare_daily SET adjustment='qfq'")
    assert not collect(db,[{'instrument':'SZ.000609','date':'2026-04-23'}],'2026-09-10T10:00:00Z')['bars']


def test_missing_account_not_zero_account():
    r=inspect_account()
    assert r['status']=='missing_real_account' and r['reconciled'] is None and not r['execution_ready']


def test_account_probe_isolated_and_never_live_ready(tmp_path):
    d={'account_id':'synthetic-test-only','mode':'live','asof':'2026-09-10T09:00:00Z',
        'valid_until':'2026-09-10T11:00:00Z','cash':'100','frozen_cash':'0','equity':'200',
        'positions':[{'instrument':'SZ.000001','quantity':10,'sellable':5,'mark_price':'10'}],
        'open_orders':[],'declared_complete':True,'source':'manual_declaration'}
    path=tmp_path/'account.json';path.write_text(json.dumps(d))
    clock=lambda:datetime(2026,9,10,10,tzinfo=timezone.utc)
    r=inspect_account(path,clock=clock)
    assert r['reconciled'] and not r['execution_ready'] and 'account_id' not in r
    assert list(tmp_path.iterdir())==[path]
    d['frozen_cash']='10';path.write_text(json.dumps(d))
    assert not inspect_account(path,clock=clock)['reconciled']
    d['positions'][0]['sellable']=11;path.write_text(json.dumps(d))
    with pytest.raises(ValueError,match='sellable'):inspect_account(path,clock=clock)
