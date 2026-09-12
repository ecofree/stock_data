"""Read-only member-verified concept flow shared by collection and review."""
from datetime import date, datetime
import hashlib
import json
import math
import duckdb
from trade_system.quality import table_exists
from trade_system.source_authority import provider_rank_sql
from trade_system.units import _number

THS_MEMBERSHIP_MAX_AGE_DAYS = 10


def _ths_membership_snapshot(con: duckdb.DuckDBPyConnection, trade_date: str):
    """Return (snapshot_date, age_days) for the THS concept membership in effect on
    trade_date, or (None, None) when no membership snapshot exists at or before it."""
    try:
        # Prefer the canonical quality-gated view.  Small legacy/test databases
        # may predate that view, in which case the raw table is the only
        # available relation; production schema initialization always creates
        # the view, so stale/partial rows cannot silently re-enter there.
        relation = (
            "v_default_concept_stock_history"
            if table_exists(con, "v_default_concept_stock_history")
            else "ths_concept_stock_history"
        )
        row = con.execute(
            f"SELECT max(trade_date) FROM {relation} "
            "WHERE trade_date<=CAST(? AS DATE)",
            [trade_date],
        ).fetchone()
        snap = row[0] if row else None
        if snap is None:
            return None, None
        snap_d = snap if isinstance(snap, date) else date.fromisoformat(str(snap)[:10])
        trade_d = date.fromisoformat(str(trade_date)[:10])
        return snap_d, (trade_d - snap_d).days
    except Exception:
        return None, None


