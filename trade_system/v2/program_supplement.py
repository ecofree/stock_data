"""Append exact requested historical observations; never change predictions or policy."""
from copy import deepcopy
from pathlib import Path

import duckdb

from .domain import identity, now_utc, utc, number
from .gap_evidence import read_json, write_json
from .paper_ledger import rounded
from .portfolio_replay import replay_variant
from .research_program import load, read_run, implementation as parent_implementation
from .rolling_research import file_hash


def implementation():
    return {**parent_implementation(), 'program_supplement.py':file_hash(Path(__file__))}


def collect(snapshot, requests, asof):
    """Read-only local cache. Stored provider labels are not authentication."""
    added, evidence, missing = [],[],[]
    with duckdb.connect(str(snapshot),read_only=True) as con:
        for code,d in sorted({(r['instrument'],r['date']) for r in requests}):
            exchange,raw = code.split('.')
            prices = con.execute('''SELECT ts_code,open,close,provider,adjustment,fetched_at
                FROM tushare_daily WHERE stock_code=? AND date=?''',[raw,d]).fetchall()
            factors = con.execute('''SELECT ts_code,adj_factor,fetched_at FROM tushare_adj_factor
                WHERE stock_code=? AND date=?''',[raw,d]).fetchall()
            reason = None
            if len(prices) != 1 or len(factors) != 1:
                reason = 'missing_or_duplicate_exact_price_factor'
            else:
                p,f = prices[0],factors[0]
                if p[0] != raw+'.'+exchange or f[0] != p[0] or p[3] not in ('tushare','xiaodefa') or p[4] != 'none':
                    reason = 'identity_provider_or_adjustment_unverified'
                elif any(x is None or number(x) <= 0 for x in (p[1],p[2],f[1])):
                    reason = 'nonpositive_or_unknown_price_factor'
                # Legacy naive timestamps lack a declared timezone. Preserve
                # their text but do not use them to assert point-in-time receipt.
                elif p[5] is None or f[2] is None:
                    reason = 'missing_local_fetch_metadata'
            if reason:
                missing.append({'instrument':code,'date':d,'reason':reason,'can_impute':False})
                continue
            added.append({'instrument':code,'date':d,'open_fen':rounded(number(p[1])*100),
                          'close_fen':rounded(number(p[2])*100),'factor':f[1]})
            evidence.append({'instrument':code,'date':d,'provider_label':p[3],
                'source_tables':['tushare_daily','tushare_adj_factor'],'adjustment':'none',
                'stored_fetch_times':[str(p[5]),str(f[2])],'available_at':utc(asof).isoformat(),
                'provenance':'existing_local_cache_not_live_provider_authenticated',
                'priority_note':'existing_tushare_cache_no_new_provider_call_no_akshare',
                'historical_first_known_at':None})
    return {'bars':added,'evidence':evidence,'missing':missing}


def merge(bundle, additions):
    result = deepcopy(bundle)
    indexed = {(b['instrument'],b['date']):b for b in result['bars']}
    allowed = set(bundle['identity_map'].values())
    for bar in additions:
        key = bar['instrument'],bar['date']
        if key[0] not in allowed or key[1] not in bundle['calendar']:
            raise ValueError('supplement cannot expand calendar or universe')
        if key in indexed and indexed[key] != bar:
            raise ValueError('supplement cannot overwrite frozen observations')
        indexed[key] = bar
    result['bars'] = sorted(indexed.values(),key=lambda b:(b['date'],b['instrument']))
    return result


def register(parent, snapshot, folder):
    folder = Path(folder).resolve()
    if folder.exists():
        raise FileExistsError('new supplement directory required')
    reg,rid,payload = load(parent)
    results = read_run(parent)
    asof = now_utc().isoformat()
    snapshot = Path(snapshot).resolve()
    before = file_hash(snapshot)
    supplement = collect(snapshot,[r for v in results.values() for r in v['data_requests']],asof)
    merged = merge(payload['bundle'],supplement['bars'])
    if before != file_hash(snapshot):
        raise ValueError('snapshot changed during read')
    registration = {'parent':str(Path(parent).resolve()),'parent_id':rid,'snapshot':str(snapshot),
        'snapshot_sha256':before,'registered_at':asof,'implementation':implementation(),
        'supplement_id':identity(supplement),'bundle_id':identity(merged),
        'policy':reg['plan']['policy'],'exposure':'previously_inspected_not_untouched',
        'execution_ready':False}
    folder.mkdir(parents=True,exist_ok=False)
    write_json(folder/'supplement.json',supplement)
    write_json(folder/'registration.json',{**registration,'registration_id':identity(registration)})
    return {'added':len(supplement['bars']),'missing':len(supplement['missing'])}


def read_inputs(folder):
    folder = Path(folder)
    r = read_json(folder/'registration.json')[0]
    rid = r.pop('registration_id')
    if identity(r) != rid or r['implementation'] != implementation():
        raise ValueError('supplement registration or implementation changed')
    parent,pid,payload = load(r['parent'])
    read_run(r['parent'])
    s = read_json(folder/'supplement.json')[0]
    if pid != r['parent_id'] or identity(s) != r['supplement_id']:
        raise ValueError('supplement source changed')
    bundle = merge(payload['bundle'],s['bars'])
    if identity(bundle) != r['bundle_id'] or r['policy'] != parent['plan']['policy']:
        raise ValueError('supplement changed sample or policy')
    return r,rid,payload,bundle


def execute(folder):
    folder = Path(folder)
    r,rid,p,bundle = read_inputs(folder)
    out = folder/'run'
    out.mkdir(exist_ok=False)
    for name in sorted(bundle['predictions']):
        result = replay_variant(bundle,p['base_policy'],r['policy'],p['fields']['cases'],
            p['prior_actions'],name,repair_asof=r['registered_at'])
        write_json(out/(name+'.json'),result)
    read_inputs(folder)
    m = {'registration_id':rid,'artifacts':{x.name:file_hash(x) for x in out.iterdir()}}
    write_json(out/'completed.json',{**m,'manifest_id':identity(m)})
    return verify(folder)


def verify(folder, *, recompute=False):
    folder = Path(folder)
    r,rid,p,bundle = read_inputs(folder)
    out = folder/'run'
    m = read_json(out/'completed.json')[0]
    mid = m.pop('manifest_id')
    if identity(m) != mid or m['registration_id'] != rid:
        raise ValueError('invalid supplement completion')
    members = list(out.iterdir())
    if any(x.is_symlink() or not x.is_file() for x in members):
        raise ValueError('invalid supplement member')
    if {x.name:file_hash(x) for x in members if x.name != 'completed.json'} != m['artifacts']:
        raise ValueError('supplement artifacts changed')
    if set(m['artifacts']) != {v+'.json' for v in bundle['predictions']}:
        raise ValueError('full variant set required')
    results = {v:read_json(out/(v+'.json'))[0] for v in bundle['predictions']}
    for v,result in results.items():
        if result['variant'] != v or result['full_input_id'] != identity(bundle) or result['execution_ready'] is not False:
            raise ValueError('supplement result identity mismatch')
        if recompute and result != replay_variant(bundle,p['base_policy'],r['policy'],p['fields']['cases'],
                p['prior_actions'],v,repair_asof=r['registered_at']):
            raise ValueError('supplement replay differs')
    return results
