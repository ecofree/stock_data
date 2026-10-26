"""Independent boundary tests for the C1/C2/C3 review findings; synthetic only."""
from copy import deepcopy
from datetime import timedelta
import json
import shutil
import sqlite3

import duckdb
import pytest

from test_isolated_capabilities import prepared, import_value, captured
from trade_system.v2 import feedback_ledger as feedback, recovery_bundle as recovery, cold_archive as cold
from trade_system.v2 import isolated_workspace as isolated, capability_report, operator_workflow, research_journal
from trade_system.v2.domain import canonical, identity, file_hash, now_utc
from trade_system.v2.isolated_workspace import seal, read_json, write_json
from tools.v2.isolated_capabilities import main


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / 'lab'
    return root, prepared(root)


def revised(root):
    original, first = import_value(root)
    feedback.confirm(root, first['receipt_id'], 'synthetic-reviewer', original['original_sha256'])
    events = deepcopy(original['events'])
    events[0].update(revision=2, status='void', supersedes_sha256=identity(events[0]))
    current, second = import_value(root, 'void', {'events': events, 'closing': {'cash_fen': 999500, 'positions': {}}})
    return original, first, current, second


def test_stale_active_declaration_cannot_pass_or_be_confirmed(lab):
    root, _ = lab
    original, _, current, _ = revised(root)
    incoming, receipt = import_value(root, 'stale', {'events': original['events'], 'closing': current['closing']})
    assert not receipt['arithmetic_reconciled']
    assert 'declared_active_events_not_applied' in receipt['blockers']
    assert 'declared_event_revision_not_current' in receipt['blockers']
    with pytest.raises(ValueError, match='reconciled'):
        feedback.confirm(root, receipt['receipt_id'], 'synthetic-reviewer', incoming['original_sha256'])
    row = next(r for r in feedback.inspect(root)['records'] if r['receipt_id'] == receipt['receipt_id'])
    assert not row['arithmetic_reconciled'] and not row['human_acknowledged']


@pytest.mark.parametrize('sql', [
    "DELETE FROM event WHERE event_id='fill-a' AND revision=2",
    "DELETE FROM event WHERE event_id='fill-a' AND revision=1",
    "DELETE FROM event WHERE event_id='fill-a'",
    "DELETE FROM statement WHERE rowid=2",
    "DELETE FROM account",
    "DELETE FROM reconciliation",
    "UPDATE account SET source_kind='broker_export'",
])
def test_ledger_logical_loss_blocks_inspect_confirm_and_import(lab, sql):
    root, _ = lab
    original, _, current, receipt = revised(root)
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        con.execute(sql)
        before = con.execute('SELECT count(*) FROM confirmation').fetchone()[0]
    with pytest.raises(ValueError):
        feedback.inspect(root)
    with pytest.raises(ValueError):
        feedback.confirm(root, receipt['receipt_id'], 'synthetic-reviewer', current['original_sha256'])
    with pytest.raises(ValueError):
        feedback.import_statement(root, 'void.json', 'void.raw')
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        assert con.execute('SELECT count(*) FROM confirmation').fetchone()[0] == before


def test_corrupt_statement_blocks_confirmation_without_writing(lab):
    root, _ = lab
    data, receipt = import_value(root)
    corrupt = deepcopy(data)
    corrupt['closing']['cash_fen'] += 1
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        con.execute('UPDATE statement SET payload=?', [canonical(corrupt)])
    for action in (lambda: feedback.inspect(root),
                   lambda: feedback.confirm(root, receipt['receipt_id'], 'synthetic-reviewer', data['original_sha256'])):
        with pytest.raises(ValueError, match='statement identity corrupted'):
            action()
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        assert con.execute('SELECT count(*) FROM confirmation').fetchone()[0] == 0


