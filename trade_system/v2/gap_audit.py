"""Read-only snapshot gap discovery and immutable research adjudication packages."""
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
import json
import math
from pathlib import Path
import shutil

import duckdb

from .domain import identity, now_utc, utc
from .gap_evidence import adjudicate, read_evidence, write_json
from .portfolio_experiment import read_portfolio_result
from .rolling_research import file_hash


def implementation():
    return {name: file_hash(Path(__file__).with_name(name)) for name in ('gap_audit.py', 'gap_evidence.py', 'domain.py')}


def seal(folder, binding):
    folder = Path(folder).resolve()
    manifest = {'binding': binding, 'implementation': implementation(),
                'artifact_hashes': {p.relative_to(folder).as_posix(): file_hash(p)
                                   for p in folder.rglob('*') if p.is_file()}}
    write_json(folder/'completed.json', {**manifest, 'manifest_id': identity(manifest)})


def read_package(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder/'completed.json').read_text(encoding='utf-8'))
    key = manifest.pop('manifest_id')
    if identity(manifest) != key or manifest['implementation'] != implementation():
        raise ValueError('gap package manifest or implementation changed')
    actual = {}
    for path in folder.rglob('*'):
        if path.is_file() and path != folder/'completed.json':
            if path.is_symlink() or folder not in path.resolve().parents:
                raise ValueError('gap artifact escapes package')
            actual[path.relative_to(folder).as_posix()] = file_hash(path)
    if actual != manifest['artifact_hashes']:
        raise ValueError('gap package member/checksum mismatch')
    return json.loads((folder/'report.json').read_text(encoding='utf-8'))


def potential_windows(bundle, policy):
    """All preselected potential holdings, including dates after stopped accounts.

    This is coverage auditing, NOT replaying trades on a surviving subset.
    """
    windows, days = [], bundle['calendar']
    for name, predictions in bundle['predictions'].items():
        grouped = defaultdict(list)
        for day, code, score in predictions:
            grouped[day].append((code, score))
        for day, scores in grouped.items():
            start = days.index(day) + 1
            for code, _ in sorted(scores, key=lambda row: (-row[1], row[0]))[:policy['top_k']]:
                windows.append({'variant': name, 'signal_date': day, 'instrument': bundle['identity_map'][code],
                                'days': days[start:start+policy['hold_sessions']]})
    return windows


