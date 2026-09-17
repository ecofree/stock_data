"""Explicit candidate-only columns; never repair labels or default model inputs."""
import json

from trade_system.v2.domain import file_hash, identity, number

PREFIX = 'candidate_identity_'
FEATURES = [PREFIX+x for x in ('net_mf_amount_cny', 'large_net_mf_cny',
    'extra_large_net_mf_cny', 'moneyflow_5d_cny')]


def apply(con, candidate, *, start, end):
    """Caller must replay candidate against the same DB, policy and raw receipts."""
    if not '2024-01-01' <= start <= end <= '2024-12-31':
        raise ValueError('candidate export currently requires verified 2024 calendar')
    rows = candidate['rows']
    if not 1 <= len(rows) <= 128:
        raise ValueError('bounded candidate corpus required')
    seen = set(); windows = set(); values = []
    for row in rows:
        if row['record_id'] != identity({k: v for k, v in row.items() if k != 'record_id'}):
            raise ValueError('candidate record binding differs')
        key = (row['original_price_code'], row['date'])
        if key in seen: raise ValueError('duplicate candidate key')
        seen.add(key)
        lo, hi = row['observation_window']
        if not lo <= row['date'] <= hi: raise ValueError('candidate outside window')
        if hi < start or lo > end: continue
        if not '2024-01-01' <= lo <= hi <= '2024-12-31':
            raise ValueError('candidate window outside verified calendar')
        windows.add((row['entity_key'], row['original_price_code'], lo, hi))
        amount = row['candidate_money_cny']
        eligible = row['candidate_eligible']
        if type(eligible) is not bool or eligible != (amount is not None):
            raise ValueError('candidate eligibility differs')
        net = large = extra = None
        if eligible:
            net = str(number(amount['net_mf_amount']))
            large = str(number(amount['buy_lg_amount'])-number(amount['sell_lg_amount']))
            extra = str(number(amount['buy_elg_amount'])-number(amount['sell_elg_amount']))
        values.append([*key, lo, hi, net, large, extra,
            'candidate_only' if eligible else 'blocked', row['effective_code'],
            row['record_id'], row['money_source']['received_at'] if row['money_source'] else None])
    for entity, code, lo, hi in windows:
        if any(code == c and (lo, hi) != (a, b) and max(lo, a) <= min(hi, b)
               for _, c, a, b in windows):
            raise ValueError('overlapping candidate windows')
    con.execute('''CREATE TEMP TABLE identity_candidate_observations(
      instrument VARCHAR, datetime DATE, lo DATE, hi DATE,
      net DOUBLE, large DOUBLE, extra DOUBLE, status VARCHAR, source_code VARCHAR,
      record_id VARCHAR, received_at VARCHAR, PRIMARY KEY(instrument,datetime))''')
    if values:
        con.executemany('INSERT INTO identity_candidate_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)', values)
    # A row for each open session, including absent source days. Never bridge a gap
    # with the last five observed rows, nor bridge independent observation windows.
    con.execute('''CREATE TEMP TABLE identity_candidate_windows AS
      SELECT value->>0 entity, value->>1 instrument,
             CAST(value->>2 AS DATE) lo, CAST(value->>3 AS DATE) hi
      FROM json_each(?)''', [json.dumps(sorted(windows))])
    invalid = con.execute('''SELECT count(*) FROM identity_candidate_observations o
      LEFT JOIN verified_calendar_overlay c ON c.cal_date=o.datetime AND c.is_open
      WHERE c.cal_date IS NULL''').fetchone()[0]
    if invalid: raise ValueError('candidate on unverified or closed session')
    con.execute('''CREATE TEMP TABLE identity_candidate_grid AS
      SELECT w.entity,w.instrument,c.cal_date datetime,w.lo,w.hi,o.* EXCLUDE(instrument,datetime,lo,hi),
        count(o.net) OVER win observed_days_5d,
        CASE WHEN count(o.net) OVER win=5 THEN sum(o.net) OVER win END moneyflow_5d
      FROM identity_candidate_windows w JOIN verified_calendar_overlay c
        ON c.is_open AND c.cal_date BETWEEN w.lo AND w.hi
      LEFT JOIN identity_candidate_observations o ON o.instrument=w.instrument AND o.datetime=c.cal_date
      WINDOW win AS (PARTITION BY w.entity,w.instrument,w.lo,w.hi ORDER BY c.cal_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)''')
    con.execute(f'''CREATE TEMP TABLE candidate_features AS SELECT f.*,
      c.net {FEATURES[0]},c.large {FEATURES[1]},c.extra {FEATURES[2]},c.moneyflow_5d {FEATURES[3]},
      c.observed_days_5d {PREFIX}observed_days_5d,
      coalesce(c.status,CASE WHEN c.instrument IS NULL THEN 'not_covered' ELSE 'source_missing' END) {PREFIX}status,
      c.source_code {PREFIX}source_code,c.record_id {PREFIX}record_id,
      c.received_at {PREFIX}received_at,c.lo {PREFIX}window_start,c.hi {PREFIX}window_end
      FROM semantic_features f LEFT JOIN identity_candidate_grid c
        ON f.instrument=c.instrument AND f.datetime=c.datetime''')
    before = con.execute('SELECT count(*) FROM semantic_features').fetchone()[0]
    after = con.execute('SELECT count(*) FROM candidate_features').fetchone()[0]
    if before != after: raise ValueError('candidate join multiplication')
    counts = dict(con.execute(f'''SELECT {PREFIX}status,count(*) FROM candidate_features
      WHERE datetime BETWEEN ? AND ? GROUP BY 1 ORDER BY 1''', [start, end]).fetchall())
    return {'scope': 'explicit_candidate_columns_not_default_QLib_features_or_historical_PIT',
        'candidate_payload_sha256': identity(candidate), 'consumer_source_sha256': file_hash(__file__),
        'column_version': 'identity_candidate_columns_v1',
        'candidate_corpus_rows': len(rows), 'consumed_context_rows': len(values),
        'output_status_counts': counts, 'feature_columns': FEATURES,
        'database_sha256': candidate['database_sha256'],
        'rolling_policy': 'five_nonnull_consecutive_open_sessions_within_one_window',
        'default_features_changed': False, 'labels_changed': False,
        'historical_PIT_qualified': False, 'research_ready': False,
        'execution_ready': False, 'production_cutover': False}