def test_complete_history_input_uses_latest_declared_revision_and_void_head(lab):
    root, _ = lab
    original, _, current, _ = revised(root)
    data, receipt = import_value(root, 'history', {'events': [original['events'][0], *current['events']], 'closing': current['closing']})
    assert receipt['arithmetic_reconciled']
    feedback.confirm(root, receipt['receipt_id'], 'synthetic-reviewer', data['original_sha256'])
    # A further void revision changes no money, but must revoke the old ack.
    changes = deepcopy(current['events'])
    changes[0].update(revision=3, supersedes_sha256=identity(changes[0]))
    _, new = import_value(root, 'void-again', {'events': changes, 'closing': current['closing']})
    assert new['arithmetic_reconciled'] and new['applied_event_hashes'] == receipt['applied_event_hashes']
    with pytest.raises(ValueError, match='superseded'):
        feedback.confirm(root, receipt['receipt_id'], 'synthetic-reviewer', data['original_sha256'])
    assert all(not r['human_acknowledged'] for r in feedback.inspect(root)['records'])


def test_legacy_receipt_is_retained_but_requires_explicit_revalidation(lab):
    root, _ = lab
    data, receipt = import_value(root)
    legacy = {k:v for k,v in receipt.items() if k not in ('receipt_id', 'event_version_hashes')}
    legacy['schema'] = 'isolated_feedback_reconciliation_v1'
    legacy = seal(legacy)
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        con.execute('DELETE FROM reconciliation')
        con.execute('INSERT INTO reconciliation VALUES(?,?)', [legacy['receipt_id'], canonical(legacy)])
    row = feedback.inspect(root)['records'][0]
    assert row['requires_revalidation'] and not row['arithmetic_reconciled']
    with pytest.raises(ValueError, match='superseded'):
        feedback.confirm(root, legacy['receipt_id'], 'synthetic-reviewer', data['original_sha256'])
    new = feedback.import_statement(root, 'first.json', 'first.raw')
    assert new['arithmetic_reconciled']
    feedback.confirm(root, new['receipt_id'], 'synthetic-reviewer', data['original_sha256'])
    assert feedback.inspect(root)['counts']['reconciliation'] == 2


def test_confirmation_holds_sqlite_write_transaction_during_validation(lab, monkeypatch):
    root, _ = lab
    data, receipt = import_value(root)
    original = feedback._inspect_connection
    attempts = []
    def checked(con):
        with sqlite3.connect(root / 'feedback/ledger.sqlite', timeout=0) as rival:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                rival.execute("UPDATE statement SET received_at=received_at")
            attempts.append(True)
        return original(con)
    monkeypatch.setattr(feedback, '_inspect_connection', checked)
    feedback.confirm(root, receipt['receipt_id'], 'synthetic-reviewer', data['original_sha256'])
    assert attempts == [True]


def test_historical_reconciliation_arithmetic_is_checked_after_revision(lab):
    root, _ = lab
    _, old, _, _ = revised(root)
    damaged = dict(old, computed_cash_fen=123)
    damaged.pop('receipt_id')
    damaged = seal(damaged)
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        con.execute('DELETE FROM confirmation')
        con.execute('DELETE FROM reconciliation WHERE receipt_id=?', [old['receipt_id']])
        con.execute('INSERT INTO reconciliation VALUES(?,?)', [damaged['receipt_id'], json.dumps(damaged)])
    with pytest.raises(ValueError, match='arithmetic'):
        feedback.inspect(root)


def human_plan(workspace, note, *, request='a' * 32, supersedes=''):
    return operator_workflow.save_observation_plan(workspace, {
        'note_id': note['note_id'], 'operator': note['operator'], 'condition': 'manual',
        'threshold': '', 'valid_until': (now_utc() + timedelta(days=1)).isoformat(),
        'supersedes': supersedes, 'request_id': request})


@pytest.mark.parametrize('missing', ['evidence', 'plan-parent', 'review-parent', 'note-revision', 'plan-revision'])
def test_recovery_rejects_broken_human_references(lab, missing):
    root, plan = lab
    workspace = root / 'inputs/workspace'
    path = next((workspace / 'notes').glob('*.json'))
    note = read_json(path)
    if missing in ('evidence', 'plan-parent', 'plan-revision'):
        pid = human_plan(workspace, note)
        assert operator_workflow.read_observation_plan(workspace, pid)['note_id'] == note['note_id']
        if missing == 'plan-revision':
            human_plan(workspace, note, request='b' * 32, supersedes=pid)
            path = workspace / 'notes/plans' / (pid + '.json')
        elif missing == 'evidence':
            path = workspace / 'notes/evidence' / (note['evidence_file_id'] + '.json')
    elif missing == 'review-parent':
        rid = research_journal.save_review(workspace, {'note_id': note['note_id'], 'request_id': 'c' * 32,
             'conclusion': 'pending', 'reviewer': 'synthetic-reviewer', 'evidence': 'synthetic observation'})
        assert research_journal.read_review(workspace, rid)['note_id'] == note['note_id']
    else:
        newer = dict(note, supersedes=note['note_id'], request_id='d' * 32)
        newer.pop('note_id')
        newer['note_id'] = identity(newer)
        write_json(workspace / 'notes/attention' / (newer['note_id'] + '.json'), newer)
    path.unlink()  # Intentional loss in the newly created synthetic fixture only.
    with pytest.raises((ValueError, FileNotFoundError)):
        recovery.capture(root, plan, 'bundle')
    assert not (root / 'bundle/completed.json').exists()