def _prepare_ths_aggregate(con, trade_date, *, now=None, max_age_seconds=10800, allow_subset=False):
    """Validate the canonical member set and every member's main flow before publication.

    This certifies consistency with a retained quality-gated snapshot, not an
    independently authenticated online universe or historical arrival time.
    """
    from datetime import timedelta
    now = now or datetime.now()
    if now.tzinfo is not None:
        raise ValueError("use the database's naive local evaluation clock")
    if not 1 <= max_age_seconds <= 86400:
        raise ValueError("THS flow TTL must be between one second and one day")
    snap, age = _ths_membership_snapshot(con, trade_date)
    report = dict(status="missing_inputs", promoted=False, expected_rows=0,
                  fetched_rows=0, rows_written=0, coverage_pct=0.0,
                  ths_membership_snapshot=str(snap) if snap else None,
                  ths_membership_age_days=age, error="", contract="ths-member-aggregate-v1",
                  evaluated_at=now.isoformat(), max_age_seconds=max_age_seconds)
    if snap is None:
        report['error'] = 'quality-gated membership snapshot unavailable'
        return [], report
    if age > THS_MEMBERSHIP_MAX_AGE_DAYS:
        report["status"] = "stale_members"
        report['error'] = 'membership snapshot exceeds maximum age'
        return [], report
    # No raw-table fallback for publication. Both relations must pass the
    # existing snapshot quality gate, including checkpoints.
    catalog = con.execute(
        "SELECT concept_code, concept_name, stock_count FROM v_default_concept_daily "
        "WHERE trade_date=? ORDER BY concept_code LIMIT 10001", [snap]).fetchall()
    members = con.execute(
        "SELECT concept_code, regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') "
        "FROM v_default_concept_stock_history WHERE trade_date=? "
        "ORDER BY concept_code, stock_code LIMIT 500001", [snap]).fetchall()
    if not catalog or not members:
        report['error'] = 'qualified catalogue or member relation is empty'
        return [], report
    if len(catalog) > 10000 or len(members) > 500000:
        report["status"] = "budget_exhausted"
        return [], report
    expected = {str(code): (name, count) for code, name, count in catalog}
    actual = {}
    for concept, stock in members:
        actual.setdefault(str(concept), set()).add(str(stock))
    invalid_codes = any(len(stock) != 6 or not stock.isascii() or not stock.isdigit()
                        for stocks in actual.values() for stock in stocks)
    mismatches = sorted(code for code, (_, count) in expected.items()
                        if count is None or count <= 0 or len(actual.get(code, ())) != count)
    report.update(expected_rows=len(expected),
                  membership_pairs=sum(map(len, actual.values())),
                  membership_sha256=hashlib.sha256(json.dumps(
                      {"date":str(snap), "catalog":catalog, "members":members},
                      sort_keys=True, default=str).encode()).hexdigest(),
                  member_count_mismatches=mismatches[:20])
    if (len(expected) != len(catalog) or set(expected) != set(actual)
            or sum(map(len, actual.values())) != len(members) or invalid_codes or mismatches):
        report["status"] = "partial_members"
        report['error'] = 'catalogue/member identities or per-concept counts disagree'
        return [], report
    provider_order = provider_rank_sql("stock_flow", "provider")
    selected = con.execute(f"""
        SELECT stock_code, main_net, super_net, large_net, mid_net, small_net,
               change_pct, provider, fetched_at, flow_definition
        FROM (
          SELECT *, row_number() OVER (PARTITION BY stock_code
            ORDER BY {provider_order} ASC, fetched_at DESC, provider ASC) AS rn
          FROM multi_source_stock_flow
          WHERE source_date=CAST(? AS DATE) AND is_stale=FALSE
            AND provider IN ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay')
            AND amount_unit='yuan' AND isfinite(main_net)
            AND flow_definition IN ('provider_main_net','provider_main_orders_net','main_orders_net')
            AND fetched_at BETWEEN ? AND ?
        ) WHERE rn=1 ORDER BY stock_code LIMIT 10001
    """, [trade_date, now-timedelta(seconds=max_age_seconds), now]).fetchall()
    if len(selected) > 10000:
        report["status"] = "budget_exhausted"
        return [], report
    flows = {str(row[0]): row for row in selected}
    required_stocks = set().union(*actual.values())
    missing = sorted(required_stocks-set(flows))
    report.update(required_stocks=len(required_stocks),
                  missing_stock_count=len(missing), missing_stock_examples=missing[:20],
                  complete_concepts=sum(stocks <= flows.keys() for stocks in actual.values()),
                  flow_input_sha256=hashlib.sha256(json.dumps(
                      [flows[k] for k in sorted(required_stocks & flows.keys())],
                      default=str).encode()).hexdigest())
    if missing:
        report["status"] = "partial_stock_flow"
        report['error'] = f'{len(missing)} members lack same-date finite, fresh, same-definition native main flow'
        if not allow_subset:
            return [], report
    rows = []
    excluded = []
    fields = ("main_net", "super_net", "large_net", "mid_net", "small_net")
    for code, (name, _) in expected.items():
        absent = sorted(actual[code]-flows.keys())
        if absent:
            excluded.append(dict(sector_code=code, sector_name=name,
                missing_member_count=len(absent), missing_members=absent,
                reason='缺少同日、同口径且未过期的成员资金'))
            continue
        inputs = [flows[s] for s in sorted(actual[code])]
        values = {}
        for index, field in enumerate(fields, start=1):
            numbers = [_number(row[index]) for row in inputs]
            # A partially populated ancillary bucket stays unknown, not a
            # deceptively complete sum over just the available members.
            try:
                values[field] = _number(math.fsum(numbers)) if all(n is not None for n in numbers) else None
            except OverflowError:
                values[field] = None
        if values["main_net"] is None:
            report["status"] = "invalid_aggregate"
            if not allow_subset:
                return [], report
            excluded.append(dict(sector_code=code,sector_name=name,missing_member_count=0,
                                 missing_members=[],reason='主力合计溢出或非法'))
            continue
        changes = [_number(row[6]) for row in inputs]
        change = math.fsum(n / len(changes) for n in changes) if all(n is not None for n in changes) else None
        rows.append(dict(sector_code=code, sector_name=name, date=trade_date,
                         sector_type="ths_concept_derived", amount_unit="yuan",
                         member_codes=sorted(actual[code]),
                         change_pct=_number(change), **values,
                         raw=dict(derived_from="multi_source_stock_flow",
                                  member_stock_count=len(inputs), expected_member_count=len(inputs),
                                  membership_snapshot_date=str(snap),
                                  member_set_sha256=hashlib.sha256(json.dumps(sorted(actual[code])).encode()).hexdigest(),
                                  providers=sorted({row[7] for row in inputs}),
                                  definitions=sorted({row[9] for row in inputs}),
                                  source_assignment_sha256=hashlib.sha256(json.dumps(
                                      [(row[0],row[7],row[9]) for row in inputs]).encode()).hexdigest(),
                                  membership_sha256=report["membership_sha256"],
                                  flow_input_sha256=report["flow_input_sha256"])))
    report.update(status="qualified_subset" if excluded else "complete",
                  eligible_concepts=len(rows), excluded_concepts=excluded,
                  fetched_rows=len(rows), coverage_pct=round(100*len(rows)/len(expected),2))
    return rows, report


def qualified_concept_review(con, trade_date, *, now=None):
    """Read-only subset consumption. No canonical writes or stale fallback."""
    try:
        rows, report = _prepare_ths_aggregate(con, trade_date, now=now, allow_subset=True)
    except Exception as exc:
        rows, report = [], dict(status='unavailable', error=str(exc)[:300], expected_rows=0,
                              eligible_concepts=0, excluded_concepts=[])
    report.update(scope='qualified_subset_not_full_market_or_investment_signal',
                  time_mode='explicit_asof_replay' if now else 'current_clock',
                  full_snapshot_promoted=False)
    return dict(rows=rows, contract=report)


