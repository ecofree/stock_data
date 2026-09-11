"""Effective-dated research candidate association, never an identity certification."""
import argparse
from collections import Counter
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2 import normalize_price_units as units
from tools.v2 import probe_identity_sources as probe
from trade_system.v2 import research_semantics as semantics
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import canonical, file_hash, identity, now_utc, number, utc
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_receipts import sealed

SCOPE = 'effective_dated_research_candidate_not_identity_PIT_or_execution_authority'
ALIAS = {'old': '300114', 'new': '302132', 'effective': '2025-02-17'}
MONEY_FIELDS = probe.API_FIELDS['moneyflow'][2:]


def separate(output, *inputs):
    output = Path(output).resolve()
    if output.exists() or any(output == Path(p).resolve() or output in Path(p).resolve().parents
                             or Path(p).resolve() in output.parents for p in inputs):
        raise ValueError('separate new output namespace required')
    return output


def observations(folder):
    folder = Path(folder); members = sealed(folder)
    reg = read_json(folder/'registration.json')[0]
    if reg['origin'] != 'native_and_relay' or reg['requests'] != probe.plan() or reg['execution_ready'] is not False:
        raise ValueError('bound real identity receipts required')
    rows = {}
    for i, request in enumerate(probe.plan()):
        status = read_json(folder/f'status-{i:02d}.json')[0]
        if type(status.get('index')) is not int or status['index'] != i:
            raise ValueError('exact request index required')
        if status['status'] != 'observed' or request['provider'] != 'xiaodefa_relay':
            continue
        name = f'receipt-{i:02d}.json'; receipt = read_json(folder/name)[0]
        if receipt['request'] != request or not utc(reg['received_after']) <= utc(receipt['received_at']) <= now_utc():
            raise ValueError('receipt request/time differs')
        parsed = probe.rows_for(request, receipt['data'])
        if type(status.get('rows')) is not int or status['rows'] != len(parsed):
            raise ValueError('receipt row count differs')
        for day, values in parsed.items():
            key = (request['api'], request['code'], day)
            if key in rows:
                raise ValueError('duplicate source code/session observation')
            rows[key] = {'values': values, 'source_code': request['code'], 'date': day,
                'provider': request['provider'], 'api': request['api'], 'receipt_file': name,
                'receipt_sha256': members[name], 'received_at': receipt['received_at']}
    return members, rows


def equal_prices(a, b):
    if a is None or b is None:
        return False
    return all(abs(number(a['values'][k])-number(b['values'][k])) <= number(t) for k, t in
        [('open', '0.00000001'), ('high', '0.00000001'), ('low', '0.00000001'),
         ('close', '0.00000001'), ('volume_shares', '0.000001'), ('turnover_cny', '.50')])


def assemble(prices, obs, alias):
    if {k: alias.get(k) for k in ALIAS} != ALIAS or not 1 <= len(prices) <= 128:
        raise ValueError('bounded explicit code-change candidate required')
    rows = []; seen = set(); used_money = set()
    for price in prices:
        original = price['original']; day = original['date']
        windows = [(start, end) for start, end in probe.WINDOWS if start <= day <= end]
        if len(windows) != 1: raise ValueError('price date outside registered observation windows')
        if original['stock_code'] != ALIAS['new'] or price['original_sha256'] != identity(original):
            raise ValueError('original price identity differs')
        if day in seen:
            raise ValueError('duplicate candidate entity/session')
        seen.add(day)
        active = ALIAS['old'] if day < ALIAS['effective'] else ALIAS['new']
        inactive = ALIAS['new'] if active == ALIAS['old'] else ALIAS['old']
        get = lambda api, code: obs.get((api, code+'.SZ', day))
        money = get('moneyflow', active); other = get('moneyflow', inactive)
        daily = get('daily', active); new_daily = get('daily', ALIAS['new'])
        factor = get('adj_factor', active); new_factor = get('adj_factor', ALIAS['new'])
        reasons = []
        if price['status'] != 'observed_row_unit_qualified': reasons.append('price_units_unqualified')
        if not equal_prices(daily, new_daily): reasons.append('effective_code_price_missing_or_conflicting')
        if factor is None or new_factor is None or number(factor['values']['adj_factor']) != number(new_factor['values']['adj_factor']):
            reasons.append('effective_code_factor_missing_or_conflicting')
        if other is not None: reasons.append('inactive_code_money_present_no_double_count')
        if money is None: reasons.append('effective_code_money_missing_no_fallback')
        elif any(money['values'][k] is None for k in MONEY_FIELDS): reasons.append('money_fields_incomplete')
        candidate = not reasons
        if candidate:
            source_key = (money['source_code'], day)
            if source_key in used_money: raise ValueError('source money reused')
            used_money.add(source_key)
        row = {'entity_key': 'research-code-change:SZ:300114:302132', 'date': day,
            'observation_window': list(windows[0]),
            'original_price_code': original['stock_code'], 'original_price_sha256': price['original_sha256'],
            'effective_code': active+'.SZ', 'effective_date': ALIAS['effective'],
            'alias_source': alias['source'], 'candidate_eligible': candidate, 'blocked_reasons': reasons,
            'price_units': {'volume': 'shares', 'turnover': 'CNY'},
            'volume_shares': price['volume_shares'], 'turnover_cny': price['turnover_cny'],
            'money_source': money, 'inactive_money_source': other,
            'price_identity_evidence': daily, 'factor_identity_evidence': factor,
            'money_amount_unit': 'CNY',
            'candidate_money_cny': {k: str(number(money['values'][k])*10000) for k in MONEY_FIELDS} if candidate else None,
            'identity_qualified': False, 'historical_PIT_qualified': False, 'research_ready': False,
            'execution_ready': False, 'production_cutover': False}
        row['record_id'] = identity(row); rows.append(row)
    return sorted(rows, key=lambda row: row['date'])