def test_restored_human_records_are_readable_without_index_rebuild(lab):
    root, _ = lab
    workspace = root / 'inputs/workspace'
    note = read_json(next((workspace / 'notes').glob('*.json')))
    pid = human_plan(workspace, note)
    rid = research_journal.save_review(workspace, {'note_id': note['note_id'], 'request_id': 'c' * 32,
         'conclusion': 'pending', 'reviewer': 'synthetic-reviewer', 'evidence': 'synthetic observation'})
    captured(lab)
    restored = root / 'restores/first/workspace'
    assert not (restored / 'notes/.index').exists()
    assert operator_workflow.read_observation_plan(restored, pid) == operator_workflow.read_observation_plan(workspace, pid)
    assert research_journal.read_review(restored, rid) == research_journal.read_review(workspace, rid)
    assert not (restored / 'notes/.index').exists()


def test_recovery_checks_feedback_semantics_not_only_sqlite_structure(lab):
    root, plan = lab
    revised(root)
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        con.execute("DELETE FROM event WHERE event_id='fill-a' AND revision=2")
        assert con.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    shutil.copyfile(root / 'feedback/ledger.sqlite', root / 'inputs/databases/feedback.sqlite')
    with pytest.raises(ValueError, match='revision missing'):
        recovery.capture(root, plan, 'bundle')


@pytest.mark.parametrize('point', ['flush', 'before-publication', 'after-publication'])
def test_archive_atomic_receipt_interruption_is_resumable(lab, monkeypatch, point):
    root, _ = captured(lab)
    before = file_hash(root / 'inputs/databases/market.duckdb')
    original_link = isolated.os.link
    original_sync = isolated.os.fsync
    def broken_link(src, dst):
        if point == 'after-publication':
            original_link(src, dst)
        raise OSError('synthetic receipt publication interruption')
    def broken_sync(fd):
        original_sync(fd)
        raise OSError('synthetic receipt flush interruption')
    with monkeypatch.context() as patch:
        patch.setattr(isolated.os, 'fsync' if point == 'flush' else 'link', broken_sync if point == 'flush' else broken_link)
        with pytest.raises(OSError, match='synthetic receipt'):
            cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    result = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    assert cold.verify(root, result['partition_id']) == result
    assert result['rows'] == 3 and file_hash(root / 'inputs/databases/market.duckdb') == before
    assert len(list((root / 'archives').glob('*/completed.json'))) == 1
    assert list((root / 'archives').glob('*/.pending-*'))


def test_legacy_partial_receipt_requires_explicit_hash_and_preserves_bad_bytes(lab):
    root, _ = captured(lab)
    first = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    path = root / 'archives' / first['partition_id'] / 'completed.json'
    path.write_text('{"schema":', encoding='utf-8')
    bad_hash = file_hash(path)
    with pytest.raises(ValueError):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    with pytest.raises(ValueError, match='changed'):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first',
                     damaged_receipt_sha256='f' * 64, repair_reason='synthetic repair')
    result = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first',
                          damaged_receipt_sha256=bad_hash, repair_reason='synthetic repair')
    assert result == first
    saved = path.parent / 'failed-receipts' / (bad_hash + '.json')
    assert file_hash(saved) == bad_hash and saved.read_text('utf-8') == '{"schema":'
    assert read_json(saved.with_suffix('.repair.json'))['reason'] == 'synthetic repair'
    with pytest.raises(ValueError, match='sealed'):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first',
                     damaged_receipt_sha256=file_hash(path), repair_reason='must refuse valid receipt')


