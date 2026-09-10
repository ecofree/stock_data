"""Field-level manual disclosure candidates; never account receipts or prices.

The seal checks byte retention and reproducible transcription, NOT whether a
human interpretation is correct. New structured knowledge starts no earlier
than both the retained source receipt and this transcription registration.
"""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

from .case_supplement import HOSTS, METHOD, day_of, fetch_pdf
from .domain import identity, instrument, now_utc, number, utc
from .entitlements import dated
from .gap_evidence import MAX_BYTES, read_json, sha, write_json
from .historical_adapter import implementation as adapter_implementation, verify_adapter
from .rolling_research import file_hash


SCOPE = 'manual_disclosure_fields_not_historical_account_replay'
HALT = {'halt_start_date', 'halt_end_date_inclusive', 'resumption_date', 'terminal_date'}
DISTRIBUTION = {'record_date', 'ex_date', 'cash_pay_date', 'gross_cny_per_share',
                'bonus_shares_per_share', 'capitalization_shares_per_share',
                'share_delivery_date', 'share_listing_date', 'cash_payment_scope'}
NUMERIC = {'gross_cny_per_share', 'bonus_shares_per_share', 'capitalization_shares_per_share'}


def implementation():
    return {**adapter_implementation(), 'disclosure_fields.py': file_hash(Path(__file__))}


def validate_document(doc):
    if set(doc) != {'id', 'url', 'title', 'published_date', 'instrument', 'date', 'kind',
                    'archive_id', 'fields', 'notes', 'method'} or doc['method'] != METHOD:
        raise ValueError('strict manual field document required')
    if not isinstance(doc['id'], str) or not doc['id'].isascii() or not doc['id'].replace('-', '').isalnum():
        raise ValueError('safe document id required')
    if doc['archive_id'] is not None and (not isinstance(doc['archive_id'], str) or
            not doc['archive_id'].isascii() or not doc['archive_id'].replace('-', '').isalnum()):
        raise ValueError('safe archived document id required')
    url = urlparse(doc['url'])
    if (url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password or
            url.port not in (None, 443) or url.query or url.fragment or not url.path.lower().endswith('.pdf')):
        raise ValueError('exact public disclosure PDF URL required')
    for k in ('title', 'notes'):
        if not isinstance(doc[k], str) or not doc[k].strip():
            raise ValueError('explicit title and interpretation limits required')
    instrument(doc['instrument'])
    dated(doc['date'])
    dated(doc['published_date'])
    fields = doc['fields']
    allowed = HALT if doc['kind'] == 'halt' else DISTRIBUTION if doc['kind'] == 'distribution' else None
    if allowed is None or not isinstance(fields, dict) or set(fields) != allowed:
        raise ValueError('complete declared field set including unknowns required')
    for key, item in fields.items():
        if set(item) != {'value', 'pages', 'basis'} or not isinstance(item['basis'], str) or not item['basis'].strip():
            raise ValueError('per-field interpretation and one-based pages required')
        pages, value = item['pages'], item['value']
        if not isinstance(pages, list) or any(type(p) is not int or not 1 <= p <= 100 for p in pages) or len(set(pages)) != len(pages):
            raise ValueError('invalid evidence pages')
        if value is None:
            continue
        if not pages:
            raise ValueError('known field requires page provenance')
        if key == 'cash_payment_scope':
            if value != 'csdc_delegated_A_share_holders_not_self_distribution':
                raise ValueError('explicit limited payment scope required')
        elif key in NUMERIC:
            if not isinstance(value, str) or not 0 <= number(value) <= (100000 if key == 'gross_cny_per_share' else 100):
                raise ValueError('explicit bounded decimal string required')
        else:
            dated(value)
    values = {k:v['value'] for k,v in fields.items()}
    if doc['kind'] == 'halt':
        start, end, resume, terminal = (values[k] for k in ('halt_start_date','halt_end_date_inclusive','resumption_date','terminal_date'))
        if start != doc['date'] or (end and end < start) or (resume and (not end or resume <= end)) or (terminal and terminal < start):
            raise ValueError('exact case start and consistent explicit halt endpoints required')
    else:
        if values['ex_date'] != doc['date']:
            raise ValueError('distribution must match exact case ex date')
        if values['record_date'] and values['record_date'] >= doc['date']:
            raise ValueError('record close must precede ex date')
        for key in ('cash_pay_date','share_delivery_date','share_listing_date'):
            if values[key] and values[key] < doc['date']:
                raise ValueError('scheduled delivery cannot precede ex date')
        if values['cash_pay_date'] and values['cash_payment_scope'] is None:
            raise ValueError('cash schedule requires explicit beneficiary scope')
        if values['share_delivery_date'] and values['share_listing_date'] and values['share_listing_date'] < values['share_delivery_date']:
            raise ValueError('listing before delivery')


