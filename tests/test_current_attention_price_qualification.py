"""Original and current attention evidence must both qualify before comparison.

These tests seal immutable JSON evidence only; no database or providers are used.
"""
from copy import deepcopy
import json

import pytest

from trade_system.v2.daily_workspace import seal_evidence
from trade_system.v2.domain import identity
from trade_system.v2.research_journal import attention_followups
from trade_system.v2.market_workspace import price_snapshot_qualified


def stock(close=10):
    return {'stock_code':'000001','close':close,'change_pct':2,
            'price_contract':['xiaodefa','none','shares','yuan'],
            'price_status':'available','price_reason':None}


def snapshot(day, row):
    value={'trade_date':day,'as_of':day+'T18:00:00+08:00','stocks':{'000001':row},
           'scope':'read_only_market_review_not_execution','execution_ready':False}
    return dict(value,snapshot_id=identity(value))


def compare(tmp_path, original, current, *, day='2026-10-12'):
    source=snapshot('2026-10-09',original)
    evidence=seal_evidence(tmp_path,source)
    note={'schema':2,'note_id':'synthetic-attention','instrument':'000001',
          'evidence_file_id':evidence,'evidence_id':source['snapshot_id'],
          'received_at':'2026-10-09T19:00:00+08:00'}
    path=tmp_path/'notes/evidence'/f'{evidence}.json'
    before=path.read_bytes()
    rows=attention_followups(tmp_path,[note],snapshot(day,current))
    assert path.read_bytes()==before
    return rows[0]


@pytest.mark.parametrize('side',['original','current'])
@pytest.mark.parametrize('defect',[
    'missing_status','unqualified_status','missing_reason','contradictory_reason',
    'missing_contract','null_contract','short_contract','unknown_provider',
    'unknown_adjustment','unknown_volume','unknown_amount','negative_close','bool_close',
    'missing_change','bool_change'])
def test_unqualified_side_never_becomes_a_price_followup(tmp_path,side,defect):
    original=stock();current=stock(11)
    row=original if side=='original' else current
    if defect=='missing_status':row.pop('price_status')
    elif defect=='unqualified_status':row['price_status']='unqualified'
    elif defect=='missing_reason':row.pop('price_reason')
    elif defect=='contradictory_reason':row['price_reason']='unknown_adjustment'
    elif defect=='missing_contract':row.pop('price_contract')
    elif defect=='null_contract':row['price_contract']=None
    elif defect=='short_contract':row['price_contract']=['xiaodefa','none']
    elif defect=='negative_close':row['close']=-10
    elif defect=='bool_close':row['close']=True
    elif defect=='missing_change':row.pop('change_pct')
    elif defect=='bool_change':row['change_pct']=True
    else:row['price_contract'][['unknown_provider','unknown_adjustment','unknown_volume','unknown_amount'].index(defect)]='unknown'
    result=compare(tmp_path,original,current)
    assert result['status']=='price_unqualified'
    assert result['change_pct'] is None
    assert result['price_qualification_reasons']=={side:'price_qualification_unverified'}


@pytest.mark.parametrize('legacy',[True,False])
def test_same_unknown_contract_or_absent_contract_is_not_proof(tmp_path,legacy):
    original=stock();current=stock(11)
    for row in (original,current):
        if legacy:
            for field in ('price_contract','price_status','price_reason'):row.pop(field)
        else:
            row.update(price_status='unqualified',price_reason='unknown_adjustment')
            row['price_contract'][1]='unknown'
    result=compare(tmp_path,original,current)
    assert result['status']=='price_unqualified'
    assert result['change_pct'] is None
    assert set(result['price_qualification_reasons'])=={'original','current'}


@pytest.mark.parametrize('side',['original','current'])
@pytest.mark.parametrize('field',['close','change_pct'])
def test_large_json_integer_is_unqualified_without_breaking_followup(tmp_path,side,field):
    original=stock();current=stock(11)
    row=original if side=='original' else current
    row[field]=10**400
    # This is valid, bounded JSON, even though conversion to float overflows.
    original,current=json.loads(json.dumps([original,current]))
    result=compare(tmp_path,original,current)
    assert result['status']=='price_unqualified'
    assert result['change_pct'] is None
    assert result['price_qualification_reasons']=={side:'price_qualification_unverified'}


@pytest.mark.parametrize('field,value',[(0,'baostock'),(1,'qfq'),(2,'hands'),(3,'thousand_yuan')])
def test_known_contract_change_remains_incomparable(tmp_path,field,value):
    current=stock(11);current['price_contract'][field]=value
    result=compare(tmp_path,stock(),current)
    assert result['status']=='source_conflict'
    assert result['change_pct'] is None


