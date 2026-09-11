"""Independent calendar-grid verification of warmup exports, without model fitting."""
import argparse
import json
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2.domain import canonical, file_hash
from trade_system.v2.gap_evidence import write_json
from trade_system.v2.research_receipts import verify


def load_export(pointer):
    pointer = Path(pointer).resolve(strict=True)
    p = json.loads(pointer.read_text())
    path = pointer.parent/p['outputs']['parquet']; mp = path.with_suffix('.metadata.json')
    if file_hash(mp) != p['metadata_sha256']: raise ValueError('metadata changed')
    meta = json.loads(mp.read_text())
    for name, digest in meta['artifact_hashes'].items():
        if file_hash(mp.parent/name) != digest: raise ValueError('export changed')
    return path, meta


def run(db, receipts, pointer, output):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    names = ['scripts/export_qlib_features.py', 'trade_system/v2/research_receipts.py',
             'trade_system/v2/research_history.py', 'tools/v2/probe_research_history.py']
    sources = {p: file_hash(root/p) for p in names}; db_hash = file_hash(db)
    reg, data, receipt_meta = verify(receipts)
    path, meta = load_export(pointer)
    if meta['research_repair']['manifest_id'] != receipt_meta['manifest_id']:
        raise ValueError('receipt/export binding mismatch')
    if meta['flow_features_available'] or any(c.startswith('flow_') for c in meta['feature_columns']):
        raise ValueError('legacy derived flows mixed into repaired export')
    with duckdb.connect(str(db), read_only=True) as c:
        c.execute("SET memory_limit='512MB'"); c.execute('SET threads=1')
        c.execute("CREATE TEMP TABLE factors AS SELECT value->>'stock_code' AS code,CAST(value->>'date' AS DATE) AS trade_day,CAST(value->>'adj_factor' AS DOUBLE) AS factor FROM json_each(?)", [canonical(data['adj_factor'])])
        c.execute("CREATE TEMP TABLE money AS SELECT value->>'stock_code' AS code,CAST(value->>'date' AS DATE) AS trade_day,CAST(value->>'net_mf_amount' AS DOUBLE) AS net,CAST(value->>'buy_lg_amount' AS DOUBLE)-CAST(value->>'sell_lg_amount' AS DOUBLE) AS large,CAST(value->>'buy_elg_amount' AS DOUBLE)-CAST(value->>'sell_elg_amount' AS DOUBLE) AS extra FROM json_each(?)", [canonical(data['moneyflow'])])
        c.execute('CREATE TEMP TABLE sessions(trade_day DATE,seq INTEGER)')
        c.executemany('INSERT INTO sessions VALUES (?,?)', [(d, i) for i, d in enumerate(reg['days'])])
        c.execute('CREATE TEMP TABLE prices AS SELECT * FROM tushare_daily WHERE date BETWEEN ? AND ?', [reg['days'][0], reg['days'][-1]])
        c.execute('CREATE TEMP TABLE exported AS SELECT * FROM read_parquet(?)', [str(path/'*.parquet')])
        # Full calendar grid includes absent bars. No exporter ROWS-over-observations
        # query or its helper functions are reused in this independent calculation.
        c.execute('''CREATE TEMP TABLE independent AS WITH grid AS (
          SELECT s.*,u.code FROM sessions s CROSS JOIN (SELECT DISTINCT stock_code AS code FROM prices) u
        ), vals AS (
          SELECT g.*,p.open*a.factor AS op,p.close*a.factor AS cl,p.volume/a.factor AS vol,
            m.net,m.large,m.extra FROM grid g
          LEFT JOIN prices p ON p.date=g.trade_day AND p.stock_code=g.code AND p.close>0
          LEFT JOIN factors a ON a.trade_day=g.trade_day AND a.code=g.code
          LEFT JOIN money m ON m.trade_day=g.trade_day AND m.code=g.code
        ), lagged AS (
          SELECT *,lag(cl) OVER w AS prev,lag(cl,5) OVER w AS prev5,
            lead(op) OVER w AS nextopen,lead(cl,2) OVER w AS next2close,
            lead(trade_day,2) OVER w AS label_end_time,
            CASE WHEN lag(cl) OVER w>0 THEN (cl/lag(cl) OVER w-1)*100 END AS ret_1d
          FROM vals WINDOW w AS (PARTITION BY code ORDER BY seq)
        ), windows AS (
          SELECT *,count(vol) OVER w20 AS n20,avg(vol) OVER w20 AS av20,stddev_samp(vol) OVER w20 AS sd20,
            count(cl) OVER w6 AS n6,count(ret_1d) OVER w5 AS nr5,stddev_samp(ret_1d) OVER w5 AS sr5,
            count(net) OVER w5 AS nm5,sum(net) OVER w5 AS sm5
          FROM lagged WINDOW w20 AS (PARTITION BY code ORDER BY seq ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
            w6 AS (PARTITION BY code ORDER BY seq ROWS BETWEEN 5 PRECEDING AND CURRENT ROW),
            w5 AS (PARTITION BY code ORDER BY seq ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
        ) SELECT trade_day,code,op AS open,cl AS close,vol AS volume,ret_1d,
          CASE WHEN n6=6 AND prev5>0 THEN (cl/prev5-1)*100 END AS ret_5d,
          CASE WHEN nr5=5 THEN sr5 END AS volatility_5d,
          CASE WHEN n20=20 AND sd20>0 THEN (vol-av20)/sd20 END AS volume_z20,
          n20 AS warmup_price_observations_20d,(n20=20) AS warmup_price_contiguous_20d,
          net AS net_mf_amount,large AS large_net_mf,extra AS extra_large_net_mf,
          CASE WHEN nm5=5 THEN sm5 END AS moneyflow_5d,
          CASE WHEN cl>0 AND nextopen>0 THEN (next2close/nextopen-1)*100 END AS label_next_ret,
          label_end_time FROM windows''')
        fields = ['open', 'close', 'volume', 'ret_1d', 'ret_5d', 'volatility_5d', 'volume_z20',
                  'net_mf_amount', 'large_net_mf', 'extra_large_net_mf', 'moneyflow_5d', 'label_next_ret']
        errors = {}
        for field in fields:
            row = c.execute(f'''SELECT count(*) FILTER(WHERE (e.{field} IS NULL)<>(i.{field} IS NULL)
                OR abs(e.{field}-i.{field})>1e-8*greatest(1,abs(i.{field}))),max(abs(e.{field}-i.{field}))
                FROM exported e LEFT JOIN independent i ON e.datetime=i.trade_day AND e.instrument=i.code''').fetchone()
            errors[field] = {'mismatches': row[0], 'max_absolute_error': row[1]}
        dates = c.execute('''SELECT count(*) FROM exported e LEFT JOIN independent i
          ON e.datetime=i.trade_day AND e.instrument=i.code
          WHERE e.label_end_time IS DISTINCT FROM i.label_end_time
          OR e.warmup_price_observations_20d IS DISTINCT FROM i.warmup_price_observations_20d
          OR e.warmup_price_contiguous_20d IS DISTINCT FROM i.warmup_price_contiguous_20d''').fetchone()[0]
        changed = c.execute('''SELECT count(*) FROM (
          (SELECT datetime,instrument FROM exported EXCEPT SELECT date,stock_code FROM prices WHERE close>0 AND date BETWEEN ? AND ?)
          UNION ALL (SELECT date,stock_code FROM prices WHERE close>0 AND date BETWEEN ? AND ? EXCEPT SELECT datetime,instrument FROM exported))''',
          [meta['start_date'],meta['end_date']]*2).fetchone()[0]
        counts = c.execute('''SELECT count(*),count(label_next_ret),count(volume_z20),count(moneyflow_5d),
          count(*) FILTER(WHERE warmup_price_contiguous_20d),count(DISTINCT instrument),count(DISTINCT datetime) FROM exported''').fetchone()
        missing = [{'instrument': r[0], 'dates': [str(d) for d in r[1]], 'zero_volume_days': r[2]}
                   for r in c.execute('''SELECT p.stock_code,list(p.date ORDER BY p.date),count(*) FILTER(WHERE p.volume=0)
                     FROM prices p LEFT JOIN money m ON m.code=p.stock_code AND m.trade_day=p.date
                     WHERE m.code IS NULL GROUP BY p.stock_code ORDER BY p.stock_code''').fetchall()]
        zeros = c.execute('SELECT count(*) FROM prices WHERE volume=0').fetchone()[0]
    unchanged = (sources == {p: file_hash(root/p) for p in names} and db_hash == file_hash(db)
                 and receipt_meta == verify(receipts)[2] and meta == load_export(pointer)[1])
    result = {'scope': 'historical_calendar_grid_arithmetic_not_PIT_or_model_validation',
        'source_files': sources, 'database_sha256': db_hash, 'inputs_unchanged': unchanged,
        'receipt_manifest_id': receipt_meta['manifest_id'], 'errors': errors, 'date_or_warmup_mismatches': dates,
        'cohort_changes': changed, 'rows': counts[0], 'labeled_rows': counts[1], 'volume_z20_rows': counts[2],
        'moneyflow_5d_rows': counts[3], 'full_price_warmup_rows': counts[4], 'instruments': counts[5],
        'output_sessions': counts[6], 'receipt_sessions': len(reg['days']), 'missing_moneyflow_cases': missing,
        'source_zero_volume_rows': zeros, 'source_zero_volume_not_certified_as_traded': True,
        'legacy_derived_flows_excluded': True, 'research_ready': False, 'execution_ready': False}
    write_json(output/'result.json', result)
    if not unchanged or changed or dates or any(v['mismatches'] for v in errors.values()) or not counts[0]:
        raise ValueError('independent grid/cohort/input verification failed')
    return {k:v for k,v in result.items() if k not in ('source_files','missing_moneyflow_cases')}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'receipts', 'pointer', 'output'): p.add_argument('--'+name, required=True)
    a = p.parse_args(); print(json.dumps(run(a.db,a.receipts,a.pointer,a.output)))