def _value_key(key, value):
    return str(number(value).normalize()) if key in NUMERIC else value


def stage_fields(parent, actions, records, *, repair_asof, replay_asof):
    repair, past = utc(repair_asof), utc(replay_asof)
    if past > repair:
        raise ValueError('knowledge cutoff exceeds repair cutoff')
    targets = {(c['instrument'], c['date']):c for c in parent['cases']}
    if len(targets) != len(parent['cases']):
        raise ValueError('duplicate parent case')
    for r in records:
        validate_document(r['document'])
        d = r['document']
        if (d['instrument'], d['date']) not in targets:
            raise ValueError('document expands frozen case scope')
        if utc(r['structured_available_at']) < utc(r['source_received_at']):
            raise ValueError('transcription cannot precede receipt')
    rows = []
    for c in parent['cases']:
        docs = [r for r in records if (r['document']['instrument'],r['document']['date']) == (c['instrument'],c['date'])]
        usable = [r for r in docs if utc(r['structured_available_at']) <= repair]
        by_field = {}
        for r in usable:
            for field, item in r['document']['fields'].items():
                if item['value'] is not None:
                    by_field.setdefault(field, []).append({'value':item['value'],'document_id':r['document']['id'],
                        'pdf_sha256':r['pdf_sha256'],'url':r['document']['url'],'pages':item['pages'],
                        'basis':item['basis'],'structured_available_at':r['structured_available_at']})
        conflicts = [k for k,v in by_field.items() if len({_value_key(k,x['value']) for x in v}) > 1]
        resolved = {k:v[0]['value'] for k,v in by_field.items() if k not in conflicts}
        native = [a for a in actions if a['case_id'] == c['case_id'] and a['source'] == 'native_cash_component']
        native_conflicts = []
        if 'gross_cny_per_share' in resolved:
            native_conflicts = [a['evidence_id'] for a in native if utc(a['available_at']) <= repair and
                number(a['terms']['gross_cny_per_share']) != number(resolved['gross_cny_per_share'])]
        missing = set(c['missing_source_fields'])
        mapped = []
        def satisfy(field):
            if field in missing:
                missing.remove(field)
                mapped.append(field)
        if 'record_date' in resolved:
            satisfy('record_date')
        if 'cash_pay_date' in resolved:
            satisfy('cash_pay_date')
        share_fields = ('bonus_shares_per_share','capitalization_shares_per_share')
        shares = None
        if all(k in resolved for k in share_fields):
            shares = sum(number(resolved[k]) for k in share_fields)
            satisfy('complete_share_component_declaration')
            if shares and 'share_delivery_date' not in resolved:
                missing.add('explicit_share_delivery_date')
            if shares and 'share_listing_date' not in resolved:
                missing.add('explicit_share_listing_date')
            if shares:
                missing.add('registry_fractional_allocation_if_needed')
        closed_halt = all(k in resolved for k in ('halt_start_date','halt_end_date_inclusive','resumption_date'))
        if closed_halt:
            satisfy('structured_exact_suspension_span')
            satisfy('structured_resumption_or_terminal_lifecycle')
        elif 'halt_start_date' in resolved:
            missing.add('explicit_halt_end_and_resumption_or_terminal_date')
        if conflicts or native_conflicts:
            missing.add('resolve_disclosure_or_native_component_conflict')
        if usable:
            missing.add('independent_field_transcription_review')
        rows.append({'case_id':c['case_id'],'instrument':c['instrument'],'date':c['date'],
            'window_ids':deepcopy(c['window_ids']),'field_candidates':by_field,
            'nonconflicting_disclosure_values':resolved,'conflicting_fields':sorted(conflicts),
            'native_cash_conflict_evidence_ids':native_conflicts,
            'new_document_ids':[r['document']['id'] for r in docs],
            'excluded_future_document_ids':[r['document']['id'] for r in docs if r not in usable],
            'historically_available_new_documents':[r['document']['id'] for r in docs if utc(r['structured_available_at']) <= past],
            'mapped_source_fields_pending_review':sorted(mapped),'remaining_source_fields':sorted(missing),
            'missing_declared_policies':deepcopy(c['missing_declared_policies']),
            'derive_only_after_new_replay':deepcopy(c['derive_only_after_new_replay']),
            'explicit_closed_halt_candidate':closed_halt,
            'total_new_shares_per_share':str(shares) if shares is not None else None,
            'native_cash_component_was_not_complete_action':bool(native and shares),
            'independent_review':'pending','can_apply_to_account':False})
    return {'scope':SCOPE,'repair_asof':repair.isoformat(),'replay_asof':past.isoformat(),
        'variant_counts':deepcopy(parent['variant_counts']),'potential_windows':parent['potential_windows'],
        'case_count':len(rows),'cases':rows,'new_documents':len(records),
        'document_kind_counts':dict(Counter(r['document']['kind'] for r in records)),
        'closed_halt_candidates':sum(c['explicit_closed_halt_candidate'] for c in rows),
        'mapped_fields_pending_review':sum(len(c['mapped_source_fields_pending_review']) for c in rows),
        'cases_with_conflicts':sum(bool(c['conflicting_fields'] or c['native_cash_conflict_evidence_ids']) for c in rows),
        'historically_available_new_documents':sum(len(c['historically_available_new_documents']) for c in rows),
        'parent_replay_classification_counts':deepcopy(parent['replay_classification_counts']),
        'emitted_parent_events':0,'historical_diagnostic_ready':False,'portfolio_resumed':False,
        'execution_ready':False,'portfolio_return':None,'signal_impact':'disabled'}