def export(db, output, *, candidate, layer, receipts, policy, calendar, research_overlay,
           start_date=None, end_date=None):
    """Explicit historical export; the current exporter never imports this workflow."""
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import os
    import duckdb
    from scripts.export_qlib_features import export_features, apply_calendar_overlay
    from tools.incidents import build_identity_candidate as build
    from trade_system.file_lock import FileLock
    from trade_system.v2.daily_session import seal
    from trade_system.v2.gap_evidence import write_json
    output = build.separate(output, db, candidate, layer, receipts, policy, calendar, research_overlay)
    output.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(output.with_suffix('.export.guard')):
        if output.exists():
            raise ValueError('new historical output required')
        verified = build.verify(candidate, layer, receipts, db, policy)
        consumer_hash = file_hash(__file__)
        with TemporaryDirectory(prefix='identity-export-', dir=output.parent) as scratch:
            scratch = Path(scratch)
            meta = export_features(db, scratch/'base.parquet', start_date=start_date,
                end_date=end_date, output_format='parquet', calendar_overlay=calendar,
                research_overlay=research_overlay, semantics=policy)
            start, end = meta['start_date'], meta['end_date']
            published = scratch/'published'; published.mkdir()
            artifact = published/'features.parquet'
            with duckdb.connect(str(db), read_only=True) as con:
                con.execute('SET threads=2')
                apply_calendar_overlay(con, calendar)
                con.execute('CREATE TEMP TABLE semantic_features AS SELECT * FROM read_parquet(?)',
                            [str(Path(meta['outputs']['parquet'])/'*.parquet')])
                candidate_meta = apply(con, verified, start=start, end=end)
                con.execute("COPY (SELECT * FROM candidate_features ORDER BY datetime,instrument) TO '"+
                            str(artifact).replace("'", "''")+"' (FORMAT PARQUET)")
            if build.verify(candidate, layer, receipts, db, policy) != verified or file_hash(__file__) != consumer_hash:
                raise ValueError('historical candidate evidence changed during export')
            meta.update(identity_candidate=candidate_meta, candidate_feature_columns=candidate_meta['feature_columns'],
                outputs={'parquet': str(output/'features.parquet')}, format='parquet',
                export_storage='single_historical_parquet', export_batches=1,
                artifact_hashes={'features.parquet': file_hash(artifact)},
                scope='fixed_identity_incident_only_not_current_default', research_ready=False, execution_ready=False)
            write_json(published/'features.metadata.json', meta)
            seal(published)
            os.replace(published, output)
            return meta


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'output', 'candidate', 'layer', 'receipts', 'policy', 'calendar', 'research-overlay'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--start-date'); parser.add_argument('--end-date')
    args = parser.parse_args()
    print(json.dumps(export(**vars(args)), ensure_ascii=False))