def _scalar(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return {'invalid_nonfinite': str(value)}
    return value


def _query(con, sql, args):
    cursor = con.execute(sql, args)
    names = [col[0] for col in cursor.description]
    rows = cursor.fetchmany(101)
    if len(rows) > 100:
        raise ValueError('case source row budget exceeded; refine exact identity')
    return [{name: _scalar(v) for name, v in zip(names, row)} for row in rows]


def _observations(con, code, day, tables):
    exchange, digits = code.split('.')
    ts_code = digits + '.' + exchange
    result = {}
    specs = {
        'tushare_daily': ('''SELECT ts_code,date,open,high,low,close,adjustment,provider,fetched_at FROM tushare_daily
                            WHERE ts_code=? AND stock_code=? AND date=?''', [ts_code, digits, day]),
        'tushare_adj_factor': ('''SELECT ts_code,date,adj_factor,fetched_at FROM tushare_adj_factor
                                WHERE ts_code=? AND stock_code=? AND date=?''', [ts_code, digits, day]),
        'kline': ('''SELECT date,stock_code,open,high,low,close,ktype,provider,adjustment,volume_unit,fetched_at,
                     raw_json IS NOT NULL AS raw_payload_present FROM kline WHERE stock_code=? AND date=?''', [digits, day]),
        'multi_source_kline': ('''SELECT source_date,asset_code,asset_type,open,close,provider,adjustment,is_stale,
                     source_event_time,collected_at,fetched_at,raw_json IS NOT NULL AS raw_payload_present
                     FROM multi_source_kline WHERE asset_code IN (?,?,?) AND source_date=?''', [digits, code, ts_code, day]),
        'tushare_gap_status': ('''SELECT data_kind,code,start_date,end_date,status,source_table,updated_at
                     FROM tushare_gap_status WHERE code IN (?,?,?) AND start_date<=? AND end_date>=?''', [digits, code, ts_code, day, day]),
        'baostock_history_checkpoint': ('''SELECT source,dataset,stock_code,start_date,end_date,status,rows_written,updated_at
                     FROM baostock_history_checkpoint WHERE stock_code IN (?,?,?) AND start_date<=? AND end_date>=?''', [digits, code, ts_code, day, day])}
    for table, (sql, params) in specs.items():
        result[table] = {'table_present': table in tables,
                         'rows': _query(con, sql, params) if table in tables else []}
    return result


def audit_portfolio(portfolio, snapshot, output, *, clock=now_utc):
    portfolio, snapshot, output = (Path(p).resolve() for p in (portfolio, snapshot, output))
    if output.exists():
        raise FileExistsError('new gap audit directory required')
    result = read_portfolio_result(portfolio)
    registration = json.loads((portfolio/'registration.json').read_text(encoding='utf-8'))
    digest = file_hash(snapshot)
    if digest != registration['snapshot_sha256']:
        raise ValueError('audit requires the exact registered source snapshot')
    bundle = json.loads((portfolio/'inputs.json').read_text(encoding='utf-8'))
    bars = {(r['instrument'], r['date']): r for r in bundle['bars']}
    reasons, affected = defaultdict(set), defaultdict(set)
    windows = potential_windows(bundle, registration['policy'])
    wanted = set()
    for window in windows:
        entry_factor = None
        for i, day in enumerate(window['days']):
            key = (window['instrument'], day)
            wanted.add(key)
            affected[key].add(window['variant'])
            bar = bars.get(key)
            if not bar:
                reasons[key].add('missing_frozen_raw_bar')
                continue
            if bar['factor'] is None:
                reasons[key].add('missing_frozen_factor')
            if not bar['open_fen'] or not bar['close_fen']:
                reasons[key].add('missing_or_zero_raw_price')
            if i == 0:
                entry_factor = bar['factor']
            elif entry_factor is not None and bar['factor'] is not None and Decimal(entry_factor) != Decimal(bar['factor']):
                reasons[key].add('potential_held_factor_change_requires_action_ledger')
    for name, account in result['accounts'].items():
        if account['blocker']:
            b = account['blocker']
            key = (b['instrument'], b['date'])
            reasons[key].add('actual_account_blocker:' + b['reason'])
            affected[key].add(name)
    if len(reasons) > 500:
        raise ValueError('bounded gap case budget exceeded')
    cases = []
    with duckdb.connect(str(snapshot), read_only=True) as con:
        tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
        for (code, day), gaps in sorted(reasons.items()):
            case = {'instrument': code, 'date': day, 'reasons': sorted(gaps), 'affected_variants': sorted(affected[(code, day)]),
                    'parent_registration_id': result['registration_id']}
            case['case_id'] = identity(case)
            facts = _observations(con, code, day, tables)
            raw, factors = facts['tushare_daily']['rows'], facts['tushare_adj_factor']['rows']
            case.update(observations=facts, classification='unknown', can_resume_portfolio=False,
                        source_diagnosis={'primary_raw_rows': len(raw), 'primary_factor_rows': len(factors),
                            'distinction': 'raw_bar_missing_factor_exists' if not raw and factors else
                                          'both_raw_bar_and_factor_missing' if not raw and not factors else
                                          'raw_bar_exists_factor_missing' if raw and not factors else 'requires_field_or_action_review'},
                        limitations=['absence is not suspension, delisting or successful acquisition',
                                     'alternate tables are unverified observations, not certified independent sources',
                                     'legacy naive fetched_at is not historical availability evidence'])
            cases.append(case)
    if file_hash(snapshot) != digest:
        raise ValueError('source snapshot changed during audit')
    output.mkdir(parents=True, exist_ok=False)
    report = {'scope': 'historical_gap_evidence_audit_not_execution', 'created_at': utc(clock()).isoformat(),
              'parent_portfolio': str(portfolio), 'parent_registration_id': result['registration_id'],
              'parent_completion_sha256': file_hash(portfolio/'run/completed.json'), 'snapshot_sha256': digest,
              'audited_potential_windows': len(windows), 'requested_bar_identities': len(wanted), 'cases': cases,
              'classification_counts': dict(Counter(c['classification'] for c in cases)),
              'execution_ready': False, 'signal_impact': 'disabled', 'portfolio_resumed': False}
    write_json(output/'report.json', report)
    lines = ['# 历史组合缺口证据案件', '', '只读冻结库；缺行不等于停牌，复权因子不能替代行情或公司行为。', '',
             '| 日期 | 证券 | 日线行数 | 因子行数 | 状态 |', '|---|---|---:|---:|---|']
    for c in cases:
        d = c['source_diagnosis']
        lines.append(f"| {c['date']} | {c['instrument']} | {d['primary_raw_rows']} | {d['primary_factor_rows']} | unknown |")
    lines += ['', '潜在窗口扫描包含账户停止后的输入覆盖，不是继续回测或存活样本收益。',
              '所有案件仍阻塞：需导入同花顺官方、xiaodefa TuShare 或其他明确来源的原始结构化响应及时间/版本；存在冲突不按优先级隐藏。']
    with (output/'review.md').open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')
    seal(output, {'parent_registration_id': result['registration_id'], 'snapshot_sha256': digest})
    return report


def adjudicate_audit(audit, evidence_folders, output, *, asof=None, mode='historical_repair', clock=now_utc):
    audit, output = Path(audit).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('new adjudication package required')
    report = read_package(audit)
    now = utc(clock())
    cutoff = utc(asof) if asof else now
    if cutoff > now:
        raise ValueError('future knowledge cutoff is not available evidence')
    if len(evidence_folders) > 1000:
        raise ValueError('bounded evidence package count exceeded')
    evidence = [read_evidence(folder) for folder in evidence_folders]
    decisions = [adjudicate(case, evidence, asof=cutoff, mode=mode) for case in report['cases']]
    output.mkdir(parents=True, exist_ok=False)
    archived = set()
    for folder, record in zip(evidence_folders, evidence):
        if record['evidence_id'] in archived:
            continue
        archived.add(record['evidence_id'])
        dest = output/'evidence'/record['evidence_id']
        dest.mkdir(parents=True)
        for filename in ('evidence.json', 'response.json'):
            shutil.copyfile(Path(folder)/filename, dest/filename)
        if read_evidence(dest) != record:
            raise ValueError('evidence changed while archiving')
    write_json(output/'audit.json', report)
    decision_report = {'scope': 'research_gap_adjudication_candidates_only', 'created_at': now.isoformat(),
        'audit_manifest_sha256': file_hash(audit/'completed.json'), 'decisions': decisions,
        'classification_counts': dict(Counter(d['classification'] for d in decisions)),
        'execution_ready': False, 'signal_impact': 'disabled', 'portfolio_resumed': False,
        'candidate_application': 'disabled_requires_independent_acceptance_and_new_experiment'}
    write_json(output/'report.json', decision_report)
    seal(output, {'audit_manifest_sha256': decision_report['audit_manifest_sha256'], 'asof': cutoff.isoformat(), 'mode': mode})
    return decision_report
