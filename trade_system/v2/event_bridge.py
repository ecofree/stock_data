"""Typed received events -> frozen strategy inputs; never infer auction finality."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from zoneinfo import ZoneInfo

from .domain import canonical, identity, instrument, money, number, quantity, utc
from .strategies import StrategyPolicy, record_signal, transition


SQL = '''CREATE TABLE market_event(
 event_id VARCHAR PRIMARY KEY, dataset VARCHAR NOT NULL, instrument VARCHAR NOT NULL,
 kind VARCHAR NOT NULL, event_at TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL,
 seq BIGINT UNIQUE NOT NULL, mode VARCHAR NOT NULL, raw_hash VARCHAR NOT NULL, payload JSON NOT NULL)'''
KINDS = {'auction_final':('CNY','point'), 'auction_indicative':('CNY','point'),
         'quote':('CNY','point'), 'funds_cumulative':('CNY','cumulative'),
         'theme_breadth':('count','point')}


def ensure_events(store):
    store.check_owner()
    digest = hashlib.sha256(SQL.encode()).hexdigest()
    row = store.con.execute('SELECT sha256 FROM v2_schema WHERE version=3').fetchone()
    if row:
        if row[0] != digest:
            raise ValueError('market event migration checksum mismatch')
        return
    with store.transaction():
        store.con.execute(SQL)
        store.con.execute('INSERT INTO v2_schema VALUES (3,?)',[digest])


def ingest_event(store, dataset, code, kind, event_at, payload, source_event_id, *, mode='system_replay'):
    ensure_events(store)
    instrument(code)
    if kind not in KINDS or mode not in ('system_replay','historical_research') or not isinstance(source_event_id,str) or not source_event_id:
        raise ValueError('typed event kind, availability mode and source event identity required')
    product = store.con.execute('SELECT unit,semantics,consumer FROM data_product WHERE dataset=?',[dataset]).fetchone()
    if product != (*KINDS[kind], 'event:'+kind):
        raise ValueError('event product contract mismatch')
    event, received = utc(event_at),utc(store.clock())
    if event > received:
        raise ValueError('future event not yet received')
    if kind in ('auction_final','auction_indicative','quote'):
        if money(payload['price']) <= 0:
            raise ValueError('positive observed price required')
    if kind.startswith('auction_'):
        if payload.get('final') is not (kind == 'auction_final'):
            raise ValueError('explicit source finality required; indication is not final')
        if number(payload['relative_volume']) < 0:
            raise ValueError('nonnegative auction comparison required')
    elif kind == 'quote':
        if payload.get('phase') not in ('continuous','auction','closed'):
            raise ValueError('explicit market phase required')
    elif kind == 'funds_cumulative':
        number(payload['net_cny'])  # signed cumulative amount, never sum samples
        if not payload.get('metric_version'):
            raise ValueError('frozen funds metric definition required')
    elif kind == 'theme_breadth':
        expected, observed, advancing = (quantity(payload[k]) for k in ('expected','observed','advancing'))
        if not 0 <= advancing <= observed <= expected or not expected or not payload.get('theme_id') or not payload.get('membership_version'):
            raise ValueError('explicit theme membership version and coherent coverage required')
    body = {'dataset':dataset,'instrument':code,'kind':kind,'event_at':event.isoformat(),
            'payload':payload,'source_event_id':source_event_id,'mode':mode}
    key = identity([dataset,code,source_event_id,mode])
    old = store.con.execute('SELECT payload FROM market_event WHERE event_id=?',[key]).fetchone()
    if old:
        if old[0] != canonical(body):
            raise ValueError('event idempotency conflict; corrections need new source event ids')
        return {'event_id':key,'inserted':False}
    raw_hash = store.archive(canonical(body).encode())
    with store.transaction():
        seq = store.con.execute('SELECT coalesce(max(seq),0)+1 FROM market_event').fetchone()[0]
        store.con.execute('INSERT INTO market_event VALUES (?,?,?,?,?,?,?,?,?,?)',
                          [key,dataset,code,kind,event,received,seq,mode,raw_hash,canonical(body)])
        if kind == 'quote' and mode == 'system_replay':
            # Quote fact and event commit atomically. Historical imports do
            # not populate executable-path numeric facts.
            fact_id = identity([dataset,code,event.isoformat(),float(number(payload['price'])),key])
            fact_seq = store.con.execute('SELECT coalesce(max(seq),0)+1 FROM fact').fetchone()[0]
            store.con.execute('INSERT INTO fact VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                              [fact_id,dataset,code,event,received,received,fact_seq,float(number(payload['price'])),key,raw_hash,None])
    return {'event_id':key,'inserted':True}


@dataclass(frozen=True)
class EventPolicy:
    version: str
    datasets: dict
    theme_id: str
    membership_version: str
    fund_metric_version: str
    entry_not_before: str
    auction_not_before: str
    min_auction_relative_volume: str
    min_fund_delta_cny: str
    fund_window_seconds: int
    fresh_seconds: int
    min_theme_coverage: str
    min_theme_advancing: str

    def validate(self):
        if not all((self.version,self.theme_id,self.membership_version,self.fund_metric_version)):
            raise ValueError('explicit event policy and evidence versions required')
        if set(self.datasets) != {'auction_final','quote','funds_cumulative','theme_breadth'}:
            raise ValueError('all required event datasets must be frozen')
        if len(set(self.datasets.values())) != 4:
            raise ValueError('event products must remain distinct')
        if utc(self.auction_not_before) > utc(self.entry_not_before):
            raise ValueError('invalid frozen event window')
        for value in (self.fund_window_seconds,self.fresh_seconds):
            if type(value) is not int or value <= 0:
                raise ValueError('positive observation budgets required')
        for value in (self.min_theme_coverage,self.min_theme_advancing):
            if not 0 < number(value) <= 1:
                raise ValueError('finite theme fractions required')
        if number(self.min_auction_relative_volume) < 0:
            raise ValueError('nonnegative comparison threshold required')
        number(self.min_fund_delta_cny)


def derive_inputs(store, code, policy):
    ensure_events(store)
    policy.validate()
    at = utc(store.clock())
    day = at.astimezone(ZoneInfo('Asia/Shanghai')).date()
    selected, ids, blockers, expires = {}, [], [], []
    for kind,dataset in policy.datasets.items():
        contract = store.con.execute('SELECT unit,semantics,consumer FROM data_product WHERE dataset=?',[dataset]).fetchone()
        if contract != (*KINDS[kind],'event:'+kind):
            raise ValueError('frozen strategy event product mismatch')
        records = store.con.execute('''SELECT event_id,event_at,received_at,payload,mode,raw_hash FROM market_event
            WHERE dataset=? AND instrument=? AND event_at<=? AND received_at<=?
            QUALIFY row_number() OVER(PARTITION BY event_at ORDER BY seq DESC)=1
            ORDER BY event_at,received_at''',[dataset,code,at,at]).fetchall()
        records = [r for r in records if r[1].astimezone(ZoneInfo('Asia/Shanghai')).date()==day]
        if kind == 'funds_cumulative':
            records = [r for r in records if (at-r[1]).total_seconds()<=policy.fund_window_seconds]
        records = records[-2:] if kind == 'funds_cumulative' else records[-1:]
        selected[kind] = records
        for row in records:
            body = json.loads(row[3])
            if (hashlib.sha256(row[3].encode()).hexdigest()!=row[5] or
                body['dataset']!=dataset or body['instrument']!=code or body['kind']!=kind or
                utc(body['event_at'])!=row[1] or body['mode']!=row[4] or
                identity([dataset,code,body['source_event_id'],row[4]])!=row[0]):
                raise ValueError('market event identity/input checksum mismatch')
            ids.append(row[0])
            if row[4] != 'system_replay':
                blockers.append('historical_event_not_system_evidence')
        if not records:
            blockers.append(kind+'_missing')
        elif kind != 'auction_final':
            end = min(records[-1][1],records[-1][2])+timedelta(seconds=policy.fresh_seconds)
            expires.append(end)
            if end <= at:
                blockers.append(kind+'_stale')
    def last(kind):
        return json.loads(selected[kind][-1][3])['payload'] if selected[kind] else None
    auction,quote,theme = (last(k) for k in ('auction_final','quote','theme_breadth'))
    confirmed = bool(auction and auction['final'] is True and
                     selected['auction_final'][-1][1] >= utc(policy.auction_not_before) and
                     number(auction['relative_volume']) >= number(policy.min_auction_relative_volume))
    theme_ok = None
    if theme:
        if theme['theme_id'] != policy.theme_id or theme['membership_version'] != policy.membership_version:
            blockers.append('theme_membership_binding_changed')
        elif number(theme['observed'])/number(theme['expected']) >= number(policy.min_theme_coverage):
            theme_ok = number(theme['advancing'])/number(theme['observed']) >= number(policy.min_theme_advancing) if theme['observed'] else None
    delta = None
    fund_rows = selected['funds_cumulative']
    if len(fund_rows) == 2:
        older,newer = (json.loads(r[3])['payload'] for r in fund_rows)
        if older['metric_version'] == newer['metric_version'] == policy.fund_metric_version:
            delta = number(newer['net_cny'])-number(older['net_cny'])
        else:
            blockers.append('fund_metric_binding_changed')
    if delta is None:
        blockers.append('two_distinct_cumulative_observations_required')
    if quote and quote['phase'] != 'continuous':
        blockers.append('continuous_quote_required')
    if at < utc(policy.entry_not_before):
        blockers.append('entry_window_not_started')
    # Stale or unbound negative theme data must not invalidate a signal; it
    # means unknown until fresh evidence is available.
    if any(b.startswith('theme_') for b in blockers):
        theme_ok = None
    inputs = {'at':at.isoformat(),'price':quote['price'] if quote and not blockers else None,
              'auction_received_at':selected['auction_final'][-1][2].isoformat() if auction else None,
              'auction_confirmed':confirmed, 'theme_supported':theme_ok,
              'funds_supported':delta is not None and delta >= number(policy.min_fund_delta_cny) and not blockers}
    return inputs, {'kind':'auction_funds_v1','policy':policy.__dict__,'event_ids':sorted(ids),
                    'valid_until':min(expires).isoformat() if expires else at.isoformat(),
                    'fund_delta_cny':str(delta) if delta is not None else None,'blockers':sorted(set(blockers))}


def event_signal(store, code, strategy_policy, event_policy):
    policy = EventPolicy(**event_policy)
    inputs,evidence = derive_inputs(store,code,policy)
    manifest = store.freeze(store.clock())
    return record_signal(store,code,manifest,StrategyPolicy(**strategy_policy),evidence=evidence,**inputs)


def recheck_signal_evidence(store, signal):
    evidence = signal.get('evidence',{})
    if evidence.get('kind') != 'auction_funds_v1':
        return []
    inputs,current = derive_inputs(store,signal['instrument'],EventPolicy(**evidence['policy']))
    state,_ = transition(signal['state'],policy=StrategyPolicy(**signal['policy']),**inputs)
    blockers = list(current['blockers'])
    if current['event_ids'] != evidence['event_ids']:
        blockers.append('signal_event_evidence_changed')
    if utc(store.clock()) >= utc(evidence['valid_until']) or state != 'triggered':
        blockers.append('signal_event_conditions_no_longer_valid')
    return blockers
