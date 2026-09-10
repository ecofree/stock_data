"""Deterministic, evidence-bound observations; no calibrated trading signal.

Membership, price reaction and catalyst evidence are separate. Missing data
changes coverage, never the declared universe. Historical availability
assumptions cannot confer system-replay or execution eligibility.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
import math
from zoneinfo import ZoneInfo

from .domain import identity, instrument, number, utc


@dataclass(frozen=True)
class ContextPolicy:
    version: str
    min_coverage: float
    min_members: int
    min_advancing_fraction: float
    max_theme_universe_fraction: float
    max_candidates: int

    def validate(self):
        if not self.version:
            raise ValueError('explicit experimental policy version required')
        for value in (self.min_coverage, self.min_advancing_fraction, self.max_theme_universe_fraction):
            if not 0 < number(value) <= 1:
                raise ValueError('policy fractions must be finite and in (0,1]')
        if any(type(v) is not int or v <= 0 for v in (self.min_members, self.max_candidates)):
            raise ValueError('positive integer policy limits required')


def _key(kind, row):
    if kind == 'memberships':
        return row['date'], row['theme_id'], row['instrument']
    if kind == 'calendar':
        return row['date'], row['exchange']
    return row['date'], row['instrument']


def _selected(bundle, kind, diagnostics):
    grouped = defaultdict(list)
    asof, day = utc(bundle['asof']), bundle['trade_date']
    for row in bundle.get(kind, []):
        try:
            date.fromisoformat(row['date'])
            source = bundle['products'][row['product']]
            event, received, known = (utc(row[k]) for k in ('event_at','received_at','known_at'))
            if event > received or received > known or not row['revision']:
                raise ValueError('invalid observation time or revision')
            if row['date'] > day or event > asof:
                diagnostics['future_event_excluded'] += 1
                continue
            if bundle['mode'] == 'system_replay' and (received > asof or known > asof):
                diagnostics['not_yet_known_excluded'] += 1
                continue
            if kind != 'calendar':
                instrument(row['instrument'])
            if kind == 'bars':
                if source['unit'] != 'CNY' or source['metric_definition'] != 'unadjusted_daily_bar':
                    raise ValueError('bar contract mismatch')
                if number(row['close']) <= 0 or number(row['amount_cny']) < 0:
                    raise ValueError('invalid bar values')
                number(row['change_pct'])
            elif kind == 'limits':
                if type(row['height']) is not int or row['height'] < 1:
                    raise ValueError('invalid observed ladder height')
            elif kind == 'memberships':
                if row.get('date_verified') is not True or not row['theme_id'] or not isinstance(row.get('theme_name'),str) or not row['theme_name']:
                    raise ValueError('unverified membership date')
            elif kind == 'calendar':
                if type(row['is_open']) is not bool:
                    raise ValueError('explicit calendar status required')
                if row.get('previous_open'):
                    date.fromisoformat(row['previous_open'])
            grouped[_key(kind, row)].append(row)
        except (KeyError, ValueError, TypeError):
            diagnostics[kind+'_invalid_excluded'] += 1
    selected = []
    for rows in grouped.values():
        # Source authority precedes freshness. Multiple gateways carrying the
        # same origin are never counted as separate votes.
        priority = min(bundle['products'][r['product']]['priority'] for r in rows)
        rows = [r for r in rows if bundle['products'][r['product']]['priority'] == priority]
        latest = max(utc(r['received_at']) for r in rows)
        rows = [r for r in rows if utc(r['received_at']) == latest]
        fields = {'bars':('close','change_pct','amount_cny'), 'limits':('height',),
                  'memberships':('theme_id','theme_name'), 'calendar':('is_open','previous_open')}[kind]
        if len({identity({k:r.get(k) for k in fields}) for r in rows}) != 1:
            diagnostics[kind+'_conflict_excluded'] += 1
            continue
        chosen = min(rows, key=identity)
        selected.append({**chosen, 'evidence_id':identity(chosen)})
    return sorted(selected, key=lambda r: _key(kind, r))


def build_context(bundle, policy, account=None):
    policy.validate()
    if bundle['mode'] not in ('system_replay','historical_research'):
        raise ValueError('explicit availability mode required')
    date.fromisoformat(bundle['trade_date'])
    if bundle['trade_date'] > utc(bundle['asof']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
        raise ValueError('market observation date cannot follow query asof')
    if not bundle.get('products'):
        raise ValueError('data product registry required')
    for product in bundle['products'].values():
        required = ('delivery_provider','origin_family','source_api','metric_definition','unit','license_status')
        if not all(isinstance(product.get(k), str) and product[k] for k in required):
            raise ValueError('source identity, metric and entitlement status required')
        if type(product.get('priority')) is not int or product['priority'] < 0:
            raise ValueError('explicit source authority priority required')
    expected = set(bundle['expected_universe'])
    if not expected or len(expected) != len(bundle['expected_universe']):
        raise ValueError('explicit unique expected universe required')
    for code in expected:
        instrument(code)
    diagnostics = Counter()
    rows = {kind:_selected(bundle, kind, diagnostics) for kind in ('bars','memberships','limits','calendar')}
    day = bundle['trade_date']
    calendar = [r for r in rows['calendar'] if r['date'] == day and r['is_open']]
    previous_days = {r.get('previous_open') for r in calendar}
    previous = next(iter(previous_days)) if len(previous_days) == 1 else None
    if previous and previous >= day:
        previous = None
    calendar_ok = bool(previous) and any(r['date'] == previous and r['is_open'] for r in rows['calendar'])
    if not calendar_ok or bundle.get('calendar_authority_verified') is not True:
        diagnostics['calendar_not_certified'] += 1

    def by_day(kind, value):
        return {r['instrument']:r for r in rows[kind] if r['date'] == value and r['instrument'] in expected}

    bars, limits = by_day('bars', day), by_day('limits', day)
    prior_limits = by_day('limits', previous) if calendar_ok else {}
    prior_bars = by_day('bars', previous) if calendar_ok else {}
    coverage = len(bars)/len(expected)
    advance = sum(number(r['change_pct']) > 0 for r in bars.values())
    decline = sum(number(r['change_pct']) < 0 for r in bars.values())
    groups = defaultdict(dict)
    for r in rows['memberships']:
        if r['instrument'] in expected:
            groups[(r['date'],r['theme_id'])][r['instrument']] = r

    def theme_observations(target, prices, pool):
        result = []
        for (theme_day, theme_id), members in groups.items():
            if theme_day != target:
                continue
            observed = sorted(set(members) & set(prices))
            up = [code for code in observed if number(prices[code]['change_pct']) > 0]
            heights = {code:pool[code]['height'] for code in members if code in pool}
            max_height = max(heights.values(), default=0)
            capacity = sorted(observed, key=lambda c:(-number(prices[c]['amount_cny']),c))[:3]
            support, against = [], []
            cov = len(observed)/len(members)
            positive = len(up)/len(observed) if observed else None
            if heights:
                support.append('observed_limit_members')
            else:
                against.append('no_observed_limit_member')
            if cov < policy.min_coverage:
                against.append('member_quote_coverage_insufficient')
            if positive is None or positive < policy.min_advancing_fraction:
                against.append('breadth_support_insufficient')
            if len(members) < policy.min_members:
                against.append('too_few_members')
            if len(members)/len(expected) > policy.max_theme_universe_fraction:
                against.append('broad_membership_basket_not_focused_theme')
            names = sorted({r['theme_name'] for r in members.values()})
            if len(names) != 1 or any('\ufffd' in name for name in names):
                against.append('theme_name_corrupted_or_conflicting')
            result.append({'theme_id':theme_id, 'name':names[0], 'member_count':len(members),
                'observed_members':len(observed), 'quote_coverage':cov, 'advancing_fraction':positive,
                'observed_limit_count':len(heights), 'max_observed_height':max_height,
                'height_roles':sorted(c for c,h in heights.items() if h == max_height),
                'capacity_roles':capacity, 'support':support, 'against':against,
                'catalyst':None, 'catalyst_status':'not_evidenced_membership_is_not_catalyst',
                'role_scope':'observed_dimensions_not_unique_leader',
                'membership_evidence':[r['evidence_id'] for r in members.values()],
                'price_evidence':[prices[c]['evidence_id'] for c in observed]})
        return sorted(result, key=lambda t:(bool(t['against']), -t['observed_limit_count'],
                      -(t['advancing_fraction'] or 0),t['theme_id']))

    themes = theme_observations(day, bars, limits)
    prior_themes = theme_observations(previous, prior_bars, prior_limits) if calendar_ok else []
    prior_ids = {t['theme_id'] for t in prior_themes[:5]}
    now_ids = {t['theme_id'] for t in themes[:5]}
    # Partition one vote per observed limit stock across its memberships. Do
    # not count a stock once per theme when reporting concentration.
    weights = Counter()
    for code in limits:
        attached = [t for (d,t),members in groups.items() if d == day and code in members]
        if not attached:
            attached = ['unmapped']
        for theme_id in attached:
            weights[theme_id] += 1/len(attached)
    concentration = sum((n/len(limits))**2 for n in weights.values()) if limits else None
    promoted = [c for c,r in prior_limits.items() if c in limits and limits[c]['height'] >= r['height']+1]
    failed = sorted(set(prior_limits)-set(promoted))
    failed_observed = [c for c in failed if c in bars]
    failure_mean = sum(float(bars[c]['change_pct']) for c in failed_observed)/len(failed_observed) if failed_observed else None
    blockers = list(bundle.get('gaps', []))
    if bundle['mode'] == 'historical_research':
        blockers.append('historical_availability_assumptions')
    if bundle.get('universe_certified') is not True:
        blockers.append('universe_not_PIT_certified')
    if not calendar_ok or bundle.get('calendar_authority_verified') is not True:
        blockers.append('calendar_not_certified')
    if any(p['license_status'] != 'verified' for p in bundle['products'].values()):
        blockers.append('data_entitlements_unverified')
    if coverage < policy.min_coverage:
        blockers.append('market_quote_coverage_insufficient')
    if bundle.get('limit_pool_complete') is not True:
        blockers.append('limit_pool_completeness_unverified')
    quote_observations = {c:{'change_pct':r['change_pct'],'close':r['close'],'evidence_id':r['evidence_id']}
                          for c,r in bars.items()}
    risks = holding_risks({'quote_observations':quote_observations,'trade_date':day},account)
    held = {r['instrument'] for r in risks}
    candidates = {}
    for theme in themes:
        if theme['against']:
            continue
        for code in theme['height_roles']:
            if code not in bars or code in held:
                continue
            candidate = candidates.setdefault(code, {'instrument':code, 'state':'watch', 'theme_ids':[],
                'support':[], 'opposition':sorted(set(blockers+['catalyst_not_verified','experimental_policy_not_calibrated'])),
                'required_next':['received_final_auction','fresh_session_quote','theme_recheck','funds_evidence','account_risk_recheck'],
                'execution_ready':False, 'signal_impact':'disabled'})
            candidate['theme_ids'].append(theme['theme_id'])
            candidate['support'].append({'theme_id':theme['theme_id'], 'height':theme['max_observed_height'],
                                         'quote_evidence':bars[code]['evidence_id']})
    # Deduplicate before limiting. No promotion to triggered from daily bars.
    candidates = list(candidates.values())[:policy.max_candidates]
    origins = sorted({p['origin_family'] for p in bundle['products'].values()})
    result = {'version':'market-context-v1', 'input_hash':identity(bundle), 'asof':bundle['asof'],
        'trade_date':day, 'mode':bundle['mode'], 'scope':'research_only', 'policy':policy.__dict__,
        'execution_ready':False, 'signal_impact':'disabled', 'blockers':sorted(set(blockers)),
        'source_origin_families':origins, 'diagnostics':dict(diagnostics),
        'market_dimensions':{'expected_universe':len(expected),'observed_quotes':len(bars),
            'coverage':coverage,'advance':advance,'decline':decline,'flat':len(bars)-advance-decline,
            'advance_fraction_observed':advance/len(bars) if bars else None,
            'observed_limit_count':len(limits),'previous_open':previous if calendar_ok else None,
            'calendar_authority_verified':bundle.get('calendar_authority_verified') is True,
            'prior_limit_cohort':len(prior_limits),'promoted_observed':len(promoted),
            'promotion_fraction':len(promoted)/len(prior_limits) if prior_limits else None,
            'nonpromoted_cohort':len(failed),'nonpromoted_observed':len(failed_observed),
            'nonpromoted_mean_return_pct':failure_mean,'fractional_theme_hhi':concentration,
            'top5_rotation_jaccard':1-len(now_ids&prior_ids)/len(now_ids|prior_ids) if now_ids and prior_ids else None,
            'cohort_scope':'observed_pools_not_certified_all_market'},
        'theme_roles':themes,'holding_risks':risks,'conditional_candidates':candidates,
        'quote_observations':quote_observations, 'account_input_hash':identity(account) if account else None,
        'evidence_ids':sorted({r['evidence_id'] for values in rows.values() for r in values})}
    # Detect non-finite arithmetic before persistence or UI rendering.
    if any(isinstance(v,float) and not math.isfinite(v) for v in result['market_dimensions'].values()):
        raise ValueError('non-finite derived market metric')
    return result


def holding_risks(context, account):
    """Position-first observations; never infer sell permission from a bar."""
    result = []
    for position in (account or {}).get('positions', []):
        code = instrument(position['instrument'])
        quote = context['quote_observations'].get(code)
        reasons = ['account_snapshot_requires_current_reconciliation']
        if str(account.get('asof',''))[:10] != context['trade_date']:
            reasons.append('account_and_market_dates_differ')
        if quote is None:
            reasons.append('position_quote_missing')
        elif number(quote['change_pct']) < 0:
            reasons.append('negative_observed_day_return')
        if position.get('sellable') == 0:
            reasons.append('snapshot_reports_no_sellable_quantity')
        result.append({'instrument':code,'reasons':reasons,'quantity':position.get('quantity'),
                       'sellable_snapshot':position.get('sellable'),'change_pct':quote['change_pct'] if quote else None,
                       'evidence_id':quote['evidence_id'] if quote else None,'action':'observe_not_sell_instruction'})
    return sorted(result,key=lambda r:('position_quote_missing' not in r['reasons'],
                                      'negative_observed_day_return' not in r['reasons'],r['instrument']))
