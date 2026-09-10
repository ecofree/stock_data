"""Bounded public-PDF evidence archive with explicitly manual transcriptions.

Hash verification proves retained bytes and derivation, not interpretation,
publication-time availability, independent authenticity, or account ownership.
"""
from pathlib import Path
from urllib.parse import urlparse

import requests

from .domain import identity, instrument, now_utc, utc
from .entitlements import dated, validate_terms
from .gap_audit import adjudicate_audit, read_package
from .gap_evidence import MAX_BYTES, ingest_response, read_evidence, read_json, sha, write_json
from .native_gap_run import verify_collection
from .rolling_research import file_hash


SCOPE = 'manual_announcement_research_supplement_not_account_authority'
HOSTS = {'static.cninfo.com.cn', 'dataclouds.cninfo.com.cn', 'disc.static.szse.cn', 'www.sse.com.cn'}
METHOD = 'assistant_manual_transcription_independent_review_pending'


def implementation():
    return {name: file_hash(Path(__file__).with_name(name)) for name in
            ('case_supplement.py', 'entitlements.py', 'gap_evidence.py', 'gap_audit.py', 'domain.py')}


def validate_document(doc):
    required = {'id', 'url', 'title', 'published_date', 'pages', 'instrument', 'date',
                'kind', 'value', 'interpretation', 'method'}
    if set(doc) != required or doc['method'] != METHOD:
        raise ValueError('explicit manual document contract required')
    if not isinstance(doc['id'], str) or not doc['id'].isascii() or not doc['id'].replace('-', '').isalnum():
        raise ValueError('safe document identity required')
    url = urlparse(doc['url'])
    if url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password or url.port not in (None,443) or url.fragment or url.query or not url.path.lower().endswith('.pdf'):
        raise ValueError('exact public HTTPS PDF allowlist required')
    for key in ('title', 'interpretation'):
        if not isinstance(doc[key], str) or not doc[key].strip():
            raise ValueError('document title and interpretation required')
    dated(doc['published_date'])
    dated(doc['date'])
    instrument(doc['instrument'])
    if not isinstance(doc['pages'], list) or not doc['pages'] or any(type(p) is not int or not 1 <= p <= 100 for p in doc['pages']):
        raise ValueError('explicit one-based evidence pages required')
    if doc['kind'] == 'session_status':
        if doc['value'] != {'status': 'suspended', 'coverage': 'full_session'}:
            raise ValueError('only exact case-day suspension transcription supported')
    elif doc['kind'] == 'entitlement_terms':
        terms = {**doc['value'], 'available_at': '2099-01-01T00:00:00+00:00', 'evidence_refs': [doc['url']]}
        validate_terms(terms)
        if terms['instrument'] != doc['instrument'] or terms['ex_date'] != doc['date']:
            raise ValueError('document and entitlement identity mismatch')
    else:
        raise ValueError('unsupported manual document claim')


