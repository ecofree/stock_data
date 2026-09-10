"""Deterministic conditional state machine; no I/O and no default thresholds."""
from dataclasses import dataclass
import json
from zoneinfo import ZoneInfo

from .domain import canonical, identity, instrument, money, utc


@dataclass(frozen=True)
class StrategyPolicy:
    version: str
    entry_low: str
    entry_high: str
    invalid_below: str
    expires_at: str
    test_only: bool = True


def transition(previous, *, at, price, auction_received_at, auction_confirmed,
               theme_supported, funds_supported, policy):
    at, expiry = utc(at), utc(policy.expires_at)
    low, high, invalid = money(policy.entry_low), money(policy.entry_high), money(policy.invalid_below)
    if not 0 < invalid < low <= high or not policy.version:
        raise ValueError('invalid frozen strategy policy')
    if previous not in ('setup', 'watch', 'armed', 'triggered', 'invalidated', 'expired'):
        raise ValueError('unknown strategy state')
    if previous in ('invalidated', 'expired'):
        return previous, ['terminal_state']
    if at >= expiry:
        return 'expired', ['strategy_window_expired']
    if theme_supported is False:
        return 'invalidated', ['theme_support_lost']
    if price is not None and money(price) < invalid:
        return 'invalidated', ['price_invalidated']
    if auction_received_at is None or utc(auction_received_at) > at or auction_confirmed is not True:
        return 'watch', ['auction_result_not_known_or_unconfirmed']
    if price is None or theme_supported is not True or funds_supported is not True:
        return 'armed', ['waiting_for_required_confirmation']
    if low <= money(price) <= high:
        return 'triggered', ['frozen_conditions_met']
    return 'armed', ['outside_entry_range']


def record_signal(store, code, manifest_id, policy, *, previous='setup', evidence=None,
                  setup_id='daily', run_scope='default', **inputs):
    store.check_owner()
    instrument(code)
    manifest = store.con.execute('SELECT asof_time,mode FROM input_manifest WHERE manifest_id=?', [manifest_id]).fetchone()
    if manifest is None or utc(inputs['at']) != manifest[0]:
        raise ValueError('signal requires a same-time frozen manifest')
    inputs['at'] = manifest[0].isoformat()
    if not all(isinstance(v,str) and v.strip() and len(v)<=120 for v in (setup_id,run_scope)):
        raise ValueError('bounded explicit setup and run scope required')
    session_id = manifest[0].astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    instance_id = identity([code,policy.version,session_id,setup_id,manifest[1],run_scope])
    old = store.con.execute('''SELECT state,asof_time,payload FROM signal_event
        WHERE json_extract_string(payload,'$.instance_id')=?
        ORDER BY asof_time DESC,rowid DESC LIMIT 1''', [instance_id]).fetchone()
    if old and old[1] > manifest[0]:
        raise ValueError('late events cannot rewrite earlier emitted signals')
    if old:
        if json.loads(old[2])['policy'] != policy.__dict__:
            raise ValueError('strategy version is immutable; changed policy needs a new version')
        if (json.loads(old[2]).get('evidence') or {}).get('policy') != (evidence or {}).get('policy'):
            raise ValueError('event evidence policy changed; a new strategy version is required')
        previous = old[0]
    state, reasons = transition(previous, policy=policy, **inputs)
    payload = {'instrument': code, 'state': state, 'strategy_version': policy.version,
               'instance_id':instance_id,'session_id':session_id,'setup_id':setup_id,'run_scope':run_scope,
               'parameter_set_id':identity({k:v for k,v in policy.__dict__.items() if k not in ('version','expires_at')}),
               'manifest_id': manifest_id, 'asof': manifest[0].isoformat(), 'mode': manifest[1],
               'policy': policy.__dict__, 'inputs': inputs, 'reasons': reasons,
               'scope': 'research_only', 'execution_ready': False}
    if evidence is not None:
        payload['evidence'] = evidence
    key = identity(payload)
    store.con.execute('INSERT INTO signal_event VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                      [key, code, policy.version, state, manifest[0], manifest_id, canonical(payload)])
    return key


def latest_instance_signal(store, current):
    """Historical unscoped events stay separate; never supersede another run."""
    return store.con.execute('''SELECT signal_id,state FROM signal_event
        WHERE instrument=? AND strategy_version=?
        AND coalesce(json_extract_string(payload,'$.instance_id'),'legacy_unscoped')=?
        ORDER BY asof_time DESC,rowid DESC LIMIT 1''',
        [current['instrument'],current['strategy_version'],current.get('instance_id','legacy_unscoped')]).fetchone()


def signal(store, signal_id):
    row = store.con.execute('SELECT payload FROM signal_event WHERE signal_id=?', [signal_id]).fetchone()
    if row is None:
        raise ValueError('unknown signal')
    return json.loads(row[0])
