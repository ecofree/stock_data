"""Business counterexamples from the October audit, on isolated databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors.collect_market import collect_market_rise_fall
from trade_system.data_store import DuckDBStore
from trade_system.pipeline_runtime import RunManifest
from trade_system.schema import init_schema


@pytest.mark.parametrize('kind',['company','plates','sector_fallback'])
def test_other_replacement_collectors_preserve_committed_rows_on_failure(tmp_path,monkeypatch,kind):
    from collectors.collect_stock import collect_stock_company_info
    from collectors.collect_sector import collect_sector_plates, _collect_sector_ranking_fallback
    with closing(DuckDBStore(tmp_path/'replacement.duckdb')) as store:
        init_schema(store.conn)
        store.conn.execute("INSERT INTO stock_company_info(stock_code,company_name,market_cap) VALUES ('000001','original',100)")
        store.conn.execute("INSERT INTO sector_plates(sector_code,sector_name) VALUES ('A','original')")
        store.conn.execute("INSERT INTO sector_ranking(date,sector_code,sector_name) VALUES ('2026-10-12','A','original')")
        store.conn.execute("INSERT INTO sector_strength(date,sector_code,strength_value) VALUES ('2026-10-12','A',5)")
        store.conn.execute("INSERT INTO sector_stocks(date,sector_code,stock_code) VALUES ('2026-10-12','A','000001')")
        tables=('stock_company_info','sector_plates','sector_ranking','sector_strength','sector_stocks')
        before={t:store.conn.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
        payloads={'/stock/company-info':{'company_name':'new','market_cap':'invalid-number'},
            '/sector/plates':{'plates':[{'code':'B','name':'new'}]},
            '/sector/strength':{'date':'2026-10-12','strength':10,'zhangting':1},
            '/sector/stocks':[{'code':'000002','name':'new'}]}
        client=SimpleNamespace(get=lambda endpoint,*a:payloads[endpoint])
        real=store.insert_rows
        def injected(table,*args,**kwargs):
            if table==('sector_plates' if kind=='plates' else 'sector_stocks'):
                raise OSError('isolated publish fault')
            return real(table,*args,**kwargs)
        if kind=='company':
            assert collect_stock_company_info(client,store,'2026-10-12',['000001'])==0
        else:
            monkeypatch.setattr(store,'insert_rows',injected)
            with pytest.raises(OSError,match='publish fault'):
                (collect_sector_plates if kind=='plates' else _collect_sector_ranking_fallback)(client,store,'2026-10-12')
        assert {t:store.conn.execute(f'SELECT * FROM {t}').fetchall() for t in tables}==before


@pytest.mark.parametrize('failure', ['bad_count', 'insert_overflow', 'summary_failure'])
def test_failed_market_batch_preserves_both_original_relations(tmp_path, failure):
    with closing(DuckDBStore(tmp_path/'market.duckdb')) as store:
        init_schema(store.conn)
        store.conn.execute("INSERT INTO market_rise_fall(date,limit_up_count,raw_json) VALUES ('2026-10-12',42,'original')")
        store.conn.execute("INSERT INTO daily_summary(date,limit_up_count,raw_json) VALUES ('2026-10-12',42,'original')")
        before = {t: store.conn.execute(f'SELECT * FROM {t}').fetchall() for t in ('market_rise_fall','daily_summary')}
        real = store.conn
        class FailingProjection:
            def execute(self, sql, params=None):
                if sql.startswith('INSERT INTO daily_summary'):
                    raise OSError('injected projection write failure')
                return real.execute(sql, params) if params else real.execute(sql)
            def executemany(self, *args):
                return real.executemany(*args)
        if failure == 'summary_failure':
            store.conn = FailingProjection()
        count = 'invalid_integer' if failure == 'bad_count' else 10**40 if failure == 'insert_overflow' else 10
        client = SimpleNamespace(get=lambda *a, **k: {'raw_data': [[count,2,3,7,30,2,'2026-10-12']]})
        assert collect_market_rise_fall(client, store, '2026-10-12') == 0
        store.conn = real
        assert {t: real.execute(f'SELECT * FROM {t}').fetchall() for t in before} == before


def test_duplicate_attempt_cannot_overwrite_failed_receipt(tmp_path, monkeypatch):
    from trade_system import pipeline_runtime as runtime
    monkeypatch.setattr(runtime, 'runtime_fingerprint', lambda: {'fixture': True})
    original = RunManifest(tmp_path, 'same', '2026-10-12', 'close')
    original.add_step('collect', 'failed', [], error='provider degraded')
    original.finish('failed')
    before = original.path.read_bytes()
    with pytest.raises(FileExistsError):
        RunManifest(tmp_path, 'same', '2026-10-12', 'close')
    assert original.path.read_bytes() == before
    assert json.loads(before)['steps'][0]['error'] == 'provider degraded'


def test_concurrent_attempt_allocation_has_one_owner(tmp_path, monkeypatch):
    from trade_system import pipeline_runtime as runtime
    monkeypatch.setattr(runtime, 'runtime_fingerprint', lambda: {'fixture': True})
    def allocate(_):
        try:
            manifest = RunManifest(tmp_path, 'collision', '2026-10-12', 'close')
            manifest.finish('completed')
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(allocate, range(4))) == 1
    assert json.loads((tmp_path/'runs/collision/run.json').read_text())['status'] == 'completed'


@pytest.mark.parametrize('log_error', [False, True])
def test_dependency_failure_keeps_diagnostic_and_failed_receipt(tmp_path, monkeypatch, log_error):
    from scripts import run_integrated_daily as runner
    from trade_system import pipeline_runtime as runtime
    monkeypatch.setattr(runtime, 'runtime_fingerprint', lambda: {'fixture': True})
    manifest = RunManifest(tmp_path, 'preflight', '2026-10-12', 'close')
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=b'broken dependency', stderr=b''))
    if log_error:
        original = Path.write_text
        def fail_log(path, *args, **kwargs):
            if path.name == 'dependency-check.log':
                raise OSError('disk full')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path, 'write_text', fail_log)
    with pytest.raises(ValueError, match='dependency check failed'):
        runner._check_collector_dependencies('fixture-python', manifest)
    receipt=json.loads(manifest.path.read_text())
    assert receipt['status'] == 'preflight_failed'
    assert 'broken dependency' in (receipt['error'] if log_error else (manifest.run_dir/'dependency-check.log').read_text())


def test_cold_split_refuses_late_data_and_preserves_both_files(tmp_path, monkeypatch):
    import duckdb
    from scripts.split_cold_storage import main
    hot=tmp_path/'hot.duckdb';cold=tmp_path/'cold.duckdb'
    for p, day in ((hot,'2025-12-15'),(cold,'2025-12-01')):
        with duckdb.connect(str(p)) as con:
            con.execute('CREATE TABLE kline(date DATE, stock_code VARCHAR, close DOUBLE)')
            con.execute('INSERT INTO kline VALUES (?, ?, ?)',[day,'000001',10])
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (hot,cold)}
    for cutoff in ('2026-01-01','2026-02-01'):
        monkeypatch.setattr('sys.argv',['split_cold_storage','--db',str(hot),'--cold-db',str(cold),'--before',cutoff,'--tables','kline','--execute'])
        assert main() == 2
        assert {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (hot,cold)} == before
