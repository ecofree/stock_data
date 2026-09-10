"""Immutable bridge from completed rolling predictions to a daily-price diagnostic."""
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

import duckdb

from .domain import identity, number, utc
from .paper_ledger import rounded
from .portfolio_diagnostic import SCOPE, evaluate_portfolio, validate_policy
from .rolling_research import dump, file_hash, read_result


def source_hashes():
    return {name: file_hash(Path(__file__).with_name(name)) for name in
            ('portfolio_experiment.py', 'portfolio_diagnostic.py', 'paper_ledger.py', 'domain.py')}


def load_oos(experiment):
    """Verify the original code/runtime/features/completed manifest before reading CSVs."""
    root = Path(experiment).resolve()
    result = read_result(root)
    registration = json.loads((root/'registration.json').read_text(encoding='utf-8'))
    names = list(registration['plan']['variants'])
    if 'identity_hash_baseline' in names:
        raise ValueError('reserved deterministic selection baseline name')
    variants = {name: [] for name in names}
    expected_all = set()
    for fold in sorted((root/'run').glob('fold-*')):
        ids = json.loads((fold/'test_ids.json').read_text(encoding='utf-8'))
        expected = {tuple(row) for row in ids}
        if len(ids) != len(expected) or expected_all & expected:
            raise ValueError('duplicate OOS identity across folds')
        expected_all |= expected
        for name in names:
            with (fold/name/'predictions.csv').open(encoding='utf-8', newline='') as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != ['datetime', 'instrument', 'prediction']:
                    raise ValueError('prediction-only contract required')
                rows = [[r['datetime'], r['instrument'], float(r['prediction'])] for r in reader]
            if len(rows) != len(expected) or {(r[0], r[1]) for r in rows} != expected:
                raise ValueError('all variants must match frozen OOS identities')
            variants[name].extend(rows)
    if not expected_all:
        raise ValueError('no completed OOS folds')
    # A predeclared, label-free stock-selection control, not constant-label MSE.
    # Exact 52-bit integers remain representable in JSON/Python floats.
    variants['identity_hash_baseline'] = [[d, c, int(identity([d, c, 'portfolio-control-v1'])[:13], 16)]
                                          for d, c in sorted(expected_all)]
    return result, variants


