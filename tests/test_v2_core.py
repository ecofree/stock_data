from datetime import timedelta
from dataclasses import replace
import json
from concurrent.futures import ThreadPoolExecutor

import duckdb
import pytest

from trade_system.v2.domain import utc
from trade_system.v2.storage import Store
from trade_system.v2.accounts import import_snapshot, append_account_event, latest_account
from trade_system.v2.strategies import StrategyPolicy, record_signal, transition
from trade_system.v2.decisions import RiskPolicy, DecisionService


class Clock:
    value = utc('2026-09-10T09:31:00+08:00')
    def __call__(self):
        return self.value


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / 'live.duckdb', clock=Clock()) as current:
        yield current


def account_raw(**changes):
    data = {'account_id': 'paper-1', 'mode': 'paper', 'asof': '2026-09-10T09:30:00+08:00',
            'valid_until': '2026-09-10T10:00:00+08:00', 'cash': '4000.00', 'frozen_cash': '0.00',
            'equity': '10000.00', 'positions': [{'instrument': 'SH.600000', 'quantity': 600,
            'sellable': 600, 'mark_price': '10.00'}], 'open_orders': [],
            'declared_complete': True, 'source': 'manual_declaration'}
    data.update(changes)
    return json.dumps(data).encode()


def signal_fixture(store):
    at = store.clock().isoformat()
    store.register_product('quote:v1', 'CNY', 'point', 'confirmation', 'fixture')
    store.ingest('quote:v1', 'SZ.000002', at, 10, '1', b'quote')
    manifest = store.freeze(at)
    policy = StrategyPolicy('fixture:v1', '9', '11', '8', '2026-09-10T09:40:00+08:00')
    sig = record_signal(store, 'SZ.000002', manifest, policy, at=at, price='10',
                        auction_received_at='2026-09-10T09:26:00+08:00', auction_confirmed=True,
                        theme_supported=True, funds_supported=True)
    risk = RiskPolicy('fixture:risk', '.7', '.2', 100, 60, 10000, 0)
    return sig, manifest, policy, DecisionService(store, risk)


def test_cumulative_zero_dedupe_and_late_revision_preserve_frozen_view(store):
    store.register_product('flow:v1', 'CNY', 'cumulative', 'flow_delta', 'one_origin')
    for i, value in enumerate((0, 10, 20, 30)):
        event = store.clock() - timedelta(seconds=4-i)
        store.ingest('flow:v1', 'SZ.000001', event, value, str(i), b'raw')
    repeated = store.ingest('flow:v1', 'SZ.000001', event, 30, str(i), b'raw')
    assert repeated['deduplicated'] == 1
    frozen = store.freeze(store.clock())
    assert store.metric(frozen, 'flow:v1', 'SZ.000001') == 30
    assert store.facts(frozen, 'flow:v1', 'SZ.000001')[0][4] == 0
    store.clock.value += timedelta(seconds=2)
    store.ingest('flow:v1', 'SZ.000001', event, 90, 'revision', b'new raw')
    assert store.metric(frozen, 'flow:v1', 'SZ.000001') == 30
    assert store.metric(store.freeze(store.clock()), 'flow:v1', 'SZ.000001') == 90


@pytest.mark.parametrize('value', [float('nan'), float('inf'), None, True])
def test_invalid_fact_is_not_committed(store, value):
    store.register_product('flow', 'CNY', 'point', 'risk', 'fixture')
    with pytest.raises(ValueError):
        store.ingest('flow', 'SZ.000001', store.clock(), value, 'v1', b'raw')
    assert store.con.execute('SELECT count(*) FROM fact').fetchone()[0] == 0


def test_future_effective_announcements_are_visible_only_after_receipt(store):
    store.register_product('unlock', 'shares', 'announced_event', 'risk', 'fixture')
    earlier = store.freeze(store.clock() - timedelta(seconds=1))
    store.ingest('unlock', 'SZ.000001', store.clock() - timedelta(days=1), 100,
                 'v1', b'announcement', effective_at=store.clock() + timedelta(days=5))
    assert store.metric(earlier, 'unlock', 'SZ.000001') is None
    assert store.metric(store.freeze(store.clock()), 'unlock', 'SZ.000001') == 100


def test_store_refuses_other_thread_and_legacy_database(store, tmp_path):
    with ThreadPoolExecutor(1) as pool:
        with pytest.raises(RuntimeError, match='owning'):
            pool.submit(store.freeze, store.clock()).result()
    legacy = tmp_path / 'legacy.duckdb'
    with duckdb.connect(str(legacy)) as con:
        con.execute('CREATE TABLE human_facts(i INT)')
    with pytest.raises(ValueError, match='not a V2'):
        with Store(legacy):
            pass
    with duckdb.connect(str(legacy), read_only=True) as con:
        assert con.execute('SHOW TABLES').fetchall() == [('human_facts',)]


