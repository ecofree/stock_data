"""Refactor invariants, independent of provider traffic or model effectiveness."""
import pytest

from trade_system.v2 import research_dataset as dataset
from tests.test_research_delivery import prices


def test_frozen_inference_compatibility_is_exact_not_general_bypass(monkeypatch):
    meta={'source_sha256':'ef6711d61e95691bb0cbb684e6528f5f5b3807fa4b06b6d57f2eea90bc90790f'}
    assert dataset.inference_compatible(meta)
    assert not dataset.inference_compatible({'source_sha256':'0'*64})
    monkeypatch.setattr(dataset,'BASE',[*dataset.BASE,'unreviewed_feature'])
    assert not dataset.inference_compatible(meta)


def test_target_never_skips_a_missing_exchange_session():
    frame,days=prices()
    calculated=dataset.features(frame,days)
    incomplete=calculated.drop(calculated.index[62])
    with pytest.raises(ValueError,match='full session grid'):
        dataset.target_labels(incomplete,days,family='price21')


def test_release_and_verifier_bind_runtime_json_configs():
    from tools.v2.verify_delivery import source_files
    from tools.v2.package_research import source_closure
    assert 'config/research_inference_compatibility.json' in source_files()
    files=source_closure()
    assert 'trade_system/v2/daily_workspace.py' in files
    assert 'trade_system/v2/operator_workflow.py' in files