def archive_catalog(lab):
    root, _ = captured(lab)
    parts = [cold.archive(root, 'inputs/databases/market.duckdb', a, b, 'restores/first')['partition_id']
             for a, b in [('2026-10-20', '2026-10-21'), ('2026-10-21', '2026-10-22')]]
    plan = cold.plan_coverage(root, 'inputs/databases/market.duckdb', parts, '2026-10-20', '2026-10-22', 'restores/first', 'coverage.json')
    with duckdb.connect(str(root / 'empty-hot.duckdb')) as con:
        con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,provider VARCHAR,adjustment VARCHAR,close DOUBLE,volume BIGINT)')
    return root, parts, plan


def test_missing_partition_fails_even_if_remaining_hot_rows_hide_omission(lab):
    root, parts, _ = archive_catalog(lab)
    for hot in ('empty-hot.duckdb', 'inputs/databases/market.duckdb'):
        with pytest.raises(ValueError, match='catalog mismatch'):
            cold.joint_read(root, hot, parts[:1], '2026-10-20', '2026-10-22', coverage='coverage.json')
    complete = cold.joint_read(root, 'empty-hot.duckdb', list(reversed(parts)), '2026-10-20', '2026-10-22', coverage='coverage.json')
    assert complete['coverage_verified'] and len(complete['rows']) == 3


def test_coverage_requires_full_values_and_independent_baseline(lab):
    root, parts, _ = archive_catalog(lab)
    # A catalog with only the first partition is allowed for a mixed hot/cold
    # layout, but cannot certify that partial archive as a complete cold tier.
    cold.plan_coverage(root, 'inputs/databases/market.duckdb', parts[:1], '2026-10-20', '2026-10-22', 'restores/first', 'partial-catalog.json')
    with pytest.raises(ValueError, match='coverage differs'):
        cold.joint_read(root, 'empty-hot.duckdb', parts[:1], '2026-10-20', '2026-10-22', coverage='partial-catalog.json')
    with duckdb.connect(str(root / 'empty-hot.duckdb')) as con:
        con.execute("INSERT INTO multi_source_kline VALUES ('2026-10-21','stock','000001','xiaodefa','raw',999,200)")
    with pytest.raises(ValueError, match='coverage differs'):
        cold.joint_read(root, 'empty-hot.duckdb', parts[:1], '2026-10-20', '2026-10-22', coverage='partial-catalog.json')
    plan = read_json(root / 'partial-catalog.json')
    plan.update(rows=2, value_sha256=cold.verify(root, parts[0])['value_sha256'])
    plan.pop('receipt_id')
    write_json(root / 'forged-controls.json', seal(plan))
    with pytest.raises(ValueError, match='actual recovery point'):
        cold.joint_read(root, 'empty-hot.duckdb', parts[:1], '2026-10-20', '2026-10-22', coverage='forged-controls.json')


def test_coverage_rejects_missing_files_unknown_plan_and_wrong_window(lab):
    root, parts, _ = archive_catalog(lab)
    with pytest.raises(ValueError, match='coverage plan required'):
        cold.joint_read(root, 'empty-hot.duckdb', parts, '2026-10-20', '2026-10-22')
    with pytest.raises(ValueError, match='window differs'):
        cold.joint_read(root, 'empty-hot.duckdb', parts, '2026-10-20', '2026-10-23', coverage='coverage.json')
    (root / 'archives' / parts[1] / 'completed.json').unlink()
    with pytest.raises(FileNotFoundError):
        cold.joint_read(root, 'empty-hot.duckdb', parts, '2026-10-20', '2026-10-22', coverage='coverage.json')


def test_actual_reports_show_rejected_feedback_and_separate_coverage_status(lab):
    root, parts, _ = archive_catalog(lab)
    original, _, current, _ = revised(root)
    _, bad = import_value(root, 'stale', {'events': original['events'], 'closing': current['closing']})
    capability_report.export(root, 'restores/first', parts, 'not-covered.html')
    assert '归档覆盖尚未核验' in (root / 'not-covered.html').read_text('utf-8')
    capability_report.export(root, 'restores/first', parts, 'rejected-feedback.html', coverage='coverage.json', source='empty-hot.duckdb')
    html = (root / 'rejected-feedback.html').read_text('utf-8')
    assert '存在差异或缺少证据' in html and '声明中的有效事件未进入本次核对' in html
    assert '已按独立恢复基准核验覆盖：3 行' in html
    assert '<td>已确认</td>' not in html and 'fetch(' not in html
    data = json.loads(html.split('<script type="application/json" id="evidence">')[1].split('</script>')[0])
    row = next(r for r in data['feedback']['records'] if r['receipt_id'] == bad['receipt_id'])
    assert not row['arithmetic_reconciled'] and not row['human_acknowledged']
    assert data['coverage']['coverage_verified'] and data['coverage']['rows'] == 3