def fetch_pdf(url):
    """One GET, no credentials, redirects, retries, or provider-rate-limit bypass."""
    with requests.get(url, headers={'User-Agent': 'stock-data-evidence/1'},
                      timeout=(5,15), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise ValueError('public document HTTP status: ' + str(response.status_code))
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError('public PDF exceeds evidence budget')
            chunks.append(chunk)
        raw = b''.join(chunks)
    if not raw.startswith(b'%PDF-'):
        raise ValueError('response is not a PDF')
    return raw


def _terms(doc, received, digest):
    return {**doc['value'], 'available_at': received,
            'evidence_refs': [doc['url']+'#sha256='+digest]}


def collect_supplement(plan, output, *, fetch=fetch_pdf, clock=now_utc):
    if set(plan) != {'scope', 'audit', 'native_parent', 'documents'} or plan['scope'] != SCOPE:
        raise ValueError('strict supplemental investigation plan required')
    docs = plan['documents']
    if not isinstance(docs, list) or not 1 <= len(docs) <= 4 or len({d['id'] for d in docs}) != len(docs):
        raise ValueError('one to four unique exact-case documents required')
    for doc in docs:
        validate_document(doc)
    audit, parent, output = Path(plan['audit']).resolve(), Path(plan['native_parent']).resolve(), Path(output).resolve()
    cases = read_package(audit)['cases']
    verify_collection(parent)
    targets = {(c['instrument'], c['date']) for c in cases}
    if any((d['instrument'], d['date']) not in targets for d in docs):
        raise ValueError('document expands frozen case scope')
    created = utc(clock()).isoformat()
    output.mkdir(parents=True, exist_ok=False)
    reg = {'plan': plan, 'created_at': created, 'max_requests': len(docs), 'retries': 0,
           'implementation': implementation(), 'audit_sha256': file_hash(audit/'completed.json'),
           'native_parent_sha256': file_hash(parent/'completed.json')}
    write_json(output/'registration.json', reg)
    evidence = sorted((parent/'adjudication/evidence').glob('*'))
    summaries = []
    try:
        for doc in docs:
            folder = output/doc['id']
            folder.mkdir()
            write_json(folder/'request.json', {'url': doc['url'], 'method': 'GET'})
            raw = fetch(doc['url'])
            if not raw.startswith(b'%PDF-') or len(raw) > MAX_BYTES:
                raise ValueError('invalid or oversized document bytes')
            received = utc(clock()).isoformat()
            if utc(received) < utc(created) or day_of(received) < doc['published_date']:
                raise ValueError('impossible receipt/publication ordering')
            with (folder/'document.pdf').open('xb') as stream:
                stream.write(raw)
            digest = sha(raw)
            record = {'document': doc, 'pdf_sha256': digest, 'available_at': received,
                      'publication_timestamp': None, 'independent_review': 'pending',
                      'extraction_is_automatically_verified': False, 'execution_ready': False}
            if doc['kind'] == 'session_status':
                structured = {'schema_version': 1, 'rows': [{k: doc[k] for k in ('instrument','date','kind','value')}]}
                write_json(folder/'structured.json', structured)
                receipt = {'schema_version': 1, 'source_family': 'other', 'source_api': doc['url'],
                    'origin_family': 'issuer_disclosure_public_pdf', 'reference': doc['url']+'#pages='+','.join(map(str,doc['pages'])),
                    'source_event_at': received, 'provider_received_at': received, 'license_status': 'unverified',
                    'row_index': 0, 'raw_sha256': file_hash(folder/'structured.json'),
                    'source_revision': METHOD+':pdf-sha256='+digest}
                write_json(folder/'receipt.json', receipt)
                imported = ingest_response(folder/'receipt.json', folder/'structured.json', folder/'evidence', clock=clock)
                evidence.append(folder/'evidence')
                record['evidence_id'] = imported['evidence_id']
            else:
                record['terms'] = _terms(doc, received, digest)
                record['classification'] = 'capitalization_schedule_candidate_not_account_receipt'
                record['account_blockers'] = ['record_close_eligible_lots_missing', 'registry_share_allocation_missing',
                    'account_delivery_and_restriction_missing', 'tax_basis_and_assessment_missing',
                    'parent_cost_transfer_integration_missing', 'independent_document_review_pending']
            write_json(folder/'record.json', record)
            summaries.append(record)
        decisions = adjudicate_audit(audit, evidence, output/'adjudication', clock=clock)
        result = {'scope': SCOPE, 'documents': summaries, 'public_pdf_requests': len(docs),
                  'classification_counts': decisions['classification_counts'], 'execution_ready': False,
                  'can_resume_portfolio': False, 'system_replay_available_from': 'per_record_local_receipt_not_publication_date'}
        write_json(output/'report.json', result)
        manifest = {'registration_id': identity(reg), 'artifact_hashes': {
            p.relative_to(output).as_posix(): file_hash(p) for p in output.rglob('*') if p.is_file()}}
        write_json(output/'completed.json', {**manifest, 'manifest_id': identity(manifest)})
        return result
    except BaseException as exc:
        write_json(output/'failed.json', {'error_type': type(exc).__name__, 'complete': False})
        raise


def day_of(at):
    from zoneinfo import ZoneInfo
    return utc(at).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()


def verify_supplement(folder):
    folder = Path(folder).resolve()
    manifest, _ = read_json(folder/'completed.json')
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid:
        raise ValueError('supplement manifest changed')
    actual = {}
    for path in folder.rglob('*'):
        if path.is_symlink() or folder not in path.resolve().parents:
            raise ValueError('supplement path escapes package')
        if path.is_file() and path != folder/'completed.json':
            actual[path.relative_to(folder).as_posix()] = file_hash(path)
    if actual != manifest['artifact_hashes']:
        raise ValueError('supplement member/hash mismatch')
    reg, _ = read_json(folder/'registration.json')
    if identity(reg) != manifest['registration_id'] or reg['implementation'] != implementation():
        raise ValueError('supplement registration/source changed')
    plan = reg['plan']
    if file_hash(Path(plan['audit'])/'completed.json') != reg['audit_sha256'] or file_hash(Path(plan['native_parent'])/'completed.json') != reg['native_parent_sha256']:
        raise ValueError('supplement parent binding changed')
    verify_collection(plan['native_parent'])
    read_package(plan['audit'])
    records = []
    for doc in plan['documents']:
        validate_document(doc)
        sub = folder/doc['id']
        record, _ = read_json(sub/'record.json')
        if record['document'] != doc or record['pdf_sha256'] != file_hash(sub/'document.pdf') or utc(record['available_at']) < utc(reg['created_at']):
            raise ValueError('document/receipt binding changed')
        if record['extraction_is_automatically_verified'] is not False or record['independent_review'] != 'pending' or record['execution_ready'] is not False or record['publication_timestamp'] is not None:
            raise ValueError('manual transcription cannot certify interpretation or execution')
        if doc['kind'] == 'session_status':
            imported = read_evidence(sub/'evidence')
            if imported['evidence_id'] != record['evidence_id'] or imported['claim'] != {k: doc[k] for k in ('instrument','date','kind','value')}:
                raise ValueError('transcribed claim derivation changed')
            if imported['receipt']['source_revision'] != METHOD+':pdf-sha256='+record['pdf_sha256']:
                raise ValueError('transcription not bound to PDF bytes')
        elif record['terms'] != _terms(doc, record['available_at'], record['pdf_sha256']):
            raise ValueError('entitlement transcription changed')
        records.append(record)
    decisions = read_package(folder/'adjudication')
    report, _ = read_json(folder/'report.json')
    if report['documents'] != records or report['classification_counts'] != decisions['classification_counts'] or report['execution_ready'] is not False or report['can_resume_portfolio'] is not False:
        raise ValueError('supplement report changed')
    return report
