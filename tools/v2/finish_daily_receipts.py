"""One bounded recovery of an interrupted native enrichment; preserve its original receipts."""
import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2 import daily_session as daily, native_enrichment as native
from trade_system.v2.daily_session_view import render,render_followup
from trade_system.v2.domain import file_hash,identity,now_utc,canonical
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.publisher import publish


def run(partial,parent,following,output):
    partial=Path(partial);output=Path(output)
    if output.exists() or partial.resolve() in output.resolve().parents:raise ValueError('separate new recovery output required')
    before={p.name:file_hash(p) for p in partial.iterdir() if p.is_file()}
    reg=read_json(partial/'registration.json')[0];failure=read_json(partial/'failed.json')[0]
    previous=daily.verify(parent);current=daily.verify(following)
    if reg['parent_report_id']!=previous['report_id'] or reg['trade_date']!=current['trade_date'] or reg['origin']!='hithink_native':raise ValueError('recovery parent/day/origin mismatch')
    expected=[c['instrument'] for c in previous['candidates']]
    if reg['instruments']!=expected or len(expected)>20 or len(reg['themes'])>3:raise ValueError('exact bounded cohort required')
    count=failure['retained_responses']
    if type(count) is not int or not 0<=count<len(expected):raise ValueError('bounded interrupted price prefix required')
    if set(before)!={'registration.json','failed.json',*(f'response-{i:02d}.json' for i in range(count))}:raise ValueError('partial package membership differs')
    prefix=[read_json(partial/f'response-{i:02d}.json')[0] for i in range(count)]
    native.derive({**reg,'instruments':expected[:count],'themes':[]},prefix)
    output.mkdir(parents=True,exist_ok=False);target=output/'enrichment';target.mkdir()
    recovery={'partial_manifest_id':identity(before),'previous_failure':failure,'preserved_receipts':count,
        'max_additional_requests':len(expected)+len(reg['themes'])-count,'failed_request_retry_limit':1,'started_at':now_utc().isoformat()}
    reg={**reg,'retries':1,'recovery':recovery}
    write_json(target/'registration.json',reg)
    for i in range(count):shutil.copyfile(partial/f'response-{i:02d}.json',target/f'response-{i:02d}.json')
    start=int(datetime.fromisoformat(reg['trade_date']).replace(tzinfo=daily.CST).timestamp()*1000)
    requests=[]
    for code in expected:
        exchange,ticker=code.split('.')
        requests.append((native.PRICES,{'thscode':ticker+'.'+exchange,'interval':'1d','start':start,'end':start+86400000-1,'adjust':'none','offset':0}))
    requests.extend((native.MEMBERS,{'thscode':t}) for t in reg['themes'])
    responses=list(prefix)
    try:
        from trade_system.hithink_client import HiThinkClient
        client=HiThinkClient(timeout=15,max_response_bytes=8_000_000,single_attempt=True)
        for i,(path,params) in enumerate(requests[count:],count):
            time.sleep(.75)
            rec={'path':path,'params':params,'data':client._get(path,params),'received_at':now_utc().isoformat()}
            write_json(target/f'response-{i:02d}.json',rec);responses.append(rec)
        enriched=native.derive(reg,responses);write_json(target/'report.json',enriched);daily.seal(target)
        native.verify(target)
        if daily.verify(parent)!=previous or daily.verify(following)!=current:
            raise ValueError('daily input changed during recovery')
        review=daily.next_review(previous,current,[],created=now_utc(),observations=enriched['observations'],enrichment=enriched)
        write_json(output/'review.json',review)
        result=publish(output/'publication',identity([current['report_id'],review['review_id']]),{
            'index.html':render(current).encode('utf-8'),'review.html':render_followup(review).encode('utf-8'),
            'observation.json':canonical(current).encode(),'review.json':canonical(review).encode()},generation=1)
        result={'scope':'recovered_native_followup_no_human_judgement_created','recovery':recovery,'publication':result,
            'source_sha256':file_hash(Path(__file__)),
            'current_candidates':len(current['candidates']),'review_cohort':len(review['rows']),
            'observed_prices':sum(r['observation'] is not None for r in review['rows']),
            'missing_prices':sum(r['observation'] is None for r in review['rows']),
            'human_judgements':0,'human_loop_complete':False,'execution_ready':False}
        if before!={p.name:file_hash(p) for p in partial.iterdir() if p.is_file()}:raise ValueError('original partial receipts changed')
        write_json(output/'result.json',result);return result
    except Exception as e:
        write_json(output/'failed.json',{'error_type':type(e).__name__,'http_status':getattr(e,'code',None),'retained_responses':len(responses),'source_partial_unchanged':before=={p.name:file_hash(p) for p in partial.iterdir() if p.is_file()}})
        raise


def verify_and_publish(recovered,parent,following,output):
    """Offline current-render publication; no receipt recollection or invented judgements."""
    recovered=Path(recovered);output=Path(output).resolve()
    if output.exists() or any(Path(p).resolve()==output or Path(p).resolve() in output.parents for p in (recovered,parent,following)):
        raise ValueError('new publication outside immutable inputs required')
    previous=daily.verify(parent);current=daily.verify(following);enriched=native.verify(recovered/'enrichment')
    original=read_json(recovered/'review.json')[0]
    review=daily.next_review(previous,current,[],created=original['created_at'],observations=enriched['observations'],enrichment=enriched)
    if review!=original:raise ValueError('recovered review differs from exact input replay')
    from trade_system.v2 import daily_session_view
    artifacts={'index.html':render(current).encode('utf-8'),'review.html':render_followup(review).encode('utf-8'),
        'observation.json':canonical(current).encode(),'review.json':canonical(review).encode()}
    output.mkdir(parents=True,exist_ok=False)
    publication=publish(output/'publication',identity([current['report_id'],review['review_id'],file_hash(daily_session_view.__file__)]),artifacts,generation=1)
    result={'scope':'offline_replay_and_current_view_not_new_native_capture_or_human_acceptance',
        'publication':publication,'parent_report_id':previous['report_id'],'following_report_id':current['report_id'],
        'enrichment_id':enriched['enrichment_id'],'review_id':review['review_id'],'recovered_review_sha256':file_hash(recovered/'review.json'),
        'view_source_sha256':file_hash(daily_session_view.__file__),'tool_source_sha256':file_hash(Path(__file__)),
        'raw_input_replay_equal':True,'native_requests':0,'human_judgements':0,'execution_ready':False}
    write_json(output/'result.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True);source.add_argument('--partial');source.add_argument('--recovered')
    for name in ('parent','following','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();print(canonical(verify_and_publish(a.recovered,a.parent,a.following,a.output) if a.recovered else run(a.partial,a.parent,a.following,a.output)))