def render_review(report):
    lines = ['# 逐字段公告补证（人工转录，待独立复核）','',
        '保留原实验和全部候选窗口。公告计划不是到账；起始停牌日不等于完整停牌区间。',
        '字段已映射不等于案件解决；来源日期、当地接收时间、结构化可用时间分开。','',
        '| 证券 / 日期 | 非冲突公告字段 | 待补来源字段 | 冲突 |','|---|---|---|---|']
    for c in report['cases']:
        lines.append('| '+' | '.join([c['instrument']+' / '+c['date'],
            ', '.join(k+'='+str(v) for k,v in sorted(c['nonconflicting_disclosure_values'].items())),
            ', '.join(c['remaining_source_fields']),
            ', '.join(c['conflicting_fields']+c['native_cash_conflict_evidence_ids']) or '无已检测字段冲突'])+' |')
    lines += ['', '每个已知字段的 PDF SHA-256、URL、页码和人工解释保存在 report.json；hash 不能证明语义正确。',
        '未创建父账事件；日线假设诊断仍须新登记成交、估值、到账、税费、退出延长期政策。真实执行关闭。']
    return '\n'.join(lines)+'\n'


def _parents(plan):
    if set(plan) != {'scope','adapter','documents'} or plan['scope'] != SCOPE:
        raise ValueError('strict field supplement plan required')
    docs = plan['documents']
    if not isinstance(docs,list) or not 1 <= len(docs) <= 20 or len({d['id'] for d in docs}) != len(docs):
        raise ValueError('bounded unique documents required')
    if sum(d['archive_id'] is None for d in docs) > 7:
        raise ValueError('at most seven public PDF requests, no retries')
    adapter = Path(plan['adapter']).resolve()
    parent = verify_adapter(adapter)
    reg,_ = read_json(adapter/'registration.json')
    supplement = Path(reg['plan']['supplement']).resolve()
    actions = read_json(adapter/'action_candidates.json')[0]['rows']
    targets = {(c['instrument'],c['date']):c for c in parent['cases']}
    archived = {}
    for d in docs:
        validate_document(d)
        case = targets.get((d['instrument'],d['date']))
        if case is None or (d['kind'] == 'halt') != ('missing_raw_bar' in case['problems']):
            raise ValueError('document must target matching frozen case kind')
        if d['archive_id'] is not None:
            sub = supplement/d['archive_id']
            r,_ = read_json(sub/'record.json')
            if any(r['document'][k] != d[k] for k in ('url','title','published_date','instrument','date')):
                raise ValueError('archived source identity mismatch')
            raw = (sub/'document.pdf').read_bytes()
            if sha(raw) != r['pdf_sha256']:
                raise ValueError('archived PDF changed')
            archived[d['id']] = (raw,r['available_at'])
    return parent,actions,archived,file_hash(adapter/'completed.json')