def freeze_inputs(experiment, snapshot, policy, folder):
    validate_policy(policy)
    folder, snapshot, experiment = (Path(p).resolve() for p in (folder, snapshot, experiment))
    if folder.exists():
        raise FileExistsError('portfolio experiment must be new')
    result, predictions = load_oos(experiment)
    before = file_hash(snapshot)
    dates = sorted({row[0] for rows in predictions.values() for row in rows})
    codes = sorted({row[1] for rows in predictions.values() for row in rows})
    with duckdb.connect(str(snapshot), read_only=True) as con:
        raw_calendar = con.execute('''SELECT cal_date,is_open,pretrade_date FROM tushare_trade_cal
            WHERE exchange='SSE' AND cal_date>=? ORDER BY cal_date''', [dates[0]]).fetchall()
        calendar = [str(d) for d, opened, _ in raw_calendar if opened]
        if len(calendar) != len(set(calendar)) or any(day not in calendar for day in dates):
            raise ValueError('calendar duplicates or missing OOS session')
        last_index = calendar.index(dates[-1]) + policy['hold_sessions']
        if last_index >= len(calendar):
            raise ValueError('calendar does not cover liquidation horizon')
        calendar = calendar[:last_index+1]
        raw_calendar = [r for r in raw_calendar if str(r[0]) <= calendar[-1]]
        daily = [r[0] for r in raw_calendar]
        if not daily or len(daily) != len(set(daily)) or any(type(r[1]) is not bool for r in raw_calendar):
            raise ValueError('calendar duplicates or unknown session flags')
        calendar_gaps = [[str(a), str(b)] for a, b in zip(daily, daily[1:]) if b-a != timedelta(days=1)]
        missing_pretrade = []
        civil_dates = set(daily)
        previous = None
        for day, opened, pretrade in raw_calendar:
            if opened:
                if previous:
                    if pretrade is not None and pretrade != previous:
                        raise ValueError('calendar previous-session chain mismatch')
                    if pretrade is None:
                        # Explicit civil-date is_open records can independently
                        # establish adjacency; never guess weekdays/holidays.
                        span = (day-previous).days
                        if any(previous+timedelta(days=i) not in civil_dates for i in range(1, span+1)):
                            raise ValueError('calendar has neither complete civil rows nor a previous-session link')
                        missing_pretrade.append(str(day))
                previous = day
        pairs = con.execute('''SELECT DISTINCT stock_code,ts_code FROM tushare_daily
            WHERE date BETWEEN ? AND ? ORDER BY stock_code,ts_code''', [calendar[0], calendar[-1]]).fetchall()
        mapping, source_codes = {}, {}
        needed = set(codes)
        for code, ts_code in pairs:
            if code not in needed:
                continue
            match = re.fullmatch(r'([0-9]{6})\.(SH|SZ|BJ)', ts_code or '')
            if not match or match[1] != code:
                raise ValueError('explicit source exchange identity mismatch')
            value = match[2] + '.' + code
            if code in mapping and mapping[code] != value:
                raise ValueError('ambiguous source exchange identity')
            mapping[code], source_codes[code] = value, ts_code
        if set(mapping) != needed:
            raise ValueError('source does not map every frozen OOS identity')
        wanted = set()
        for rows in predictions.values():
            by_day = {}
            for d, c, score in rows:
                by_day.setdefault(d, []).append((c, score))
            for d, rows_for_day in by_day.items():
                start = calendar.index(d) + 1
                for c, _ in sorted(rows_for_day, key=lambda r: (-r[1], r[0]))[:policy['top_k']]:
                    for session in calendar[start:start+policy['hold_sessions']]:
                        wanted.add((session, c, source_codes[c]))
        # Temporary relation only; source database is opened read_only throughout.
        con.execute('CREATE TEMP TABLE requested(date DATE, stock_code VARCHAR, ts_code VARCHAR)')
        con.executemany('INSERT INTO requested VALUES (?,?,?)', sorted(wanted))
        raw = con.execute('''SELECT r.date,r.stock_code,d.open,d.close,d.adjustment,a.adj_factor,
                d.fetched_at,a.fetched_at,d.provider
            FROM requested r JOIN tushare_daily d ON r.date=d.date AND r.stock_code=d.stock_code AND r.ts_code=d.ts_code
            LEFT JOIN tushare_adj_factor a ON r.date=a.date AND r.stock_code=a.stock_code AND r.ts_code=a.ts_code
            ORDER BY r.date,r.stock_code''').fetchall()
    bars, seen = [], set()
    for d, code, opening, close, adjustment, factor, _, _, _ in raw:
        if (d, code) in seen or adjustment != 'none':
            raise ValueError('duplicate or non-raw daily/factor source')
        seen.add((d, code))
        bars.append({'date': str(d), 'instrument': mapping[code],
                     'open_fen': None if opening is None else rounded(number(opening)*100),
                     'close_fen': None if close is None else rounded(number(close)*100),
                     'factor': None if factor is None else str(number(factor))})
    if file_hash(snapshot) != before:
        raise ValueError('snapshot changed during extraction')
    folder.mkdir(parents=True, exist_ok=False)
    bundle = {'predictions': predictions, 'bars': bars, 'calendar': calendar, 'identity_map': mapping}
    dump(folder/'inputs.json', bundle)
    declaration = {'registered_at': utc(datetime.now().astimezone()).isoformat(), 'scope': SCOPE,
        'policy': policy, 'source_hashes': source_hashes(), 'input_sha256': file_hash(folder/'inputs.json'),
        'parent_experiment': str(experiment), 'parent_registration_id': result['registration_id'],
        'parent_completion_sha256': file_hash(experiment/'run/completed.json'),
        'snapshot_sha256': before, 'snapshot_path': str(snapshot),
        'requested_bar_identities': len(wanted), 'available_bars': len(bars),
        'calendar_quality': {'civil_gaps_linked_by_explicit_pretrade': calendar_gaps,
                             'null_pretrade_linked_by_complete_civil_rows': missing_pretrade,
                             'exchange_calendar_certified': False},
        'source_receipt_ranges': {name: [str(min(vals)), str(max(vals))] if vals else None for name, vals in
                                  [('daily', [r[6] for r in raw if r[6]]), ('factor', [r[7] for r in raw if r[7]])]},
        'source_providers': sorted({r[8] or 'unknown' for r in raw}),
        'declaration': 'Frozen before portfolio evaluation, after historical labels and OOS diagnostics were inspected. '
                       'SSE calendar shared across exchanges; daily open/close unconstrained; no orderbook evidence. '
                       'Legacy provider column is not proof of delivery API or licensed PIT receipt.'}
    declaration['registration_id'] = identity(declaration)
    dump(folder/'registration.json', declaration)
    return declaration


