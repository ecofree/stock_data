"""Historical algorithm fixtures run only on genuinely verified disposable copies.

Copy-back is solely for these synthetic test fixtures, never a runtime escape hatch.
Public entrypoint refusal has its own tests without this helper.
"""
from pathlib import Path
import shutil
import tempfile

from tools.v2.backup_verify import backup_verify
from trade_system.signals import generate_signals as _signals
from trade_system.stage_signals import generate_stage_signals as _stage
from trade_system.stage_signals import refresh_close_signals_if_needed as _refresh


def _run(function, db_path, *args, **kwargs):
    db = Path(db_path)
    root = Path(tempfile.mkdtemp(prefix='verified-diagnostic-',dir=db.parent)) / 'copy'
    receipt = backup_verify(db,root)
    result = function(receipt['backup'],*args,migration_root=root,**kwargs)
    shutil.copy2(receipt['backup'],db)
    return result


def generate_signals(db_path,*args,**kwargs):
    return _run(_signals,db_path,*args,**kwargs)


def generate_stage_signals(db_path,*args,**kwargs):
    return _run(_stage,db_path,*args,**kwargs)


def refresh_close_signals_if_needed(db_path,*args,**kwargs):
    return _run(_refresh,db_path,*args,**kwargs)