def _record(doc, digest, received, created):
    if day_of(received) < doc['published_date']:
        raise ValueError('receipt precedes publication date')
    if doc['archive_id'] is None and utc(received) < utc(created):
        raise ValueError('new source receipt precedes collection')
    return {'document':doc,'pdf_sha256':digest,'source_received_at':utc(received).isoformat(),
        'structured_available_at':max(utc(received),utc(created)).isoformat(),
        'publication_timestamp':None,'independent_review':'pending','license_status':'unverified',
        'extraction_is_automatically_verified':False,'account_receipt_proven':False}


def build_fields(plan, output, *, fetch=fetch_pdf, clock=now_utc):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('new field evidence directory required')
    parent,actions,archived,binding = _parents(plan)
    created = utc(clock()).isoformat()
    if utc(created) < utc(parent['repair_asof']):
        raise ValueError('new transcription cannot predate parent staging')
    output.mkdir(parents=True,exist_ok=False)
    reg = {'plan':plan,'created_at':created,'adapter_sha256':binding,'implementation':implementation()}
    write_json(output/'registration.json',reg)
    records = []
    for doc in plan['documents']:
        if doc['id'] in archived:
            raw, received = archived[doc['id']]
        else:
            raw, received = fetch(doc['url']),utc(clock()).isoformat()
        if not raw.startswith(b'%PDF-') or len(raw) > MAX_BYTES:
            raise ValueError('invalid or oversized PDF; leave incomplete package unsealed')
        record = _record(doc,sha(raw),received,created)
        with (output/(doc['id']+'.pdf')).open('xb') as stream:
            stream.write(raw)
        records.append(record)
    finished = utc(clock()).isoformat()
    if any(utc(r['structured_available_at']) > utc(finished) for r in records):
        raise ValueError('collection clock moved backwards')
    report = stage_fields(parent,actions,records,repair_asof=finished,replay_asof=parent['replay_asof'])
    write_json(output/'records.json',{'finished_at':finished,'records':records})
    write_json(output/'report.json',report)
    (output/'review.md').write_text(render_review(report),encoding='utf-8')
    if _parents(plan)[-1] != binding:
        raise ValueError('source parent changed; incomplete package not sealed')
    manifest = {'registration_id':identity(reg),'artifact_hashes':{p.name:file_hash(p) for p in output.iterdir()}}
    write_json(output/'completed.json',{**manifest,'manifest_id':identity(manifest)})
    return report


def verify_fields(folder):
    folder = Path(folder).resolve()
    manifest,_ = read_json(folder/'completed.json')
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid:
        raise ValueError('field supplement manifest changed')
    paths = list(folder.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths) or {p.name:file_hash(p) for p in paths if p.name != 'completed.json'} != manifest['artifact_hashes']:
        raise ValueError('field supplement members changed')
    reg,_ = read_json(folder/'registration.json')
    if identity(reg) != manifest['registration_id'] or reg['implementation'] != implementation():
        raise ValueError('field supplement registration/source changed')
    parent,actions,archived,binding = _parents(reg['plan'])
    if binding != reg['adapter_sha256'] or utc(reg['created_at']) < utc(parent['repair_asof']):
        raise ValueError('field source parent or chronology changed')
    saved,_ = read_json(folder/'records.json')
    docs = reg['plan']['documents']
    if len(saved['records']) != len(docs):
        raise ValueError('field record count mismatch')
    expected_members = {'registration.json','records.json','report.json','review.md'} | {d['id']+'.pdf' for d in docs}
    if set(manifest['artifact_hashes']) != expected_members:
        raise ValueError('unexpected sealed member')
    records = []
    for doc, row in zip(docs,saved['records']):
        raw = (folder/(doc['id']+'.pdf')).read_bytes()
        if not raw.startswith(b'%PDF-') or len(raw) > MAX_BYTES:
            raise ValueError('invalid retained PDF')
        received = archived[doc['id']][1] if doc['id'] in archived else row['source_received_at']
        if doc['id'] in archived and raw != archived[doc['id']][0]:
            raise ValueError('archived source bytes changed')
        record = _record(doc,sha(raw),received,reg['created_at'])
        if row != record or utc(record['structured_available_at']) > utc(saved['finished_at']):
            raise ValueError('field transcription or knowledge time changed')
        records.append(record)
    expected = stage_fields(parent,actions,records,repair_asof=saved['finished_at'],replay_asof=parent['replay_asof'])
    if read_json(folder/'report.json')[0] != expected or (folder/'review.md').read_text(encoding='utf-8') != render_review(expected):
        raise ValueError('field supplement transformation not reproducible')
    return expected