def concept_source_evidence(con, trade_date):
    """Read retained provenance, never infer current subscription rights."""
    result = {'online_entitlement_verified':False, 'scope':'retained_source_and_metric_inventory',
              'preference':['hithink_official','authorized_xiaodefa_relay','other_qualified_sources'],
              'sources':[], 'catalogue':[]}
    try:
        result['catalogue'] = [dict(provider=p,status=s,concepts=n) for p,s,n in con.execute(
            'SELECT provider,status,expected_concepts FROM ths_concept_snapshot_expectation WHERE trade_date=CAST(? AS DATE)',
            [trade_date]).fetchall()]
        result['sources'] = [dict(provider=p,definition=d,amount_unit=u,rows=n,
            role='native_main_flow_candidate' if p in ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay')
                and d in ('provider_main_net','provider_main_orders_net','main_orders_net') and u=='yuan'
                else 'separate_semantics_not_automatically_interchangeable')
            for p,d,u,n in con.execute('SELECT provider,flow_definition,amount_unit,count(*) FROM multi_source_stock_flow '
                'WHERE source_date=CAST(? AS DATE) GROUP BY ALL ORDER BY provider,flow_definition,amount_unit',[trade_date]).fetchall()]
    except Exception as exc:
        result['error'] = str(exc)[:200]
    return result


def matched_concept_history(con, trade_date, current, *, now=None, sessions=3):
    """Same members and provider/measure per concept; exact exchange sessions only.

    Earlier sessions are retrospective at the same wall-clock evaluation time.
    This is descriptive persistence, not historical information-availability proof.
    """
    from datetime import time
    if not 2 <= sessions <= 5:
        raise ValueError('two to five exchange sessions required')
    result = dict(status='unavailable',sessions=[],rows=[],scope='fixed_members_retrospective_not_PIT_or_alpha')
    now = now or datetime.now()
    try:
        days = [str(row[0]) for row in con.execute(
            "SELECT DISTINCT CAST(cal_date AS DATE) FROM tushare_trade_cal "
            "WHERE exchange='SSE' AND CAST(is_open AS BOOLEAN) AND CAST(cal_date AS DATE)<=CAST(? AS DATE) "
            "ORDER BY 1 DESC LIMIT ?",[trade_date,sessions]).fetchall()][::-1]
        if len(days)!=sessions or days[-1]!=trade_date:
            result['reason']='缺少完整交易日历，不按自然日或已有数据日期补猜'
            return result
        result['sessions']=days
        histories=[]
        for day in days[:-1]:
            clock=datetime.combine(date.fromisoformat(day), time(now.hour,now.minute,now.second,now.microsecond))
            snapshot=qualified_concept_review(con,day,now=clock)
            histories.append({r['sector_code']:r for r in snapshot['rows']})
        for row in current['rows']:
            chain=[h.get(row['sector_code']) for h in histories]+[row]
            signature=lambda r:(r['raw'].get('member_set_sha256'),r['raw'].get('source_assignment_sha256'))
            valid=all(r and signature(r)==signature(row) for r in chain)
            values=[r['main_net'] for r in chain] if valid else []
            streak=0
            for value in reversed(values):
                if value<=0: break
                streak+=1
            try:
                total=_number(math.fsum(values)) if values else None
            except OverflowError:
                total=None
            result['rows'].append(dict(sector_code=row['sector_code'],sector_name=row['sector_name'],
                history_status='matched' if valid and total is not None else 'missing_or_members_source_changed',
                sessions=days,main_net=total,positive_days=sum(v>0 for v in values) if valid else None,
                flow_streak=streak if valid else None,
                last_main_net=row['main_net'],values=values,
                member_set_sha256=row['raw']['member_set_sha256']))
        result['status']='matched_subset' if any(r['history_status']=='matched' for r in result['rows']) else 'no_matched_concepts'
    except Exception as exc:
        result['reason']=str(exc)[:200]
    return result


def explain_concept_candidates(candidates, current, history):
    """Append evidence to existing candidates; never create a buy score."""
    history_by={r['sector_code']:r for r in history['rows']}
    links={}
    for concept in current['rows']:
        for code in concept['member_codes']:
            links.setdefault(code,[]).append(concept)
    enriched=[]
    for row in candidates[:50]:
        related=sorted(links.get(str(row['stock_code']),[]),key=lambda r:(-abs(r['main_net']),r['sector_code']))
        evidence=[]
        for concept in related[:3]:
            h=history_by.get(concept['sector_code'],{})
            evidence.append(dict(code=concept['sector_code'],name=concept['sector_name'],main_net=concept['main_net'],
                members=concept['raw']['member_stock_count'],history_status=h.get('history_status','unavailable'),
                positive_days=h.get('positive_days'),flow_streak=h.get('flow_streak'),
                input_sha256=concept['raw']['flow_input_sha256']))
        enriched.append(dict(row,concept_evidence=evidence,
            concept_evidence_status='qualified_subset' if evidence else 'no_qualified_concept_not_negative_signal',
            invalidation='成员资金过期、成员集合变化、来源口径变化或主力净额反向时，需重新核对；不是自动卖出规则。',
            explanation_scope='context_only_not_model_contribution_or_new_ranking'))
    return enriched
