

"""Retained CNInfo adapters; acquisition policy is owned by resilient_sources."""
from trade_system.http_transport import (read_verified_once, request_budget, request_deadline,
    diagnostic_budget, measure_requests, stop_diagnostic, wire_request_budget)

import json
import os
import subprocess
import time
import sys
import hashlib
import math
import re
import threading
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
import urllib.request as _u
import urllib.error as _e
import urllib.parse as _up
from trade_system.adapters.kline_sources import UA
from trade_system.adapters._shared import _f


_CNINFO_ORGID_MAP = {}


class CninfoPaginationCircuit:
    """Shared, conservative two-send boundary for one explicitly approved round.

    A protocol failure closes the whole round, including a later security. This
    is not a bulk collector: a new circuit never supplies permission or budget.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._slots = 0
        self.stopped = None

    def reserve(self):
        with self._lock:
            if self.stopped:
                raise ValueError('CNINFO round stopped: '+self.stopped)
            if self._slots >= 2:
                self.stopped = 'request_budget_exhausted'
                raise ValueError('CNINFO round stopped: '+self.stopped)
            self._slots += 1

    def stop(self, reason):
        with self._lock:
            self.stopped = self.stopped or reason


def cninfo_pagination_plan(ts_code, org_id, window_from, window_through):
    """Pure fixed-form plan. No org lookup, provider call or qualification."""
    if not isinstance(ts_code, str) or not re.fullmatch(r'\d{6}\.(SZ|SH)', ts_code):
        raise ValueError('CNINFO diagnostic needs one explicit SZ/SH security')
    if not isinstance(org_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', org_id):
        raise ValueError('retained explicit official org_id required')
    start, through = date.fromisoformat(window_from), date.fromisoformat(window_through)
    if start.isoformat() != window_from or through.isoformat() != window_through or start > through:
        raise ValueError('explicit ordered ISO date window required')
    headers = {'User-Agent': UA, 'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://www.cninfo.com.cn/new/disclosure',
        'Origin': 'https://www.cninfo.com.cn',
        'Content-Type': 'application/x-www-form-urlencoded'}
    requests = []
    for number in (1, 2):
        params = {'stock': ts_code[:6]+','+org_id, 'tabName': 'fulltext',
            'column': {'SZ': 'szse', 'SH': 'sse'}[ts_code[-2:]], 'pageSize': '30',
            'pageNum': str(number), 'seDate': window_from+'~'+window_through,
            'isHLtitle': 'false'}
        body = _up.urlencode(params)
        requests.append(dict(method='POST', url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
            headers=headers.copy(), params=params, body_utf8=body,
            body_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest()))
    return dict(schema='cninfo_two_page_protocol_plan_v1', ts_code=ts_code, org_id=org_id,
        window_from=window_from, window_through=window_through, requests=requests,
        max_actual_requests=2, max_concurrency=1, request_seconds=20,
        total_seconds=60, max_response_bytes=4_000_000, retries=0, redirects=0,
        pdf_downloads=0, org_lookup_requests=0, production_writes=0,
        only_changed_request_variable='pageNum', financial_qualification=False,
        live_execution_requires_new_explicit_budget_approval=True)


def _cninfo_protocol_page(raw, plan, number, expected_total, seen):
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError('invalid_response_shape')
    # Business failures can arrive as HTTP 200. Preserve bytes, close the round.
    if body.get('code') not in (None, 0, '0', 200, '200') or body.get('success') is False:
        raise ValueError('business_rejected')
    total, more, rows = body.get('totalAnnouncement'), body.get('hasMore'), body.get('announcements')
    if type(total) is not int or total < 0 or type(more) is not bool or not isinstance(rows, list):
        raise ValueError('invalid_pagination_metadata')
    if expected_total is not None and total != expected_total:
        raise ValueError('catalogue_total_changed')
    expected_rows = min(30, max(0, total-(number-1)*30))
    if len(rows) != expected_rows or more != (total > number*30):
        raise ValueError('pagination_count_or_terminal_mismatch')
    identities = []
    for row in rows:
        if (not isinstance(row, dict) or row.get('secCode') != plan['ts_code'][:6]
                or row.get('orgId') != plan['org_id']):
            raise ValueError('announcement_identity_mismatch')
        identity = row.get('announcementId')
        if type(identity) not in (str, int) or not str(identity):
            raise ValueError('announcement_id_missing')
        stamp = row.get('announcementTime')
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise ValueError('announcement_date_missing')
        day = datetime.fromtimestamp(stamp/1000, timezone(timedelta(hours=8))).date().isoformat()
        if not plan['window_from'] <= day <= plan['window_through']:
            raise ValueError('announcement_outside_requested_window')
        identities.append(str(identity))
    if len(set(identities)) != len(identities) or seen.intersection(identities):
        raise ValueError('duplicate_announcement_page')
    return total, more, identities


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate plan field')
        result[key] = value
    return result


def diagnose_cninfo_pagination(plan_path, plan_sha256, output_directory, *, circuit=None):
    """Explicit isolated acquisition; caller must validate the new approval.

    SHA binding selects the reviewed request, not proof of human approval. The
    CLI has no scheduler integration, retries, PDF download or database writer.
    Raw successful bodies and exact uploaded form bytes are never normalized.
    """
    path = Path(plan_path)
    if path.stat().st_size > 64_000 or not re.fullmatch(r'[0-9a-f]{64}', plan_sha256 or ''):
        raise ValueError('bounded plan and exact reviewed SHA256 required')
    raw_plan = path.read_bytes()
    if hashlib.sha256(raw_plan).hexdigest() != plan_sha256:
        raise ValueError('reviewed CNINFO plan changed')
    plan = json.loads(raw_plan, object_pairs_hook=_unique_json_pairs)
    if not isinstance(plan, dict):
        raise ValueError('CNINFO plan object required')
    expected = cninfo_pagination_plan(plan['ts_code'], plan['org_id'], plan['window_from'], plan['window_through'])
    if json.dumps(plan, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError('CNINFO plan differs from fixed two-page protocol')
    out = Path(output_directory).resolve()
    out.mkdir(parents=True, exist_ok=False)  # Never overwrite a previous round.
    (out/'reviewed-request-plan.json').write_bytes(raw_plan)
    circuit = circuit or CninfoPaginationCircuit()
    result = dict(schema='cninfo_two_page_protocol_result_v1', plan_sha256=plan_sha256,
        ts_code=plan['ts_code'], window_from=plan['window_from'], window_through=plan['window_through'],
        status='not_started', protocol_verified=False, catalogue_complete=False,
        financial_qualification=False, production_writes=0, actual_requests=0,
        pdf_downloads=0, org_lookup_requests=0, requests=[], source_pages=[], stopped=None)
    total, seen = None, set()
    deadline = time.monotonic()+plan['total_seconds']
    with request_budget(plan['total_seconds']), wire_request_budget(2) as budget, diagnostic_budget() as diagnosis:
        for number, entry in enumerate(plan['requests'], 1):
            record = dict(page_number=number, status='not_dispatched', reused=False,
                request_path=str(out/f'page-{number}-request.json'),
                request_body_path=str(out/f'page-{number}-request-body.txt'),
                response_path=str(out/f'page-{number}-response.json'),
                response_body_available=False)
            result['requests'].append(record)
            try:
                remaining = min(20, deadline-time.monotonic(), request_deadline.get()-time.monotonic())
                if remaining <= 0:
                    raise TimeoutError('round_deadline_exhausted')
                circuit.reserve()
                request = _u.Request(entry['url'], data=entry['body_utf8'].encode('utf-8'),
                    headers=entry['headers'], method='POST')
                exact = dict(entry, headers=dict(request.header_items()),
                    fingerprint_scope='explicit_request_fields_and_form_bytes_not_transport_added_headers')
                request_raw = (json.dumps(exact, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf-8')
                Path(record['request_path']).write_bytes(request_raw)
                Path(record['request_body_path']).write_bytes(request.data)
                record.update(request_sha256=hashlib.sha256(request_raw).hexdigest(),
                    body_sha256=entry['body_sha256'], dispatched_at=datetime.now(timezone.utc).isoformat(),
                    max_seconds=remaining, status='outcome_unknown')
                with measure_requests() as measured:
                    try:
                        response_raw = read_verified_once(request, timeout=remaining, max_bytes=4_000_000)
                    finally:
                        record['transport_receipts'] = measured
                metadata = measured[-1] if measured else {}
                record.update(received_at=datetime.now(timezone.utc).isoformat(),
                    http_status=metadata.get('http_status'),
                    response_headers=metadata.get('response_headers', []),
                    response_headers_sha256=metadata.get('response_headers_sha256'),
                    response_sha256=hashlib.sha256(response_raw).hexdigest(),
                    response_bytes=len(response_raw), response_body_available=True, status='received')
                Path(record['response_path']).write_bytes(response_raw)
                if record['http_status'] != 200:
                    raise ValueError('actual_http_200_evidence_missing')
                total, more, identities = _cninfo_protocol_page(response_raw, plan, number, total, seen)
                seen.update(identities)
                result['source_pages'].append(dict(path=record['response_path'],
                    sha256=record['response_sha256'], http_status=record['http_status'], params=entry['params'].copy(),
                    received_at=record['received_at'], request_sha256=record['request_sha256'],
                    returned_rows=len(identities), announcement_ids=identities))
                record['pagination_valid'] = True
                result.update(total_announcements=total, unique_announcements=len(seen))
                if not more:
                    result['catalogue_complete'] = len(seen) == total
                if number == 1 and not more:
                    result.update(status='single_page_no_pagination_proof', stopped='no_second_page_available')
                    circuit.stop('no_second_page_available')
                    break
                if number == 2:
                    result.update(status='pagination_verified', protocol_verified=True)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__
                if isinstance(exc, _e.HTTPError):
                    reason = 'http_'+str(exc.code)
                # Close the shared round before any further body read or local
                # evidence write can fail inside this exception handler.
                circuit.stop(reason)
                if not diagnosis['stopped']:
                    stop_diagnostic('business_rejected')
                result.update(status='stopped', stopped=circuit.stopped)
                result['failure_category'] = diagnosis['stopped']
                metadata = record.get('transport_receipts', [])
                if metadata:
                    record.update(http_status=metadata[-1].get('http_status'),
                                  response_headers=metadata[-1].get('response_headers', []),
                                  response_headers_sha256=metadata[-1].get('response_headers_sha256'))
                if isinstance(exc, _e.HTTPError):
                    record['http_status'] = exc.code
                    unavailable = getattr(exc, 'response_body_unavailable', False) or (
                        metadata and metadata[-1].get('response_body_unavailable') is True)
                    if not unavailable and exc.fp is not None:
                        try:
                            failed_raw = exc.read(4_000_001)
                            if len(failed_raw) <= 4_000_000:
                                Path(record['response_path']).write_bytes(failed_raw)
                                record.update(received_at=datetime.now(timezone.utc).isoformat(),
                                    response_sha256=hashlib.sha256(failed_raw).hexdigest(),
                                    response_bytes=len(failed_raw), response_body_available=True)
                        except Exception as capture_error:
                            record['response_body_capture_error_type'] = type(capture_error).__name__
                record.update(status='rejected' if record['response_body_available'] else 'failed', error=reason)
                if not record['response_body_available']:
                    record['missing_response_reason'] = 'transport_did_not_return_a_retained_body'
                break
            finally:
                result['actual_requests'] = budget['attempts']
                Path(out/f'page-{number}-receipt.json').write_text(
                    json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    result['round_closed'] = True
    circuit.stop('round_closed')
    result['further_requests_authorized'] = False
    result['source_manifest_schema'] = 'official_disclosure_page_set_v1'
    manifest = dict(schema='official_disclosure_page_set_v1', ts_code=plan['ts_code'],
        source_pages=result['source_pages'], catalogue_complete=result['catalogue_complete'])
    (out/'catalogue-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (out/'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return result

def _cninfo_ts_to_date(ts):
    if isinstance(ts, (int, float)):
        return __import__("datetime").datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    return str(ts)[:10] if ts else ""

def _cninfo_orgid(code):
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            r = read_verified_once(_u.Request("https://www.cninfo.com.cn/new/data/szse_stock.json",
                                      headers={"User-Agent": UA}), timeout=15, max_bytes=4_000_000)
            _CNINFO_ORGID_MAP = {s["code"]: s["orgId"] for s in json.loads(r).get("stockList", [])}
        except Exception:
            pass
    org = _CNINFO_ORGID_MAP.get(code)
    if isinstance(org, str) and org:
        return org
    return None  # A guessed issuer is not an official identity.

@request_budget(15)
def _from_cninfo_announcements(code, page_size=30):
    org_id = _cninfo_orgid(code)
    if not org_id:
        return None
    payload = {"stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": str(page_size),
               "pageNum": "1", "column": "", "category": "", "plate": "", "seDate": "",
               "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}
    try:
        req = _u.Request("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                         data=_up.urlencode(payload).encode(),
                         headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                                  "Referer": "https://www.cninfo.com.cn/new/disclosure",
                                  "Origin": "https://www.cninfo.com.cn"}, method="POST")
        d = json.loads(read_verified_once(req, timeout=15, max_bytes=4_000_000))
        rows = d.get('announcements') if isinstance(d, dict) else None
        if (not isinstance(rows, list) or any(not isinstance(it, dict)
                or it.get('secCode') != code or it.get('orgId') != org_id for it in rows)):
            return None
    except Exception:
        return None
    out = [{"id": str(it.get("announcementId") or ""), "title": it.get("announcementTitle", ""), "type": it.get("announcementTypeName", ""),
            "date": _cninfo_ts_to_date(it.get("announcementTime")),
            "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={it.get('announcementId', '')}"}
           for it in rows]
    return out or None

@request_budget(10)
def _from_cninfo_irm(code, page_size=30):
    try:
        r1 = read_verified_once(_u.Request("https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
                                   data=_up.urlencode({"keyWord": code}).encode(),
                                   headers={"User-Agent": UA}, method="POST"), timeout=10, max_bytes=4_000_000)
        d1 = json.loads(r1).get("data") or []
        if not d1:
            return None
        org_id = d1[0].get("secid")
        from datetime import datetime
        params = {"_t": 1, "stockcode": code, "orgId": org_id, "pageSize": page_size,
                  "pageNum": 1, "keyWord": "", "startDay": "", "endDay": ""}
        r2 = read_verified_once(_u.Request("https://irm.cninfo.com.cn/newircs/company/question?" + _up.urlencode(params),
                                   headers={"User-Agent": UA}, method="POST"), timeout=10, max_bytes=4_000_000)
        rows = json.loads(r2).get("rows") or []
    except Exception:
        return None
    out = []
    for it in rows:
        pd_ = it.get("pubDate")
        out.append({"code": it.get("stockCode"), "company": it.get("companyShortName"),
                    "question": it.get("mainContent"), "answer": it.get("attachedContent"),
                    "answerer": it.get("attachedAuthor"),
                    "ask_time": datetime.fromtimestamp(pd_ / 1000).strftime("%Y-%m-%d %H:%M") if pd_ else ""})
    return out or None

@request_budget(10)
def _cninfo_enckey():
    p = os.environ.get("CNINFO_JS")
    if not p:
        cand = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cninfo.js")
        p = cand if os.path.exists(cand) else None
    if not p or not os.path.exists(p):
        return None
    try:
        if p.endswith(".js"):
            remaining = request_deadline.get() - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('CNINFO token deadline exhausted')
            out = subprocess.run(['node', '-e',
                "const fs=require('fs');eval(fs.readFileSync(process.argv[1],'utf8'));"
                "process.stdout.write(String(getResCode1()).slice(0,4097))", p],
                capture_output=True, timeout=remaining,
                creationflags=0x08000000 if sys.platform == 'win32' else 0)
            if out.returncode or len(out.stdout) > 4096:
                return None
            return out.stdout.decode('utf-8').strip() or None
        with open(p, encoding='utf-8') as fh:
            token = fh.read(4097)
        return token.strip() or None if len(token) <= 4096 else None
    except subprocess.TimeoutExpired:
        raise TimeoutError('CNINFO token deadline exhausted') from None
    except TimeoutError:
        raise
    except Exception:
        return None


@request_budget(15)
def _cninfo_webapi(api, params):
    """取 cninfo webapi 的 records；无令牌/失败返回 None。"""
    enc = _cninfo_enckey()
    if not enc:
        return None
    try:
        url = "https://webapi.cninfo.com.cn/api/sysapi/" + api
        req = _u.Request(url + "?" + _up.urlencode(params),
                         headers={"User-Agent": UA, "Accept-Enckey": enc,
                                  "Accept": "*/*", "Referer": "http://webapi.cninfo.com.cn/"},
                         method="GET")
        d = json.loads(read_verified_once(req, timeout=15, max_bytes=4_000_000))
    except Exception:
        return None
    return d.get("records") or d.get("data") or []

def _from_cninfo_dividend(code):
    """巨潮分红配股第二源（p_sysapi1139）。字段已按 akshare 实测命名映射。"""
    recs = _cninfo_webapi("p_sysapi1139", {"scode": code})
    if not recs:
        return None
    out = [{"report_date": r.get("F001V", ""), "impl_date": r.get("F006D", ""),
            "div_type": r.get("F044V", ""), "bonus_ratio": _f(r, "F011N"),
            "transfer_ratio": _f(r, "F010N"), "cash_div": _f(r, "F012N"),
            "record_date": r.get("F018D", ""), "ex_date": r.get("F020D", ""),
            "pay_date": r.get("F023D", ""), "desc": r.get("F007V", "")}
           for r in recs]
    return out or None

def _from_cninfo_lockup(code):
    """巨潮限售股解禁第二源（p_sysapi1011）。字段名上线前需用真实样本校准。"""
    recs = _cninfo_webapi("p_sysapi1011", {"scode": code})
    if not recs:
        return None
    out = [{"ann_date": r.get("F001D", r.get("DECLAREDATE", "")),
            "float_date": r.get("F002D", ""), "float_shares": _f(r, "F003N"),
            "float_ratio": _f(r, "F004N"), "holder": r.get("F005V", ""),
            "share_type": r.get("F006V", "")}
           for r in recs]
    return out or None

def _from_cninfo_block_trade(code):
    """巨潮大宗交易第二源（p_sysapi1009）。字段名上线前需用真实样本校准。"""
    recs = _cninfo_webapi("p_sysapi1009", {"scode": code})
    if not recs:
        return None
    out = [{"date": r.get("F001D", ""), "price": _f(r, "F002N"),
            "volume": _f(r, "F003N"), "amount": _f(r, "F004N"),
            "buyer": r.get("F005V", ""), "seller": r.get("F006V", "")}
           for r in recs]
    return out or None