def test_bad_parquet_cannot_be_hidden_by_receipt_repair(lab):
    root, _ = captured(lab)
    receipt = cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first')
    folder = root / 'archives' / receipt['partition_id']
    completion = folder / 'completed.json'
    completion.write_text('{', encoding='utf-8')
    (folder / 'rows.parquet').write_bytes(b'broken synthetic parquet')
    bad_hash = file_hash(completion)
    with pytest.raises((ValueError, duckdb.Error)):
        cold.archive(root, 'inputs/databases/market.duckdb', '2026-10-20', '2026-10-22', 'restores/first',
                     damaged_receipt_sha256=bad_hash, repair_reason='must recheck data first')
    assert file_hash(completion) == bad_hash and not (folder / 'failed-receipts').exists()


def test_known_empty_window_can_be_verified_without_guessing_missing_data(lab):
    root, _ = captured(lab)
    cold.plan_coverage(root, 'inputs/databases/market.duckdb', [], '2026-10-22', '2026-10-23', 'restores/first', 'empty-coverage.json')
    value = cold.joint_read(root, 'inputs/databases/market.duckdb', [], '2026-10-22', '2026-10-23', coverage='empty-coverage.json')
    assert value['coverage_verified'] and value['rows'] == []


def test_cli_coverage_and_report_are_bound_to_actual_query(lab, capsys):
    root, parts, _ = archive_catalog(lab)
    import_value(root)
    prefix = ['--root', str(root)]
    assert main(prefix + ['plan-coverage', '--source', 'inputs/databases/market.duckdb', '--partitions', *parts,
                         '--start', '2026-10-20', '--end', '2026-10-22', '--restore', 'restores/first', '--out', 'cli-coverage.json']) == 0
    assert main(prefix + ['joint-read', '--source', 'empty-hot.duckdb', '--partitions', *parts,
                         '--start', '2026-10-20', '--end', '2026-10-22', '--coverage', 'cli-coverage.json', '--out', 'joint.json']) == 0
    assert read_json(root / 'joint.json')['coverage_verified']
    assert main(prefix + ['report', '--restore', 'restores/first', '--partitions', *parts,
                         '--coverage', 'cli-coverage.json', '--source', 'empty-hot.duckdb', '--out', 'cli-report.html']) == 0
    assert '已按独立恢复基准核验覆盖：3 行' in (root / 'cli-report.html').read_text('utf-8')
    assert main(prefix + ['report', '--restore', 'restores/first', '--partitions', parts[0],
                         '--coverage', 'cli-coverage.json', '--source', 'empty-hot.duckdb', '--out', 'bad-report.html']) == 1
    assert not (root / 'bad-report.html').exists()
    assert 'coverage_verified' in capsys.readouterr().out


def test_corrupt_ledger_prevents_export_before_any_html_is_written(lab):
    root, _ = captured(lab)
    revised(root)
    with sqlite3.connect(root / 'feedback/ledger.sqlite') as con:
        con.execute("DELETE FROM event WHERE event_id='fill-a' AND revision=2")
    with pytest.raises(ValueError, match='revision missing'):
        capability_report.export(root, 'restores/first', [], 'bad-report.html')
    assert not (root / 'bad-report.html').exists()


def test_immutable_json_publication_never_overwrites_a_racing_writer(lab, monkeypatch):
    root, _ = lab
    target = root / 'immutable.json'
    link = isolated.os.link
    def race(source, destination):
        destination.write_text('{"owner":"other"}', encoding='utf-8')
        return link(source, destination)
    monkeypatch.setattr(isolated.os, 'link', race)
    with pytest.raises(FileExistsError):
        write_json(target, {'owner': 'this'})
    assert read_json(target) == {'owner': 'other'}