def test_complete_none_contract_can_compare_and_seal_preserves_qualification(tmp_path):
    original=stock();current=stock(11)
    before=deepcopy((original,current))
    result=compare(tmp_path,original,current)
    assert result['status']=='observed_close_change_not_execution_return'
    assert result['change_pct']==pytest.approx(10)
    assert result['price_qualification_reasons']=={}
    assert (original,current)==before
    saved=json.loads(next((tmp_path/'notes/evidence').glob('*.json')).read_text(encoding='utf-8'))
    assert saved['stocks']['000001']==original
    assert saved['execution_ready'] is False


def test_qualified_zero_change_is_a_valid_result(tmp_path):
    result=compare(tmp_path,stock(),stock())
    assert result['status']=='observed_close_change_not_execution_return'
    assert result['change_pct']==0


def test_nonfinite_derived_change_is_not_published(tmp_path):
    result=compare(tmp_path,stock(1e-300),stock(1e300))
    assert result['status']=='price_unqualified'
    assert result['change_pct'] is None
    assert result['price_qualification_reasons']=={'comparison':'nonfinite_price_change'}


def test_missing_close_remains_missing_not_zero(tmp_path):
    current=stock();current['close']=None
    result=compare(tmp_path,stock(),current)
    assert result['status']=='source_missing'
    assert result['change_pct'] is None


def test_same_session_does_not_invent_a_followup(tmp_path):
    result=compare(tmp_path,stock(),stock(11),day='2026-10-09')
    assert result['status']=='pending_next_session'
    assert result['change_pct'] is None


def test_changed_original_evidence_is_still_refused(tmp_path):
    source=snapshot('2026-10-09',stock())
    evidence=seal_evidence(tmp_path,source)
    note={'schema':2,'note_id':'synthetic-attention','instrument':'000001','evidence_file_id':evidence}
    source['stocks']['000001']['price_status']='unqualified'
    (tmp_path/'notes/evidence'/f'{evidence}.json').write_text(json.dumps(source),encoding='utf-8')
    with pytest.raises(ValueError,match='original attention evidence changed'):
        attention_followups(tmp_path,[note],snapshot('2026-10-12',stock(11)))


@pytest.mark.parametrize('value',[None,[],True,'available',42])
def test_snapshot_helper_refuses_non_stock_values(value):
    assert not price_snapshot_qualified(value)


@pytest.mark.parametrize('field',['close','change_pct'])
@pytest.mark.parametrize('value',[None,True,'10',float('inf'),float('-inf'),float('nan')])
def test_snapshot_helper_requires_actual_finite_numbers(field,value):
    row=stock();row[field]=value
    assert not price_snapshot_qualified(row)


@pytest.mark.parametrize('contract',['xiaodefa,none,shares,yuan',{'provider':'xiaodefa'},
                                     ['xiaodefa',None,'shares','yuan'],
                                     ['xiaodefa','none','shares','yuan','extra']])
def test_snapshot_helper_refuses_malformed_contracts(contract):
    row=stock();row['price_contract']=contract
    assert not price_snapshot_qualified(row)


@pytest.mark.parametrize('adjustment',['none','qfq','hfq'])
def test_known_matching_adjustments_remain_qualified(adjustment):
    row=stock();row['price_contract'][1]=adjustment
    assert price_snapshot_qualified(row)


@pytest.mark.parametrize('defect',['note_missing_receipt','note_naive_receipt','original_missing_clock',
    'original_invalid_clock','current_missing_clock','current_invalid_clock','current_future_clock','current_clock_before_day'])
def test_original_note_and_current_view_clocks_are_both_required(tmp_path,monkeypatch,defect):
    from datetime import datetime
    from trade_system.v2 import domain
    monkeypatch.setattr(domain,'now_utc',lambda:datetime.fromisoformat('2026-10-13T19:00:00+08:00'))
    original=snapshot('2026-10-09',stock());current=snapshot('2026-10-12',stock(11))
    if defect=='original_missing_clock':original.pop('as_of')
    elif defect=='original_invalid_clock':original['as_of']='not a timestamp'
    elif defect=='current_missing_clock':current.pop('as_of')
    elif defect=='current_invalid_clock':current['as_of']='2026-10-12T24:00:00+08:00'
    elif defect=='current_future_clock':current['as_of']='2026-10-14T18:00:00+08:00'
    elif defect=='current_clock_before_day':current['as_of']='2026-10-11T18:00:00+08:00'
    evidence=seal_evidence(tmp_path,original)
    note={'schema':2,'note_id':'synthetic-clock','instrument':'000001','evidence_file_id':evidence,
          'received_at':'2026-10-09T19:00:00+08:00'}
    if defect=='note_missing_receipt':note.pop('received_at')
    elif defect=='note_naive_receipt':note['received_at']='2026-10-09T19:00:00'
    row=attention_followups(tmp_path,[note],current)[0]
    assert row['change_pct'] is None
    assert row['original_close']==10 and row['current_close']==11
