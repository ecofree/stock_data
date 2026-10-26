"""Read-only snapshot scope. Observation time never invents membership validity."""
from datetime import date
import hashlib
import json


def membership_contract(session, snapshot, status, members, *, max_age_days):
    day = date.fromisoformat(str(session))
    source = date.fromisoformat(str(snapshot)) if snapshot is not None else None
    age = (day - source).days if source else None
    keys = sorted((str(concept), str(code)) for concept, codes in members.items() for code in codes)
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate membership identity')
    qualified = bool(keys) and status == 'complete' and age is not None and 0 <= age <= max_age_days
    digest = hashlib.sha256(json.dumps(keys, separators=(',', ':')).encode()).hexdigest()
    return {'schema': 'membership_scope_v1', 'requested_session': str(session),
            'snapshot_session': str(source) if source else None, 'age_days': age,
            'completeness': 'complete_declared_snapshot' if qualified else 'unverified',
            'usable_for_retained_review': qualified,
            'same_session_membership': qualified and age == 0,
            'observed_member_count': len(keys), 'member_keys_sha256': digest,
            'effective_from': None, 'effective_through': None,
            'point_in_time_qualified': False, 'absence_proves_exit': False,
            'boundary': 'snapshot date is not a membership change effective date; no KPL partial fallback'}


def historical_worklist(con):
    """Classify existing debt; no repair, migration, relabelling or guessed facts."""
    tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
    items = []

    def check(key, table, columns, query, action):
        present = {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()} if table in tables else set()
        if not set(columns) <= present:
            items.append(dict(id=key, status='not_assessed_missing_schema', count=None,
                              query=query, disposition=action))
            return
        count = int(con.execute(query).fetchone()[0])
        items.append(dict(id=key, status='review_required' if count else 'no_matches_in_checked_scope',
                          count=count, query=query, disposition=action))

    check('unknown_price_identity', 'multi_source_kline', ('provider', 'adjustment'),
          "SELECT count(*) FROM multi_source_kline WHERE coalesce(trim(provider),'')='' "
          "OR lower(trim(provider))='unknown' OR coalesce(trim(adjustment),'')='' "
          "OR lower(trim(adjustment))='unknown'",
          'recover original product evidence; keep unknown and exclude from qualified uses; never guess adjustment')
    check('duplicate_price_product', 'multi_source_kline',
          ('source_date', 'asset_type', 'asset_code', 'provider', 'adjustment'),
          'SELECT count(*) FROM (SELECT source_date,asset_type,asset_code,provider,adjustment '
          'FROM multi_source_kline GROUP BY ALL HAVING count(*)>1)',
          'compare values and raw receipts under full product key; conflicts quarantined; no automatic deletion')
    check('market_count_lineage', 'daily_summary', ('count_provenance_json',),
          "SELECT count(*) FROM daily_summary WHERE count_provenance_json IS NULL "
          "OR NOT json_valid(count_provenance_json)",
          're-evaluate with market_contract; no fabricated source time or provenance; inspect valid JSON separately')
    for table, keys in (('sector_stocks', 'date,sector_code,stock_code'),
                        ('sector_strength', 'date,sector_code')):
        check(table + '_duplicates', table, keys.split(','),
              f'SELECT count(*) FROM (SELECT {keys} FROM {table} GROUP BY ALL HAVING count(*)>1)',
              'compare per-source values before separately authorized correction; no absent-key deletion')
    items.append({'id': 'legacy_sector_completeness', 'status': 'not_certified', 'count': None,
                  'query': None, 'disposition': 'KPL merged rows have no complete universe receipt; '
                  'absence cannot prove exit or a historical effective interval'})
    return {'schema': 'historical_data_worklist_v1', 'items': items,
            'scope': 'bounded_categories_not_all_data_quality_or_production_audit',
            'repairs_applied': False, 'execution_ready': False}