def payload(layer, receipts, db, policy):
    source_hash = file_hash(__file__); policy_hash = file_hash(policy)
    value, policy_meta = semantics.validate(policy)
    aliases = [a for a in value['aliases'] if {k: a[k] for k in ALIAS} == ALIAS]
    if len(aliases) != 1: raise ValueError('exact dated alias evidence required')
    layer_members = sealed(layer)
    verified = units.verify(layer, receipts, db)
    members, obs = observations(receipts)
    rows = assemble(verified['rows'], obs, aliases[0])
    counts = Counter(reason for row in rows for reason in row['blocked_reasons'])
    result = {'scope': SCOPE, 'source_sha256': source_hash,
        'policy_sha256': policy_hash, 'policy_manifest_id': policy_meta['manifest_id'],
        'policy_received_at': value['received_at'], 'alias': aliases[0],
        'alias_source_sha256': policy_meta['source_hashes'][aliases[0]['source']],
        'price_layer_manifest_id': identity(layer_members), 'receipt_manifest_id': identity(members),
        'database_sha256': verified['database_sha256'], 'rows': rows,
        'summary': {'price_rows': len(rows), 'candidate_rows': sum(r['candidate_eligible'] for r in rows),
            'blocked_rows': sum(not r['candidate_eligible'] for r in rows), 'blocked_reasons': dict(counts),
            'old_code_candidate_rows': sum(r['candidate_eligible'] and r['effective_code']=='300114.SZ' for r in rows),
            'new_code_candidate_rows': sum(r['candidate_eligible'] and r['effective_code']=='302132.SZ' for r in rows)},
        'join_policy': 'one_entity_day_one_effective_source_no_fallback_no_sum',
        'window_policy': 'two_disjoint_observation_windows_not_continuous_history',
        'label_policy': 'no_training_labels_created_or_changed', 'identity_qualified': False,
        'historical_PIT_qualified': False, 'research_ready': False, 'execution_ready': False,
        'production_cutover': False, 'new_production_rows': 0}
    if (sealed(layer) != layer_members or sealed(receipts) != members or file_hash(policy) != policy_hash
            or semantics.validate(policy)[1] != policy_meta or file_hash(__file__) != source_hash
            or file_hash(db) != verified['database_sha256']):
        raise ValueError('candidate inputs changed')
    return result


def build(layer, receipts, db, policy, output):
    output = separate(output, layer, receipts, db, policy)
    result = payload(layer, receipts, db, policy)
    output.mkdir(parents=True)
    write_json(output/'candidate.json', result); seal(output)
    return result['summary']


def verify(folder, layer, receipts, db, policy):
    folder = Path(folder); members = sealed(folder)
    if set(members) != {'candidate.json'}: raise ValueError('exact candidate membership required')
    result = read_json(folder/'candidate.json')[0]
    if result != payload(layer, receipts, db, policy) or sealed(folder) != members:
        raise ValueError('candidate differs from source replay')
    return result


def join_probe(rows):
    """Private in-memory SQL consumer, never attaches a source or production DB."""
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE prices(date VARCHAR PRIMARY KEY,original_sha VARCHAR UNIQUE)')
        con.execute('CREATE TABLE candidates(date VARCHAR PRIMARY KEY,original_sha VARCHAR UNIQUE,source_code VARCHAR,net_cny VARCHAR)')
        for row in rows:
            con.execute('INSERT INTO prices VALUES (?,?)', [row['date'], row['original_price_sha256']])
            if row['candidate_eligible']:
                con.execute('INSERT INTO candidates VALUES (?,?,?,?)', [row['date'], row['original_price_sha256'],
                    row['effective_code'], row['candidate_money_cny']['net_mf_amount']])
        joined = con.execute('SELECT p.date,p.original_sha,c.source_code,c.net_cny FROM prices p LEFT JOIN candidates c ON p.date=c.date AND p.original_sha=c.original_sha ORDER BY p.date').fetchall()
    expected = [(r['date'], r['original_price_sha256'], r['effective_code'] if r['candidate_eligible'] else None,
                 r['candidate_money_cny']['net_mf_amount'] if r['candidate_eligible'] else None) for r in sorted(rows, key=lambda r: r['date'])]
    if joined != expected: raise ValueError('candidate consumer differs')
    return {'source_rows': len(rows), 'joined_rows': len(joined),
        'nonnull_candidate_money_rows': sum(r[3] is not None for r in joined),
        'join_multiplication': len(joined)-len(rows), 'mismatches': 0,
        'persistent_source_writes': 0, 'research_ready': False, 'execution_ready': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['build', 'verify'])
    for name in ('layer', 'receipts', 'db', 'policy', 'output'): p.add_argument('--'+name, required=True)
    a = p.parse_args()
    if a.action == 'build': result = build(a.layer, a.receipts, a.db, a.policy, a.output)
    else:
        result = verify(a.output, a.layer, a.receipts, a.db, a.policy)
        result = {'summary': result['summary'], 'consumer': join_probe(result['rows'])}
    print(canonical(result))
