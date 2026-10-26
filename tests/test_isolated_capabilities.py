"""C1/C2/C3 counterexamples use synthetic data in one new tmp workspace only."""
from copy import deepcopy
import hashlib
import shutil
import sqlite3

import duckdb
import pytest

from trade_system.file_lock import FileLock, FileLockBusy
from trade_system.v2 import recovery_bundle as recovery, cold_archive as cold, feedback_ledger as feedback
from trade_system.v2 import capability_report
from trade_system.v2.daily_workspace import empty_projection
from trade_system.v2.domain import canonical, identity, file_hash
from trade_system.v2.isolated_workspace import initialize, child, write_json
from trade_system.v2.publisher import publish, read_current
from trade_system.v2.research_product_view import render
from tools.v2.isolated_capabilities import main


def prepared(root):
    initialize(root)
    for name in recovery.COMPONENTS:
        (root / 'inputs' / name).mkdir(parents=True)
    (root / 'inputs/code/source.txt').write_text('synthetic source identity', encoding='utf-8')
    (root / 'inputs/environment/lock.txt').write_text('synthetic environment identity', encoding='utf-8')
    (root / 'inputs/raw_facts/original.json').write_text('{"synthetic":true}', encoding='utf-8')
    data = empty_projection()
    data['report_id'] = identity(data)
    publish(root / 'inputs/workspace/publication', 'synthetic-run',
            {'desk.json': canonical(data).encode(), 'index.html': render(data).encode()}, generation=1)
    fact = {'snapshot_id': identity({'synthetic': True}), 'trade_date': '2026-10-20', 'stocks': {}}
    note = {'schema': 2, 'instrument': '000001', 'operator': 'synthetic-reviewer',
            'intent': 'observe', 'hypothesis': 'synthetic condition', 'invalidation': 'synthetic invalidation',
            'prediction_id': None, 'model_id': None, 'evidence_id': fact['snapshot_id'],
            'evidence_file_id': identity(fact), 'received_at': '2026-10-20T00:00:00+00:00',
            'request_id': '1' * 32, 'command_id': '2' * 64, 'supersedes': None,
            'identity_scope': 'caller_declared_not_authenticated', 'execution_ready': False}
    note['note_id'] = identity(note)
    write_json(root / 'inputs/workspace/notes' / (note['note_id'] + '.json'), note)
    write_json(root / 'inputs/workspace/notes/evidence' / (identity(fact) + '.json'), fact)
    db = root / 'inputs/databases/market.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('''CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,
          asset_code VARCHAR,provider VARCHAR,adjustment VARCHAR,close DOUBLE,volume BIGINT)''')
        con.execute("""INSERT INTO multi_source_kline VALUES
          ('2026-10-20','stock','000001','hithink','raw',10.0,100),
          ('2026-10-20','stock','000001','hithink','qfq',8.0,100),
          ('2026-10-21','stock','000001','xiaodefa','raw',11.0,200)""")
    plan = {'components': {name: 'inputs/' + name for name in recovery.COMPONENTS},
            'credential_custody_reference': 'external-custodian-not-supplied-for-rehearsal'}
    write_json(root / 'plan.json', plan)
    return plan


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / 'lab'
    plan = prepared(root)
    return root, plan


def captured(lab):
    root, plan = lab
    recovery.capture(root, plan, 'bundles/first')
    proof = recovery.restore(root, 'bundles/first', 'restores/first')
    return root, proof


def event(eid='fill-a', **changes):
    value = {'event_id': eid, 'revision': 1, 'supersedes_sha256': None, 'kind': 'fill',
             'status': 'active', 'order_id': 'order-a', 'instrument': 'SZ.000001', 'side': 'buy',
             'quantity': 100, 'amount_fen': 100000, 'occurred_at': '2026-10-20T10:00:00+08:00',
             'trade_date': '2026-10-20'}
    return dict(value, **changes)


def statement(root, *, name='first', changes=None):
    raw = b'Synthetic broker-shaped original. NOT a real broker statement.'
    (root / (name + '.raw')).write_bytes(raw)
    fee = event('fee-a', kind='fee', instrument=None, side=None, quantity=0, amount_fen=500)
    cancel = event('cancel-a', kind='cancel', quantity=200, amount_fen=0)
    value = {'schema': 'stockdata_feedback_statement_v1', 'account_id': 'synthetic-account',
             'source_kind': 'synthetic', 'original_sha256': hashlib.sha256(raw).hexdigest(),
             'period_start': '2026-10-20', 'period_end': '2026-10-21',
             'calendar': ['2026-10-20', '2026-10-21'],
             'opening': {'cash_fen': 1000000, 'positions': {}},
             'closing': {'cash_fen': 899500, 'positions': {'SZ.000001': 100}},
             'events': [event(), fee, cancel], 'declared_complete': True, 'fees_complete': True}
    value.update(changes or {})
    write_json(root / (name + '.json'), value)
    return value


def import_value(root, name='first', changes=None):
    value = statement(root, name=name, changes=changes)
    return value, feedback.import_statement(root, name + '.json', name + '.raw')


def test_full_declared_restore_preserves_files_and_actual_publication(lab):
    root, plan = lab
    before = {k: recovery.inventory(root / v) for k, v in plan['components'].items()}
    root, proof = captured(lab)
    assert proof['rpo_seconds'] is None and not proof['offsite_accepted']
    assert proof['elapsed_seconds'] >= 0
    assert before == {k: recovery.inventory(root / v) for k, v in plan['components'].items()}
    assert recovery.verified_restore(root, 'restores/first') == proof
    manifest, files = read_current(root / 'restores/first/workspace/publication')
    assert manifest['generation'] == 1
    assert files['index.html'] == read_current(root / 'inputs/workspace/publication')[1]['index.html']
    assert list((root / 'restores/first/workspace/notes').glob('*.json'))
    with pytest.raises(ValueError, match='separate new'):
        recovery.restore(root, 'bundles/first', 'restores/first')


@pytest.mark.parametrize('name', ['.env', 'credential.pem', 'market.duckdb.wal'])
def test_credentials_or_unclean_wal_block_capture(lab, name):
    root, plan = lab
    (root / 'inputs/databases' / name).write_bytes(b'not-a-real-credential')
    with pytest.raises(ValueError):
        recovery.capture(root, plan, 'bundle')
    assert not (root / 'bundle').exists()


@pytest.mark.parametrize('target', ['bundles/first/raw_facts/original.json',
                                  'restores/first/code/source.txt',
                                  'restores/first/workspace/publication/current.json'])
def test_tampering_revokes_restore_proof(lab, target):
    root, _ = captured(lab)
    (root / target).write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        recovery.verified_restore(root, 'restores/first')
    with pytest.raises(ValueError):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')


def test_busy_handle_is_not_stolen_and_guard_is_retained(lab):
    root, plan = lab
    guard = root / 'inputs/workspace/judgement.guard'
    with FileLock(guard):
        with pytest.raises(FileLockBusy):
            recovery.capture(root, plan, 'bundle')
    assert guard.exists() and not (root / 'bundle').exists()


def test_copy_interruption_never_seals_a_partial_backup(lab, monkeypatch):
    root, plan = lab
    original = shutil.copyfile
    calls = []
    def interrupted(source, destination):
        calls.append(str(source))
        if len(calls) == 2:
            raise OSError('synthetic interrupted copy')
        return original(source, destination)
    monkeypatch.setattr(recovery.shutil, 'copyfile', interrupted)
    with pytest.raises(OSError):
        recovery.capture(root, plan, 'bundle')
    assert not (root / 'bundle/completed.json').exists()
    assert file_hash(root / 'inputs/databases/market.duckdb')


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/outside', 'a/../b', 'a\\b', 'a//b'])
def test_relative_path_escape_rejected(lab, name):
    with pytest.raises(ValueError):
        child(lab[0], name)


def test_duplicate_json_key_rejected(lab):
    root, _ = lab
    (root / 'bad.json').write_text('{"components":{},"components":{}}', encoding='utf-8')
    assert main(['--root', str(root), 'capture', '--plan', 'bad.json', '--out', 'bundle']) == 1


def test_archive_repeat_resume_joint_overlap_and_source_preservation(lab, monkeypatch):
    root, _ = captured(lab)
    db = root / 'inputs/databases/market.duckdb'
    before = file_hash(db)
    original = cold.write_json
    def interrupted(path, value):
        raise OSError('synthetic interruption before completion receipt')
    monkeypatch.setattr(cold, 'write_json', interrupted)
    with pytest.raises(OSError):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-21', 'restores/first')
    monkeypatch.setattr(cold, 'write_json', original)
    first = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-21', 'restores/first')
    assert first == cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-21', 'restores/first')
    second = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    parts = [first['partition_id'], second['partition_id']]
    cold.plan_coverage(root, 'inputs/databases/market.duckdb', parts, '2026-10-20', '2026-10-22', 'restores/first', 'coverage.json')
    result = cold.joint_read(root, 'inputs/databases/market.duckdb', parts, '2026-10-20', '2026-10-22', coverage='coverage.json')
    assert len(result['rows']) == 3 and result['identical_overlap_rows'] == 5
    assert {r['adjustment'] for r in result['rows']} == {'raw', 'qfq'}
    assert before == file_hash(db)
    assert cold.retention_plan(root, [first['partition_id']])['deletions'] == 0


def test_hot_cold_conflict_and_uncovered_source_are_rejected(lab):
    root, _ = captured(lab)
    record = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    cold.plan_coverage(root, 'inputs/databases/market.duckdb', [record['partition_id']], '2026-10-20', '2026-10-22', 'restores/first', 'coverage.json')
    with duckdb.connect(str(root / 'inputs/databases/market.duckdb')) as con:
        con.execute("UPDATE multi_source_kline SET close=99 WHERE adjustment='raw'")
    with pytest.raises(ValueError, match='conflict'):
        cold.joint_read(root, 'inputs/databases/market.duckdb', [record['partition_id']], '2026-10-20', '2026-10-22', coverage='coverage.json')
    with pytest.raises(ValueError, match='recovery point'):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')


@pytest.mark.parametrize('sql', ["UPDATE multi_source_kline SET adjustment=NULL", 'INSERT INTO multi_source_kline SELECT * FROM multi_source_kline LIMIT 1'])
def test_unknown_or_duplicate_product_identity_blocks_archive(lab, sql):
    root, _ = lab
    with duckdb.connect(str(root / 'inputs/databases/market.duckdb')) as con:
        con.execute(sql)
    captured(lab)
    with pytest.raises(ValueError, match='product key'):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')


def test_partial_fills_cancel_fees_and_repeat_import(lab):
    root, _ = lab
    data, receipt = import_value(root)
    assert receipt['arithmetic_reconciled'] and receipt['computed_positions'] == {'SZ.000001': 100}
    assert receipt['fees_fen'] == 500 and not receipt['real_statement_acceptance']
    assert feedback.import_statement(root, 'first.json', 'first.raw') == receipt
    assert feedback.inspect(root)['counts']['event'] == 3
    ack = feedback.confirm(root, receipt['receipt_id'], 'test-operator', data['original_sha256'])
    assert ack['human_acknowledged'] and not ack['real_statement_acceptance']
    assert feedback.inspect(root)['records'][0]['human_acknowledged']


def test_late_revision_preserves_history_and_invalidates_old_confirmation(lab):
    root, _ = lab
    data, old = import_value(root)
    feedback.confirm(root, old['receipt_id'], 'test-operator', data['original_sha256'])
    corrected = deepcopy(data['events'])
    corrected[0].update(revision=2, supersedes_sha256=identity(data['events'][0]), amount_fen=99000)
    _, new = import_value(root, 'correction', {'events': corrected, 'closing': {'cash_fen': 900500, 'positions': {'SZ.000001': 100}}})
    assert new['arithmetic_reconciled']
    with pytest.raises(ValueError, match='superseded'):
        feedback.confirm(root, old['receipt_id'], 'test-operator', data['original_sha256'])
    inspected = feedback.inspect(root)
    assert inspected['counts']['event'] == 4
    old_view = next(v for v in inspected['records'] if v['receipt_id'] == old['receipt_id'])
    assert old_view['superseded'] and not old_view['human_acknowledged']


def test_void_is_not_cancellation_and_requires_linked_revision(lab):
    root, _ = lab
    data, _ = import_value(root)
    events = deepcopy(data['events'])
    events[0].update(status='void', revision=2, supersedes_sha256=identity(events[0]))
    _, result = import_value(root, 'void', {'events': events, 'closing': {'cash_fen': 999500, 'positions': {}}})
    assert result['arithmetic_reconciled'] and result['computed_positions'] == {}


@pytest.mark.parametrize('changes', [{'quantity': True}, {'amount_fen': 1.5}, {'amount_fen': -1},
                                    {'trade_date': '2026-10-21'}, {'occurred_at': '2026-10-20T02:00:00'},
                                    {'revision': 3, 'supersedes_sha256': 'a' * 64},
                                    {'kind': 'paper_fill'}, {'status': 'void'}])
def test_invalid_events_are_atomic_and_never_admitted(lab, changes):
    root, _ = lab
    import_value(root)
    before = feedback.inspect(root)['counts']
    statement(root, name='invalid', changes={'events': [event('new-valid'), event('bad', **changes)]})
    with pytest.raises(ValueError):
        feedback.import_statement(root, 'invalid.json', 'invalid.raw')
    assert feedback.inspect(root)['counts'] == before


@pytest.mark.parametrize('change', [{'fees_complete': False}, {'declared_complete': False},
                                  {'closing': {'cash_fen': 0, 'positions': {}}}, {'events': []}])
def test_missing_or_mismatched_evidence_cannot_pass_reconciliation(lab, change):
    root, _ = lab
    import_value(root)
    _, result = import_value(root, 'incomplete', change)
    assert not result['arithmetic_reconciled']
    with pytest.raises(ValueError):
        feedback.confirm(root, result['receipt_id'], 'test-operator', result['original_sha256'])


def test_conflicting_event_and_source_identity_roll_back_entire_import(lab):
    root, _ = lab
    import_value(root)
    before = feedback.inspect(root)['counts']
    for name, changes in [('conflict', {'events': [event(amount_fen=2)]}),
                          ('wrongmode', {'source_kind': 'broker_export'})]:
        statement(root, name=name, changes=changes)
        with pytest.raises(ValueError):
            feedback.import_statement(root, name + '.json', name + '.raw')
        assert feedback.inspect(root)['counts'] == before


def test_corruption_and_wrong_original_block_inspection_and_confirmation(lab):
    root, _ = lab
    value, receipt = import_value(root)
    (root / 'first.raw').write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'):
        feedback.import_statement(root, 'first.json', 'first.raw')
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        con.execute('UPDATE original SET content=?', [b'changed'])
    with pytest.raises(ValueError, match='corrupted'):
        feedback.inspect(root)
    with pytest.raises(ValueError, match='corrupted'):
        feedback.confirm(root, receipt['receipt_id'], 'test-operator', value['original_sha256'])


def test_sqlite_feedback_restores_and_report_is_self_contained(lab):
    root, _ = lab
    import_value(root)
    shutil.copyfile(root / 'feedback/ledger.sqlite', root / 'inputs/databases/feedback.sqlite')
    root, _ = captured(lab)
    record = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    result = capability_report.export(root, 'restores/first', [record['partition_id']], 'acceptance.html')
    page = (root / result['page']).read_text('utf-8')
    assert '算术核对一致' in page and '尚未完成' in page and 'synthetic' in page
    assert 'fetch(' not in page and '<script src=' not in page
    assert file_hash(root / 'inputs/databases/feedback.sqlite') == file_hash(root / 'restores/first/databases/feedback.sqlite')
    with pytest.raises(FileExistsError):
        capability_report.export(root, 'restores/first', [], 'acceptance.html')


def test_cli_requires_explicit_new_root_and_no_production_defaults(tmp_path, capsys):
    root = tmp_path / 'cli'
    assert main(['--root', str(root), 'init']) == 0
    assert main(['--root', str(root), 'init']) == 1
    assert main(['--root', str(root), 'inspect-feedback']) == 1
    assert 'execution_ready' in capsys.readouterr().out
    assert not (root / 'feedback/ledger.sqlite').exists()


def test_changed_capture_and_unknown_files_cannot_be_sealed(lab, monkeypatch):
    root, plan = lab
    original = shutil.copyfile
    source = root / 'inputs/code/source.txt'
    def changing(a, b):
        result = original(a, b)
        if str(a) == str(source):
            source.write_text('changed during copy', encoding='utf-8')
        return result
    monkeypatch.setattr(recovery.shutil, 'copyfile', changing)
    with pytest.raises(ValueError, match='changed'):
        recovery.capture(root, plan, 'bundle')
    assert not (root / 'bundle/completed.json').exists()


def test_archive_corruption_and_cold_only_read(lab):
    root, _ = captured(lab)
    record = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    cold.plan_coverage(root, 'inputs/databases/market.duckdb', [record['partition_id']], '2026-10-20', '2026-10-22', 'restores/first', 'coverage.json')
    empty = root / 'empty-hot.duckdb'
    with duckdb.connect(str(root / 'inputs/databases/market.duckdb'), read_only=True) as source:
        ddl = source.execute("SELECT sql FROM duckdb_tables() WHERE table_name='multi_source_kline'").fetchone()[0]
    with duckdb.connect(str(empty)) as con:
        con.execute(ddl)
    result = cold.joint_read(root, 'empty-hot.duckdb', [record['partition_id']], '2026-10-20', '2026-10-22', coverage='coverage.json')
    assert len(result['rows']) == 3 and result['identical_overlap_rows'] == 0
    path = root / 'archives' / record['partition_id'] / 'rows.parquet'
    path.write_bytes(b'corrupted archive')
    with pytest.raises(ValueError, match='checksum'):
        cold.joint_read(root, 'empty-hot.duckdb', [record['partition_id']], '2026-10-20', '2026-10-22', coverage='coverage.json')
    assert path.read_bytes() == b'corrupted archive'


def test_missing_member_and_bad_human_record_block_recovery(lab):
    root, plan = lab
    path = next((root / 'inputs/workspace/notes').glob('*.json'))
    path.write_text('{"note_id":"wrong"}', encoding='utf-8')
    with pytest.raises(ValueError, match='identity'):
        recovery.capture(root, plan, 'bundle')


def test_trade_calendar_and_cross_period_revision_are_not_guessed(lab):
    root, _ = lab
    data, _ = import_value(root)
    before = feedback.inspect(root)['counts']
    statement(root, name='bad-calendar', changes={'calendar': ['2026-10-21']})
    with pytest.raises(ValueError, match='session'):
        feedback.import_statement(root, 'bad-calendar.json', 'bad-calendar.raw')
    event2 = dict(data['events'][0], revision=2, supersedes_sha256=identity(data['events'][0]),
                  trade_date='2026-10-21', occurred_at='2026-10-21T10:00:00+08:00')
    statement(root, name='moved', changes={'events': [event2]})
    with pytest.raises(ValueError, match='cross-period'):
        feedback.import_statement(root, 'moved.json', 'moved.raw')
    assert feedback.inspect(root)['counts'] == before


def test_report_escapes_display_and_inline_evidence(lab):
    root, _ = captured(lab)
    import_value(root)
    value = {'recovery': recovery.verified_restore(root, 'restores/first'), 'archives': [],
             'feedback': feedback.inspect(root)}
    value['feedback']['records'][0]['account_id'] = '</script><script>alert(1)</script>'
    page = capability_report.render(value)
    assert '</script><script>alert(1)</script>' not in page
    assert '&lt;script&gt;' in page and '\\u003c/script' in page


def test_changed_statement_controls_invalidate_confirmation_even_without_event_changes(lab):
    root, _ = lab
    data, old = import_value(root)
    feedback.confirm(root, old['receipt_id'], 'test-operator', data['original_sha256'])
    _, new = import_value(root, 'changed-controls', {'closing': {'cash_fen': 1, 'positions': {'SZ.000001': 100}}})
    assert not new['arithmetic_reconciled']
    with pytest.raises(ValueError, match='superseded'):
        feedback.confirm(root, old['receipt_id'], 'test-operator', data['original_sha256'])
    rows = feedback.inspect(root)['records']
    assert all(not r['human_acknowledged'] for r in rows)
    assert next(r for r in rows if r['receipt_id'] == old['receipt_id'])['superseded']
