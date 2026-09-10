"""Isolated import/reconciliation probe; never admits a real trading route."""
import hashlib
from pathlib import Path
import tempfile

from .accounts import import_snapshot
from .domain import now_utc
from .storage import Store


def inspect_account(path=None, *, clock=now_utc):
    if path is None:
        return {'status':'missing_real_account','parser_verified':False,'reconciled':None,
                'execution_ready':False,'required':['asof','cash','positions','sellable','frozen_cash','open_orders']}
    path = Path(path)
    if path.stat().st_size > 1_000_000:
        raise ValueError('account export exceeds 1 MB')
    raw = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix='stock-data-account-check-') as temporary:
        with Store(Path(temporary)/'isolated.duckdb',clock=clock) as store:
            imported = import_snapshot(store,raw)
    return {'status':'structurally_reconciled_not_operationally_accepted' if imported['reconciled'] else 'reconciliation_failed',
        'parser_verified':True,'reconciled':imported['reconciled'],'mode':imported['mode'],
        'source_sha256':hashlib.sha256(raw).hexdigest(),'execution_ready':False,
        'not_verified':['broker_source_authenticity','external_fills','operator_approval','live_routing']}
