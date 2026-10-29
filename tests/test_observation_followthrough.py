"""Synthetic, isolated manual observation contracts; no provider requests."""
from datetime import datetime, timedelta
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest

from trade_system.v2 import research_product as product, research_journal as journal
from trade_system.v2 import operator_workflow as workflow
from trade_system.v2.domain import identity, now_utc
from trade_system.v2.research_product_server import handler
from tests.test_current_attention_price_qualification import stock, snapshot


def setup_note(output):
    market=snapshot('2026-10-12',stock())
    product.publish_desk(output,market=market)
    values=dict(instrument='000001',operator='SYNTHETIC ONLY',intent='observe',
        hypothesis='D0 observation',invalidation='human condition',request_id='1'*32,
        evidence_id=market['snapshot_id'])
    return product.save_note(output,values),values


def review_values(note, market, request='3'*32):
    return dict(note_id=note,request_id=request,reviewer='SYNTHETIC ONLY',
        evidence='D1 human check',conclusion='unclear',
        seen_market_snapshot_id=market['snapshot_id'],seen_market_evidence_file_id=identity(market))


def plan_values(note, request='2'*32):
    return dict(note_id=note,operator='SYNTHETIC ONLY',condition='manual',
        valid_until=(now_utc()+timedelta(days=1)).isoformat(),request_id=request)


def test_current_scope_is_not_recent_page_and_history_has_global_leaf_status(tmp_path):
    note,values=setup_note(tmp_path)
    plan=workflow.save_observation_plan(tmp_path,plan_values(note))
    original_note=journal.read_note(tmp_path,note)
    original_plan=workflow.read_observation_plan(tmp_path,plan)
    for i in range(102):
        other=dict(original_note,request_id=f'{i+100:032x}',hypothesis=f'bulk fixture {i}',
            received_at=(datetime.fromisoformat(original_note['received_at'])+timedelta(microseconds=i+1)).isoformat())
        other.pop('note_id');other['note_id']=identity(other)
        journal.durable_event(tmp_path/'notes/attention',other['note_id'],other)
        other_plan=dict(original_plan,note_id=other['note_id'],request_id=f'{i+1000:032x}',
            received_at=(datetime.fromisoformat(original_plan['received_at'])+timedelta(microseconds=i+1)).isoformat())
        other_plan.pop('plan_id');other_plan['plan_id']=identity(other_plan)
        journal.durable_event(tmp_path/'notes/plans',other_plan['plan_id'],other_plan)
    from trade_system.v2.journal_index import rebuild
    rebuild(tmp_path)
    data=product.saved_projection(tmp_path)
    assert note in {n['note_id'] for n in data['notes']}
    assert plan in {p['plan_id'] for p in data['plans']['rows']}
    page=product.journal_navigation(tmp_path,kind='note',before=None,limit=10)
    assert len(page['events'])==10 and page['next_before']
    assert product.journal_navigation(tmp_path,kind='note',selected=note)['selected']['note_id']==note
    product.save_note(tmp_path,dict(values,supersedes=note,request_id='c'*32))
    assert product.journal_navigation(tmp_path,kind='note',selected=note)['selected']['is_latest'] is False
    old_plan=next(p for p in workflow.observation_plans(tmp_path,now_utc().isoformat())['rows'] if p['plan_id']==plan)
    assert old_plan['evaluation']['invalidation']=='judgement_revised_requires_new_plan'


@pytest.mark.parametrize('invalidation',['revision','triggered'])
def test_new_plan_rejects_invalidated_note_but_old_request_retries(tmp_path,invalidation):
    note,values=setup_note(tmp_path)
    command=plan_values(note);plan=workflow.save_observation_plan(tmp_path,command)
    if invalidation=='revision':
        product.save_note(tmp_path,dict(values,supersedes=note,request_id='4'*32))
    else:
        market=snapshot('2026-10-13',stock(11));product.publish_desk(tmp_path,market=market)
        journal.save_review(tmp_path,dict(review_values(note,market),conclusion='triggered'))
    assert workflow.save_observation_plan(tmp_path,command)==plan
    with pytest.raises(ValueError,match='current|invalidated'):
        workflow.save_observation_plan(tmp_path,dict(command,request_id='5'*32))


