"""One separate, explicitly bounded retry package for a completed campaign's HTTP 429 gaps."""
import argparse
from collections import defaultdict
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2 import duplicate_year_campaign as campaign
from tools.v2 import probe_identity_sources as probe
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import canonical,file_hash,identity,now_utc,utc
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.research_receipts import sealed


def parent(folder):
    folder=Path(folder);reg=read_json(folder/'registration.json')[0];manifests={};requests=[];last=utc(reg['started_at'])
    if reg['origin']!='native_and_relay' or reg['scope']!=campaign.SCOPE or not 1<=len(reg['codes'])<=54 or reg['max_requests']!=10*len(reg['codes']) or reg['retries']!=0:raise ValueError('original real bounded campaign required')
    for batch,offset in enumerate(range(0,len(reg['codes']),6),1):
        name=f'batch-{batch:02d}';target=folder/name
        members,_,statuses=campaign.replay_batch(target,reg['codes'][offset:offset+6],reg['origin']);manifests[name]=identity(members)
        for p in target.glob('receipt-*.json'):last=max(last,utc(read_json(p)[0]['received_at']))
        for status in statuses:
            if status['status']=='failed' and status.get('error_type')=='HTTPError' and status.get('http_status')==429:
                requests.append({'batch':name,'index':status['index'],'request':campaign.plan(reg['codes'][offset:offset+6])[status['index']]})
    if not 1<=len(requests)<=4:raise ValueError('one to four observed rate-limit gaps required')
    return {'registration_sha256':file_hash(folder/'registration.json'),'batch_manifest_ids':manifests},requests,last


def capture(folder,output,*,client=None):
    binding,requests,last=parent(folder);output=Path(output)
    if output.resolve()==Path(folder).resolve() or Path(folder).resolve() in output.resolve().parents:raise ValueError('separate repair output required')
    if (now_utc()-last).total_seconds()<60:raise ValueError('at least 60 seconds after completed source capture required')
    output.mkdir(parents=True,exist_ok=False)
    reg={'parent_binding':binding,'requests':requests,'max_requests':len(requests),'automatic_retries':0,'explicit_attempts_per_failed_request':1,
        'origin':'native_and_relay' if client is None else 'synthetic_fixture','started_at':now_utc().isoformat(),'source_sha256':file_hash(Path(__file__))}
    write_json(output/'registration.json',reg);client=client or probe.Client()
    for i,item in enumerate(requests):
        try:
            time.sleep(.75);data=client.query(item['request'])
            if len(canonical(data).encode())>3_900_000:raise ValueError('response budget exceeded')
            write_json(output/f'receipt-{i:02d}.json',{'request':item['request'],'received_at':now_utc().isoformat(),'data':data})
            rows=probe.rows_for(item['request'],data);status={'index':i,'status':'observed','rows':len(rows)}
        except Exception as exc:status={'index':i,'status':'failed','error_type':type(exc).__name__,'http_status':getattr(exc,'code',None)}
        write_json(output/f'status-{i:02d}.json',status);print(canonical(status),flush=True)
    if parent(folder)[0]!=binding:raise ValueError('original campaign changed')
    seal(output)


def replay(folder,repair):
    repair=Path(repair);members=sealed(repair);reg=read_json(repair/'registration.json')[0];binding,requests,last=parent(folder)
    if (reg['parent_binding']!=binding or reg['requests']!=requests or reg['max_requests']!=len(requests) or reg['automatic_retries']!=0
        or reg['explicit_attempts_per_failed_request']!=1 or reg['origin']!='native_and_relay' or (utc(reg['started_at'])-last).total_seconds()<60):raise ValueError('bounded real repair binding differs')
    required={'registration.json',*(f'status-{i:02d}.json' for i in range(len(requests)))}
    if not required<=set(members) or not set(members)<=required|{f'receipt-{i:02d}.json' for i in range(len(requests))}:raise ValueError('repair membership differs')
    evidence=defaultdict(list);count=0
    for i,item in enumerate(requests):
        r=item['request'];status=read_json(repair/f'status-{i:02d}.json')[0];filename=f'receipt-{i:02d}.json'
        if status.get('index')!=i or status.get('status') not in ('observed','failed'):raise ValueError('invalid repair status')
        if filename in members:
            receipt=read_json(repair/filename)[0]
            if receipt['request']!=r or not utc(reg['started_at'])<=utc(receipt['received_at'])<=now_utc():raise ValueError('repair receipt differs')
        if status['status']=='observed':
            if filename not in members:raise ValueError('missing repair receipt')
            rows=probe.rows_for(r,receipt['data'])
            if status['rows']!=len(rows):raise ValueError('repair row count differs')
            count+=1
            for day,values in rows.items():evidence[(r['code'].split('.')[0],day)].append({'provider':r['provider'],'request_code':r['code'],'date':day,'values':values,
                'receipt_file':'repair/'+filename,'receipt_sha256':members[filename],'received_at':receipt['received_at']})
    return members,evidence,{'requested':len(requests),'observed':count,'failed':len(requests)-count,'original_failed_receipts_preserved':True}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();capture(a.source,a.output)
