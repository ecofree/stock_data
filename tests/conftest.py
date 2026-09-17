"""Shared fixture for the frozen historical recovery contract; no machine changes."""
import hashlib
import json
from pathlib import Path
import zipfile

import pytest


@pytest.fixture(scope='session')
def frozen_recovery(tmp_path_factory):
    archive = Path(__file__).resolve().parents[1] / 'recovery/20260916.zip'
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == 'be1bf79b80a82c28cc2bfdecac400ea1b9031ff33dbe7520325c162e1607cc71'
    root = tmp_path_factory.mktemp('frozen-recovery')
    with zipfile.ZipFile(archive) as z:
        hashes = json.loads(z.read('SHA256.json'))
        assert set(z.namelist()) == set(hashes) | {'SHA256.json'}
        for name, expected in hashes.items():
            data = z.read(name)
            assert hashlib.sha256(data).hexdigest() == expected
            target = (root / name).resolve()
            assert target.is_relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return root