def test_bound_review_is_immutable_across_new_publication_and_retry(tmp_path):
    note,_=setup_note(tmp_path)
    d1=snapshot('2026-10-13',stock(11));product.publish_desk(tmp_path,market=d1)
    values=review_values(note,d1);key=journal.save_review(tmp_path,values)
    event=tmp_path/'notes/attention/reviews'/f'{key}.json';original=event.read_bytes()
    product.publish_desk(tmp_path,market=snapshot('2026-10-14',stock(12)))
    assert journal.save_review(tmp_path,values)==key
    review=journal.review_projection(tmp_path,journal.read_review(tmp_path,key))
    assert review['market_binding']=='bound_daily_market_snapshot'
    assert review['seen_comparison']['current_close']==11
    assert review['seen_comparison']['change_pct']==pytest.approx(10)
    assert event.read_bytes()==original
    from trade_system.file_lock import FileLock
    with FileLock(tmp_path/'publication.guard'):
        assert journal.save_review(tmp_path,values)==key
        with pytest.raises(RuntimeError,match='lock busy'):
            journal.save_review(tmp_path,dict(values,request_id='a'*32))
    with pytest.raises(ValueError,match='changed'):
        journal.save_review(tmp_path,dict(values,request_id='6'*32))
    with pytest.raises(ValueError,match='different content'):
        journal.save_review(tmp_path,dict(values,seen_market_snapshot_id='f'*64))


def test_binding_requires_seen_hash_and_matching_plan_and_checks_integrity(tmp_path):
    note,values=setup_note(tmp_path)
    other=product.save_note(tmp_path,dict(values,request_id='7'*32))
    plan=workflow.save_observation_plan(tmp_path,plan_values(other))
    market=snapshot('2026-10-13',stock(11));product.publish_desk(tmp_path,market=market)
    command=review_values(note,market)
    with pytest.raises(ValueError,match='plan parent'):
        journal.save_review(tmp_path,dict(command,plan_id=plan))
    with pytest.raises(ValueError,match='changed'):
        journal.save_review(tmp_path,dict(command,seen_market_evidence_file_id='e'*64))
    key=journal.save_review(tmp_path,command)
    evidence=tmp_path/'notes/evidence'/f'{identity(market)}.json'
    evidence.write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='evidence changed'):
        journal.read_review(tmp_path,key)


def test_old_review_is_explicitly_unbound_and_future_seen_cannot_gain(tmp_path):
    note,_=setup_note(tmp_path)
    old=dict(note_id=note,request_id='8'*32,reviewer='old human',evidence='old free text',
        conclusion='unclear',prediction_id=None,instrument='000001',received_at=now_utc().isoformat(),
        execution_ready=False)
    old['review_id']=identity(old)
    journal.durable_event(tmp_path/'notes/attention/reviews',old['review_id'],old)
    assert journal.review_projection(tmp_path,journal.read_review(tmp_path,old['review_id']))['market_binding']=='historical_unbound'
    from trade_system.v2.journal_index import rebuild
    rebuild(tmp_path)
    future=snapshot('2099-10-13',stock(11));product.publish_desk(tmp_path,market=future)
    key=journal.save_review(tmp_path,review_values(note,future))
    comparison=journal.review_projection(tmp_path,journal.read_review(tmp_path,key))['seen_comparison']
    assert comparison['change_pct'] is None and comparison['status']=='future_evidence'


