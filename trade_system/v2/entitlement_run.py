"""New-directory, source-bound entitlement scenarios with deterministic replay."""
from pathlib import Path

from .domain import identity
from .entitlements import POLICY, SCOPE, replay
from .gap_evidence import read_json, write_json
from .rolling_research import file_hash


def implementation():
    return {name: file_hash(Path(__file__).with_name(name)) for name in
            ('entitlements.py', 'entitlement_run.py', 'domain.py', 'paper_ledger.py', 'gap_evidence.py')}


def golden_scenario():
    terms = {'action_id': 'fixture-cash-bonus', 'instrument': 'SZ.000001', 'action_type': 'cash_and_bonus',
        'record_date': '2026-05-18', 'ex_date': '2026-05-19', 'cash_pay_date': '2026-05-20',
        'share_delivery_date': '2026-05-21', 'share_listing_date': '2026-05-22',
        'gross_cny_per_share': '0.025', 'shares_per_share': '0.4',
        'available_at': '2026-05-18T16:00:00+08:00', 'evidence_refs': ['synthetic-terms-not-real-security']}
    config = {'scope': SCOPE, 'policy': POLICY, 'terms': terms, 'snapshot': {
        'account_id': 'synthetic-only', 'asof': '2026-05-18T15:00:00+08:00',
        'available_at': '2026-05-18T16:00:00+08:00', 'basis': 'synthetic_record_close',
        'lots': [{'lot_id': 'a', 'instrument': 'SZ.000001', 'quantity': 100, 'cost_fen': 100001,
                  'acquired_at': '2026-04-01T10:00:00+08:00', 'restricted': False},
                 {'lot_id': 'b', 'instrument': 'SZ.000001', 'quantity': 200, 'cost_fen': 240000,
                  'acquired_at': '2026-04-02T10:00:00+08:00', 'restricted': True}]}}
    specs = [('ex', 19, {}), ('cash_payment', 20, {'net_fen': 720, 'withheld_fen': 30}),
             ('share_delivery', 21, {'by_lot': {'a': 40, 'b': 80}}), ('share_release', 22, {}),
             ('tax_assessment', 22, {'total_fen': 75, 'basis_ref': 'fixture-external-assessment'}),
             ('tax_payment', 22, {'amount_fen': 45})]
    events = [{'event_id': str(i), 'kind': kind, 'at': f'2026-05-{d}T10:{i:02d}:00+08:00',
               'effective_at': f'2026-05-{d}T10:{i:02d}:00+08:00', 'value': value,
               'evidence_ref': 'synthetic-receipt-'+str(i)} for i, (kind, d, value) in enumerate(specs)]
    return {'config': config, 'events': events}


def run_scenario(scenario, output):
    if set(scenario) != {'config', 'events'}:
        raise ValueError('strict scenario required')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    registration = {'scope': SCOPE, 'scenario_id': identity(scenario), 'implementation': implementation()}
    write_json(output/'registration.json', registration)
    write_json(output/'scenario.json', scenario)
    result = replay(**scenario)
    write_json(output/'report.json', result)
    manifest = {'registration_id': identity(registration),
                'artifact_hashes': {p.name: file_hash(p) for p in output.iterdir() if p.is_file()}}
    write_json(output/'completed.json', {**manifest, 'manifest_id': identity(manifest)})
    return result


def verify_scenario(folder):
    folder = Path(folder).resolve()
    manifest, _ = read_json(folder/'completed.json')
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid:
        raise ValueError('scenario manifest changed')
    paths = list(folder.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths):
        raise ValueError('unexpected scenario member')
    if {p.name: file_hash(p) for p in paths if p.name != 'completed.json'} != manifest['artifact_hashes']:
        raise ValueError('scenario members or bytes changed')
    reg, _ = read_json(folder/'registration.json')
    scenario, _ = read_json(folder/'scenario.json')
    result, _ = read_json(folder/'report.json')
    if identity(reg) != manifest['registration_id'] or reg['implementation'] != implementation() or identity(scenario) != reg['scenario_id']:
        raise ValueError('scenario/source binding changed')
    if replay(**scenario) != result:
        raise ValueError('scenario cannot be reproduced')
    return result