def test_real_positions_reduce_budget_and_two_confirmations_cannot_double_spend(store):
    imported = import_snapshot(store, account_raw())
    assert imported['reconciled']
    sig, manifest, _, service = signal_fixture(store)
    draft = service.propose('paper-1', sig, manifest, 'quote:v1')
    assert draft['max_quantity'] == 100  # already 60%, total budget 70%
    first = service.confirm(draft['decision_id'], quantity_requested=100, operator='fixture', request_id='one', quote_manifest=manifest)
    assert first['hypothetical_ready'] and first['execution_ready'] is False
    assert service.confirm(draft['decision_id'], quantity_requested=100, operator='fixture', request_id='one', quote_manifest=manifest) == first
    second = service.confirm(draft['decision_id'], quantity_requested=100, operator='fixture', request_id='two', quote_manifest=manifest)
    assert not second['hypothetical_ready']
    assert store.con.execute('SELECT sum(amount_fen) FROM reservation').fetchone()[0] == 100000
    service.mark_unknown(first['reservation_id'])
    assert service.evaluate('paper-1', sig, manifest, 'quote:v1')['max_quantity'] == 0
    assert latest_account(store, 'paper-1')['payload']['positions'][0]['quantity'] == 600


def test_account_events_are_idempotent_and_force_reconciliation(store):
    import_snapshot(store, account_raw())
    sig, manifest, _, service = signal_fixture(store)
    assert append_account_event(store, 'external-1', 'paper-1', 'fill', {'quantity': 100})
    assert not append_account_event(store, 'external-1', 'paper-1', 'fill', {'quantity': 100})
    with pytest.raises(ValueError, match='idempotency'):
        append_account_event(store, 'external-1', 'paper-1', 'fill', {'quantity': 200})
    result = service.evaluate('paper-1', sig, manifest, 'quote:v1')
    assert 'account_event_requires_reconciliation' in result['blockers']


def test_unknown_live_and_unreconciled_accounts_never_approve(store):
    sig, manifest, _, service = signal_fixture(store)
    assert 'account_unknown' in service.evaluate('missing', sig, manifest, 'quote:v1')['blockers']
    import_snapshot(store, account_raw(mode='live'))
    assert 'live_account_not_enabled' in service.evaluate('paper-1', sig, manifest, 'quote:v1')['blockers']
    imported = import_snapshot(store, account_raw(account_id='different', equity='11000'))
    assert not imported['reconciled']
    assert 'account_not_reconciled' in service.evaluate('different', sig, manifest, 'quote:v1')['blockers']


def test_old_quote_cannot_be_refreshed_by_downloading_again(store):
    import_snapshot(store, account_raw())
    sig, manifest, _, service = signal_fixture(store)
    draft = service.propose('paper-1', sig, manifest, 'quote:v1')
    store.clock.value += timedelta(seconds=61)
    store.ingest('quote:v1', 'SZ.000002', store.clock()-timedelta(seconds=61), 10, 'redownload', b'old')
    new_manifest = store.freeze(store.clock())
    result = service.confirm(draft['decision_id'], quantity_requested=100, operator='fixture', request_id='stale', quote_manifest=new_manifest)
    assert not result['hypothetical_ready']
    assert {'quote_stale_or_invalid', 'original_certificate_expired'} <= set(result['blockers'])


def test_pending_external_orders_consume_total_risk_capacity(store):
    raw = account_raw(frozen_cash='1000', open_orders=[{'order_id':'pending', 'instrument':'SZ.000003',
                       'remaining_quantity':100, 'frozen_cash':'1000'}])
    assert import_snapshot(store, raw)['reconciled']
    sig, manifest, _, service = signal_fixture(store)
    assert service.evaluate('paper-1', sig, manifest, 'quote:v1')['max_quantity'] == 0


def test_confirmation_receipt_cannot_be_used_as_another_draft(store):
    import_snapshot(store, account_raw())
    sig, manifest, _, service = signal_fixture(store)
    service = DecisionService(store, replace(service.policy, max_total_fraction='1', max_single_fraction='.4'))
    draft = service.propose('paper-1', sig, manifest, 'quote:v1')
    first = service.confirm(draft['decision_id'], quantity_requested=100, operator='fixture', request_id='first', quote_manifest=manifest)
    assert first['hypothetical_ready']
    second = service.confirm(first['decision_id'], quantity_requested=100, operator='fixture', request_id='second', quote_manifest=manifest)
    assert not second['hypothetical_ready']
    assert 'original_certificate_does_not_allow_confirmation' in second['blockers']
    assert store.con.execute('SELECT count(*) FROM reservation').fetchone()[0] == 1


def test_new_triggered_signal_still_invalidates_old_certificate(store):
    import_snapshot(store, account_raw())
    sig, manifest, policy, service = signal_fixture(store)
    store.clock.value += timedelta(seconds=1)
    later = store.freeze(store.clock())
    record_signal(store, 'SZ.000002', later, policy, at=store.clock().isoformat(), price='10',
                  auction_received_at='2026-09-10T09:26:00+08:00', auction_confirmed=True,
                  theme_supported=True, funds_supported=True)
    assert 'signal_version_superseded' in service.evaluate('paper-1', sig, manifest, 'quote:v1')['blockers']


def test_strategy_waits_for_received_auction_and_expiry_is_terminal():
    policy = StrategyPolicy('v1', '9', '11', '8', '2026-09-10T09:40:00+08:00')
    inputs = dict(at='2026-09-10T09:24:00+08:00', price='10', auction_received_at='2026-09-10T09:26:00+08:00',
                  auction_confirmed=True, theme_supported=True, funds_supported=True, policy=policy)
    assert transition('setup', **inputs)[0] == 'watch'
    assert transition('expired', **inputs)[0] == 'expired'
    inputs['policy'] = replace(policy, entry_low='NaN')
    with pytest.raises(ValueError):
        transition('watch', **inputs)