def _read_registration(folder):
    folder = Path(folder).resolve()
    record = json.loads((folder/'registration.json').read_text(encoding='utf-8'))
    key = record.pop('registration_id')
    if identity(record) != key or record['source_hashes'] != source_hashes():
        raise ValueError('portfolio registration/code mismatch')
    if record['input_sha256'] != file_hash(folder/'inputs.json'):
        raise ValueError('portfolio inputs changed')
    parent = Path(record['parent_experiment'])
    if file_hash(parent/'run/completed.json') != record['parent_completion_sha256']:
        raise ValueError('parent completion changed')
    result = read_result(parent)
    if result['registration_id'] != record['parent_registration_id']:
        raise ValueError('parent experiment changed')
    record['registration_id'] = key
    validate_policy(record['policy'])
    return record


def run_portfolio(folder):
    folder = Path(folder).resolve()
    record = _read_registration(folder)
    bundle = json.loads((folder/'inputs.json').read_text(encoding='utf-8'))
    out = folder/'run'
    out.mkdir(exist_ok=False)
    dump(out/'started.json', {'registration_id': record['registration_id']})
    try:
        accounts = {name: evaluate_portfolio(rows, bundle['bars'], bundle['calendar'], bundle['identity_map'], record['policy'])
                    for name, rows in bundle['predictions'].items()}
        if len({r['prediction_identity_sha256'] for r in accounts.values()}) != 1:
            raise ValueError('portfolio variants do not share OOS sample')
        comparable = all(r['period_complete'] for r in accounts.values())
        result = {'registration_id': record['registration_id'], 'scope': SCOPE, 'accounts': accounts,
                  'common_full_period_comparison_available': comparable,
                  'hypothetical_excess_over_cash': {k: v['hypothetical_return'] for k, v in accounts.items()} if comparable else None,
                  'cash_control': {'initial_cash_fen': record['policy']['initial_cash_fen'], 'return': '0', 'fees_fen': 0},
                  'execution_ready': False, 'signal_impact': 'disabled', 'champion_changed': False,
                  'actual_operator_return': None,
                  'paper_reconciliation': 'Synthetic fixture tests only; no historical bar was submitted as observed orderbook'}
        dump(out/'results.json', result)
        lines = ['# 冻结 OOS 组合诊断', '', '日线假设诊断，不是可执行回测、实际成交或正式选股优势。', '',
                 '| 变体 | 全区间完成 | 假设收益 | 假设成交笔数 | 阻塞 |', '|---|---|---|---:|---|']
        for name, account in accounts.items():
            lines.append(f"| {name} | {account['period_complete']} | {account['hypothetical_return']} | {len(account['fills'])} | {account['blocker']} |")
        lines += ['', '现金/股数/费用逐日结算；收益不是标签均值复利。缺失估值或公司行为时保留持仓并停止该账户，整段收益与回撤保持 null。',
                  '不同截断区间不作输赢比较。SSE 共享日历、统一整手/费率为声明假设，不认证实际规则；无盘口/停牌/封板/容量证据。',
                  '纸面账只在合成夹具上核对相同现金和费用，真实日线未进入纸面盘口接口；真实操作收益仍为 null。',
                  '', f"登记：{record['registration_id']}"]
        with (out/'review.md').open('x', encoding='utf-8') as stream:
            stream.write('\n'.join(lines) + '\n')
        completion = {'registration_id': record['registration_id'],
                      'artifact_hashes': {p.name: file_hash(p) for p in out.iterdir() if p.is_file()}}
        dump(out/'completed.json', {**completion, 'manifest_id': identity(completion)})
        return result
    except BaseException as exc:
        dump(out/'failed.json', {'error_type': type(exc).__name__, 'message': str(exc), 'partial_outputs_are_not_success': True})
        raise


def read_portfolio_result(folder):
    folder = Path(folder).resolve()
    record = _read_registration(folder)
    out = folder/'run'
    completion = json.loads((out/'completed.json').read_text(encoding='utf-8'))
    key = completion.pop('manifest_id')
    if identity(completion) != key or completion['registration_id'] != record['registration_id']:
        raise ValueError('portfolio completion mismatch')
    actual = {p.name: file_hash(p) for p in out.iterdir() if p.is_file() and p.name != 'completed.json'}
    if actual != completion['artifact_hashes']:
        raise ValueError('portfolio outputs changed')
    return json.loads((out/'results.json').read_text(encoding='utf-8'))
