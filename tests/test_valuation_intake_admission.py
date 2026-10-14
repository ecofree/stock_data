"""Filesystem admission is byte custody, not financial qualification or database import."""
from datetime import datetime, timezone
import hashlib
import json

import pytest

from trade_system.file_lock import FileLock, FileLockBusy

NOW = datetime(2026, 10, 14, 5, tzinfo=timezone.utc)


def encode(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fixture(tmp_path):
    stage = tmp_path / 'reviewed-staging'
    runtime = tmp_path / 'isolated-runtime'
    (stage / 'bundles').mkdir(parents=True)
    (stage / 'originals').mkdir()
    runtime.mkdir()
    original = b'%PDF-1.7\nsynthetic storage fixture, not a financial statement\n'
    original_hash = digest(original)
    (stage / 'originals' / original_hash).write_bytes(original)
    bundle = encode({'reviews': [{'ts_code': '000001.SZ', 'trade_date': '2026-10-14'}],
                     'reviewed_source_documents': [{'document': {
                         'path': 'originals/' + original_hash, 'sha256': original_hash,
                         'format': 'pdf', 'url': 'https://static.cninfo.com.cn/fixture.pdf'}}]})
    (stage / 'bundles/review.json').write_bytes(bundle)
    manifest = {'schema': 'valuation_daily_intake_v1', 'trade_date': '2026-10-14',
                'reviewed_by': 'synthetic-reviewer', 'reviewed_at': '2026-10-14T12:00:00+08:00',
                'bundles': [{'path': 'bundles/review.json', 'sha256': digest(bundle)}]}
    raw = encode(manifest)
    (stage / 'manifest.json').write_bytes(raw)
    return stage, runtime, raw, original_hash


def test_admission_exact_bytes_mtime_and_idempotent_retry(tmp_path):
    from trade_system.valuation_intake import plan_intake, admit_intake, plan_sha256
    stage, runtime, raw, original_hash = fixture(tmp_path)
    plan = plan_intake(stage, digest(raw), runtime, now=NOW)
    assert not (runtime / 'evidence').exists(), 'plan must be read-only'
    result = admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
    day = runtime / 'workspace/valuation-intake/20261014'
    assert (day / 'manifest.json').read_bytes() == raw
    assert (day / 'manifest.sha256').read_text().strip() == digest(raw)
    assert (day / 'bundles/review.json').read_bytes() == (stage / 'bundles/review.json').read_bytes()
    target = runtime / 'evidence/objects' / original_hash
    assert target.read_bytes() == (stage / 'originals' / original_hash).read_bytes()
    assert target.stat().st_mtime_ns == (stage / 'originals' / original_hash).stat().st_mtime_ns
    again = admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
    assert again['manifest_sha256'] == result['manifest_sha256']
    assert again['status'] == 'already_admitted'
    assert result['business_database_writes'] == 0 and result['financial_qualification_granted'] is False
    assert (runtime / 'workspace/valuation-intake/admission.guard').is_file()


@pytest.mark.parametrize('case', ['manifest_hash', 'bundle_hash', 'original_hash', 'old_day',
                                   'future_review', 'missing_reviewer', 'escape_bundle', 'outside_original'])
def test_invalid_material_rejected_before_any_target_write(tmp_path, case):
    from trade_system.valuation_intake import plan_intake
    stage, runtime, raw, h = fixture(tmp_path)
    manifest = json.loads(raw)
    if case == 'manifest_hash':
        expected = '0' * 64
    else:
        if case == 'bundle_hash': manifest['bundles'][0]['sha256'] = '0' * 64
        elif case == 'original_hash': (stage / 'originals' / h).write_bytes(b'changed original')
        elif case == 'old_day': manifest['trade_date'] = '2026-10-13'
        elif case == 'future_review': manifest['reviewed_at'] = '2026-10-14T23:00:00+08:00'
        elif case == 'missing_reviewer': manifest['reviewed_by'] = ''
        elif case == 'escape_bundle': manifest['bundles'][0]['path'] = '../outside.json'
        elif case == 'outside_original':
            (stage / 'originals' / h).unlink()
            bundle = json.loads((stage / 'bundles/review.json').read_bytes())
            outside = tmp_path / 'outside.pdf'
            outside.write_bytes(b'%PDF-1.7\nsynthetic storage fixture, not a financial statement\n')
            bundle['reviewed_source_documents'][0]['document']['path'] = str(outside)
            body = encode(bundle)
            (stage / 'bundles/review.json').write_bytes(body)
            manifest['bundles'][0]['sha256'] = digest(body)
        raw = encode(manifest)
        (stage / 'manifest.json').write_bytes(raw)
        expected = digest(raw)
    with pytest.raises((ValueError, FileNotFoundError)):
        plan_intake(stage, expected, runtime, now=NOW)
    assert not list(runtime.iterdir())


def test_tampered_plan_or_source_cannot_publish(tmp_path):
    from trade_system.valuation_intake import plan_intake, admit_intake, plan_sha256
    stage, runtime, raw, h = fixture(tmp_path)
    plan = plan_intake(stage, digest(raw), runtime, now=NOW)
    with pytest.raises(ValueError):
        admit_intake(plan, '0' * 64, runtime, now=NOW)
    (stage / 'originals' / h).write_bytes(b'changed')
    with pytest.raises(ValueError):
        admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
    assert not (runtime / 'workspace/valuation-intake/20261014/manifest.json').exists()


def test_existing_corrupt_object_never_overwritten(tmp_path):
    from trade_system.valuation_intake import plan_intake, admit_intake, plan_sha256
    stage, runtime, raw, h = fixture(tmp_path)
    plan = plan_intake(stage, digest(raw), runtime, now=NOW)
    objects = runtime / 'evidence/objects'
    objects.mkdir(parents=True)
    (objects / h).write_bytes(b'corrupt retained object')
    with pytest.raises(ValueError): admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
    assert (objects / h).read_bytes() == b'corrupt retained object'


def test_other_owner_guard_blocks_and_survives(tmp_path):
    from trade_system.valuation_intake import plan_intake, admit_intake, plan_sha256
    stage, runtime, raw, _ = fixture(tmp_path)
    plan = plan_intake(stage, digest(raw), runtime, now=NOW)
    intake = runtime / 'workspace/valuation-intake'
    intake.mkdir(parents=True)
    guard = intake / 'admission.guard'
    with FileLock(guard):
        with pytest.raises(FileLockBusy):
            admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
    assert guard.exists() and not (intake / '20261014').exists()


def test_existing_different_manifest_is_preserved(tmp_path):
    from trade_system.valuation_intake import plan_intake, admit_intake, plan_sha256
    stage, runtime, raw, _ = fixture(tmp_path)
    plan = plan_intake(stage, digest(raw), runtime, now=NOW)
    day = runtime / 'workspace/valuation-intake/20261014'
    day.mkdir(parents=True)
    (day / 'manifest.json').write_bytes(b'original review must remain')
    with pytest.raises(ValueError): admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
    assert (day / 'manifest.json').read_bytes() == b'original review must remain'


def test_interrupted_owned_admission_resumes_without_stealing_a_guard(tmp_path, monkeypatch):
    from trade_system import valuation_intake as intake
    stage, runtime, raw, h = fixture(tmp_path)
    plan = intake.plan_intake(stage, digest(raw), runtime, now=NOW)
    original_publish = intake._publish_bytes

    def interrupt(path, data, mtime=None):
        if path.name == 'manifest.json':
            raise OSError('synthetic interruption before publication')
        return original_publish(path, data, mtime)

    monkeypatch.setattr(intake, '_publish_bytes', interrupt)
    with pytest.raises(OSError):
        intake.admit_intake(plan, intake.plan_sha256(plan), runtime, now=NOW)
    day = runtime / 'workspace/valuation-intake/20261014'
    assert not (day / 'manifest.json').exists()
    guard = runtime / 'workspace/valuation-intake/admission.guard'
    guard_identity = guard.stat().st_ino
    monkeypatch.setattr(intake, '_publish_bytes', original_publish)
    result = intake.admit_intake(plan, intake.plan_sha256(plan), runtime, now=NOW)
    assert result['status'] == 'admitted_bytes_only'
    assert (day / 'manifest.json').read_bytes() == raw
    assert guard.stat().st_ino == guard_identity
    assert [p.name for p in (runtime / 'evidence/objects').iterdir()] == [h]
    assert not list(runtime.rglob('.incoming-*'))


def test_concurrent_claim_has_one_manifest_and_retries_return_same_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from trade_system.valuation_intake import plan_intake, admit_intake, plan_sha256
    stage, runtime, raw, _ = fixture(tmp_path)
    plan = plan_intake(stage, digest(raw), runtime, now=NOW)

    def attempt():
        try:
            return admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
        except FileLockBusy:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert any(results)
    retried = admit_intake(plan, plan_sha256(plan), runtime, now=NOW)
    assert retried['status'] == 'already_admitted'
    assert all(r['manifest_sha256'] == retried['manifest_sha256'] for r in results if r)
    assert len(list((runtime / 'workspace/valuation-intake').glob('20261014'))) == 1


def test_atomic_publish_does_not_replace_a_racing_different_object(tmp_path, monkeypatch):
    from trade_system import valuation_intake as intake
    destination = tmp_path / 'object'

    def raced_link(source, target):
        target.write_bytes(b'independent existing bytes')
        raise FileExistsError('synthetic competing publisher')

    monkeypatch.setattr(intake.os, 'link', raced_link)
    with pytest.raises(ValueError):
        intake._publish_bytes(destination, b'our bytes')
    assert destination.read_bytes() == b'independent existing bytes'
    assert not list(tmp_path.glob('.incoming-*'))
