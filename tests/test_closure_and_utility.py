import os
import duckdb
import pandas as pd
import pytest

from trade_system import db_utils
from trade_system.v2.utility_verification import evaluate


def test_installed_legacy_disk_writes_are_denied_but_reads_and_memory_work(tmp_path,monkeypatch):
    target=tmp_path/'not-created.duckdb'
    monkeypatch.setattr(db_utils,'primary_checkout',lambda:None)
    with pytest.raises(ValueError,match='installed runtime'):db_utils.legacy_connect(target)
    assert not target.exists()
    with duckdb.connect(str(target)) as con:con.execute('CREATE TABLE retained(i INTEGER)')
    with db_utils.legacy_connect(target,read_only=True) as con:assert con.execute('SHOW TABLES').fetchall()==[('retained',)]
    with db_utils.legacy_connect(':memory:') as con:con.execute('CREATE TABLE temporary(i INTEGER)')


def test_primary_database_hardlink_alias_cannot_bypass_path_guard(tmp_path,monkeypatch):
    primary=tmp_path/'live';primary.mkdir();source=primary/'kpl_data.duckdb'
    with duckdb.connect(str(source)) as con:con.execute('CREATE TABLE retained(i INTEGER)')
    alias=tmp_path/'alias.duckdb';os.link(source,alias)
    monkeypatch.setattr(db_utils,'primary_checkout',lambda:primary)
    with pytest.raises(ValueError,match='hardlink'):db_utils.legacy_connect(alias)


def frame(days=30,stocks=8):
    return pd.DataFrame([{'datetime':d.strftime('%Y-%m-%d'),'instrument':f'{i:06d}',
        'prediction':float(i),'old_prediction':float(-i),'label_next_ret':float(-i),'ret_20d':float(-i)}
        for d in pd.bdate_range('2025-01-01',periods=days) for i in range(stocks)])


def test_utility_can_conclude_negative_without_promoting_a_model():
    r=evaluate(frame())
    assert r['comparisons']['momentum']['negative_interval']
    assert r['means_pct']['model']<r['means_pct']['momentum']
    assert not r['superiority_proven'] and not r['promotion_allowed']


def test_small_cross_section_cannot_claim_top5_advantage():
    r=evaluate(frame(stocks=5))
    assert r['eligible_days']==0
    assert r['comparisons']['momentum']['status']=='insufficient_eligible_days'


def test_utility_requires_unique_exact_paired_values():
    f=frame()
    with pytest.raises(ValueError,match='duplicate'):evaluate(pd.concat([f,f.iloc[:1]]))
    f.loc[0,'ret_20d']=float('nan')
    with pytest.raises(ValueError,match='nonfinite'):evaluate(f)


def test_inventory_includes_tool_alias_and_dynamic_sql(tmp_path):
    import subprocess
    from tools.v2.closure_readiness import inventory
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/'tools').mkdir()
    (tmp_path/'tools/legacy.py').write_text('import duckdb as db\nc=db.connect(path)\nc.execute(query)\n')
    result=inventory(tmp_path)
    assert {s['kind'] for s in result['sites']}=={'potential_disk_writer','dynamic_sql_requires_connection_boundary'}
    assert not result['all_authority_closed']
    from tools.v2.closure_readiness import writer_gate
    with pytest.raises(ValueError,match='unclassified'):writer_gate(tmp_path)