def test_new_review_requires_seen_identity_but_old_success_retry_survives_revision(tmp_path):
    note,values=setup_note(tmp_path)
    command=dict(note_id=note,conclusion='pending',reviewer='SYNTHETIC OLD',evidence='original legacy text')
    old=dict(command,request_id='9'*32,command_id=identity(command),prediction_id=None,
        instrument='000001',received_at=now_utc().isoformat(),execution_ready=False)
    old['review_id']=identity(old)
    journal.durable_event(tmp_path/'notes/attention/reviews',old['review_id'],old)
    from trade_system.v2.journal_index import rebuild
    rebuild(tmp_path)
    product.save_note(tmp_path,dict(values,supersedes=note,request_id='a'*32))
    before=(tmp_path/'notes/attention/reviews'/f"{old['review_id']}.json").read_bytes()
    assert journal.save_review(tmp_path,dict(command,request_id='9'*32))==old['review_id']
    with pytest.raises(ValueError,match='changed'):
        journal.save_review(tmp_path,dict(command,request_id='b'*32))
    assert before==(tmp_path/'notes/attention/reviews'/f"{old['review_id']}.json").read_bytes()


@pytest.mark.parametrize('defect',['same_day','contract','unknown','missing','missing_clock','clock_before_day'])
def test_bound_review_keeps_insufficient_facts_without_calculating_gain(tmp_path,defect):
    note,_=setup_note(tmp_path)
    market=snapshot('2026-10-12' if defect=='same_day' else '2026-10-13',stock(11))
    market.pop('snapshot_id')
    row=market['stocks']['000001']
    if defect=='contract':row['price_contract'][1]='qfq'
    elif defect=='unknown':row['price_contract'][1]='unknown'
    elif defect=='missing':row['close']=None
    elif defect=='missing_clock':market.pop('as_of')
    elif defect=='clock_before_day':market['as_of']='2026-10-12T18:00:00+08:00'
    market['snapshot_id']=identity(market)
    product.publish_desk(tmp_path,market=market)
    key=journal.save_review(tmp_path,review_values(note,market))
    review=journal.review_projection(tmp_path,journal.read_review(tmp_path,key))
    assert review['evidence']=='D1 human check'
    assert review['seen_comparison']['change_pct'] is None


def test_explicit_unbound_new_review_only_without_published_market(tmp_path):
    note,_=setup_note(tmp_path)
    values=dict(note_id=note,request_id='d'*32,conclusion='unclear',reviewer='SYNTHETIC ONLY',
        evidence='no published market',seen_market_snapshot_id='unbound')
    with pytest.raises(ValueError,match='changed'):journal.save_review(tmp_path,values)
    # Model an explicitly unavailable publication; original evidence remains intact.
    (tmp_path/'publication/current.json').rename(tmp_path/'publication/retained-pointer.json')
    key=journal.save_review(tmp_path,values)
    assert journal.review_projection(tmp_path,journal.read_review(tmp_path,key))['market_binding']=='explicit_unbound_no_published_market'


def test_get_navigation_reopens_old_receipt_without_writes_and_validates_queries(tmp_path):
    note,_=setup_note(tmp_path)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(tmp_path,tmp_path,0))
    server.RequestHandlerClass=handler(tmp_path,tmp_path,server.server_port)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    before={p.relative_to(tmp_path).as_posix():p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    try:
        with urlopen(origin+'/journal?kind=note&id='+note) as response:
            text=response.read().decode()
        assert note in text and 'D0 observation' in text
        for query in ('kind=bad','kind=note&before=bad','kind=note&limit=101','kind=note&id=../bad','kind=note&kind=plan'):
            with pytest.raises(HTTPError) as error:urlopen(origin+'/journal?'+query)
            assert error.value.code==400
    finally:server.shutdown();server.server_close();thread.join()
    assert before=={p.relative_to(tmp_path).as_posix():p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_journal_detail_leads_with_readable_fixed_seen_facts(tmp_path):
    from trade_system.v2.research_product_view import journal_page
    note,_=setup_note(tmp_path)
    market=snapshot('2026-10-13',stock(11));product.publish_desk(tmp_path,market=market)
    key=journal.save_review(tmp_path,review_values(note,market))
    text=journal_page(product.journal_navigation(tmp_path,kind='review',selected=key))
    assert '固定所见行情' in text and '原始价格' in text and '所见价格' in text
    assert 'D1 human check' in text and '2026-10-13' in text
    assert text.index('<summary>原件身份与完整字段</summary>')<text.index('<pre')
    assert text.count('<article id="review-'+key+'">')==1
    assert 'id="related-reviews"' in text


@pytest.mark.parametrize('future_field',['as_of','trade_date'])
def test_future_original_stays_unqualified_for_its_note_but_explicit_revision_has_own_clock(tmp_path,monkeypatch,future_field):
    from trade_system.v2 import domain
    clock=['2026-10-13T18:00:00+08:00']
    monkeypatch.setattr(domain,'now_utc',lambda:datetime.fromisoformat(clock[0]))
    original=snapshot('2026-10-12',stock())
    original.pop('snapshot_id')
    original['as_of']='2026-10-14T18:00:00+08:00'
    if future_field=='trade_date':original['trade_date']='2026-10-14'
    original['snapshot_id']=identity(original)
    product.publish_desk(tmp_path,market=original)
    command=dict(instrument='000001',operator='SYNTHETIC ONLY',intent='observe',hypothesis='future original',
        invalidation='manual',request_id='1'*32,evidence_id=original['snapshot_id'])
    note=product.save_note(tmp_path,command)
    original_path=tmp_path/'notes/attention'/f'{note}.json';note_bytes=original_path.read_bytes()
    seen=snapshot('2026-10-15',stock(11));clock[0]='2026-10-16T18:00:00+08:00'
    product.publish_desk(tmp_path,market=seen)
    values=review_values(note,seen);key=journal.save_review(tmp_path,values)
    old_review=tmp_path/'notes/attention/reviews'/f'{key}.json';review_bytes=old_review.read_bytes()
    current=journal.attention_followups(tmp_path,[journal.read_note(tmp_path,note)],seen)[0]
    fixed=journal.review_projection(tmp_path,journal.read_review(tmp_path,key))['seen_comparison']
    assert current['change_pct'] is None and fixed['change_pct'] is None
    assert current['status']==fixed['status']=='original_evidence_future_at_judgement'
    revision=product.save_note(tmp_path,dict(command,supersedes=note,request_id='4'*32))
    revised_key=journal.save_review(tmp_path,review_values(revision,seen,'5'*32))
    assert journal.review_projection(tmp_path,journal.read_review(tmp_path,revised_key))['seen_comparison']['change_pct']==pytest.approx(10)
    clock[0]='2026-10-17T18:00:00+08:00';product.publish_desk(tmp_path,market=snapshot('2026-10-16',stock(12)))
    assert journal.save_review(tmp_path,values)==key
    for row in product.saved_projection(tmp_path)['attention_followups']:
        if row['note_id']==note:assert row['change_pct'] is None
    assert journal.review_projection(tmp_path,journal.read_review(tmp_path,key))['seen_comparison']['change_pct'] is None
    assert original_path.read_bytes()==note_bytes and old_review.read_bytes()==review_bytes


def test_receipt_and_history_share_readable_fixed_fact_summary(tmp_path):
    from trade_system.v2.research_product_view import journal_page,review_receipt_page,render
    from html import unescape
    note,_=setup_note(tmp_path)
    market=snapshot('2026-10-13',stock(11));product.publish_desk(tmp_path,market=market)
    key=journal.save_review(tmp_path,review_values(note,market))
    review=journal.review_projection(tmp_path,journal.read_review(tmp_path,key))
    pages=[review_receipt_page(review),journal_page(product.journal_navigation(tmp_path,kind='review',selected=key))]
    for page in pages:
        primary=unescape(page.split('<details>')[0])
        assert '2026-10-13' in primary and '原始价格' in primary and '所见价格' in primary
        assert '中继' in primary and '不复权' in primary and '成交量：股' in primary
        assert '10.000000000000009' not in primary and "['xiaodefa'" not in primary
        assert 'bound_daily_market_snapshot' not in primary
        assert '10%' in primary
        assert '<summary>原件身份与完整字段</summary>' in page
    current=render(product.saved_projection(tmp_path))
    assert '选两只证券加入对比；模型正向贡献' not in current
