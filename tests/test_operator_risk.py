"""The explicit paper authority owns risk limits; no score-based permission."""
from dataclasses import replace

import pytest

from tests.test_v2_operator_workflow import seed
from tools.v2.run_event_replay import Clock
from trade_system.v2.decisions import DecisionService, RiskPolicy
from trade_system.v2.accounts import import_snapshot, latest_account
from trade_system.v2.domain import canonical
from trade_system.v2.storage import Store


@pytest.mark.parametrize('field', ['max_total_fraction', 'max_single_fraction'])
def test_position_caps_and_fee_buffer_limit_whole_lots(tmp_path, field):
    with Store(tmp_path/'paper.duckdb', clock=Clock()) as store:
        draft, manifest = seed(store)
        policy = RiskPolicy('explicit', '.7', '.7', 100, 60, 10000, 0)
        service = DecisionService(store, replace(policy, **{field: '.2'}))
        result = service.evaluate('fixture-event-paper', draft['signal_id'], manifest, 'quote')
        assert result['max_quantity'] == 200
        assert not result['execution_ready']
        assert result['allowed_actions'] == ['observe', 'paper_confirm']
        # Fees consume the same explicit limit, never default to a larger score budget.
        service = DecisionService(store, replace(service.policy, fee_buffer_fen=1))
        assert service.evaluate('fixture-event-paper', draft['signal_id'], manifest, 'quote')['max_quantity'] == 100
        service = DecisionService(store, replace(policy, **{field: '.01'}))
        blocked = service.evaluate('fixture-event-paper', draft['signal_id'], manifest, 'quote')
        assert blocked['max_quantity'] == 0
        assert blocked['blockers'] == ['risk_budget_exhausted']




@pytest.mark.parametrize('field', ['cash', 'equity', 'frozen_cash'])
@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', None, True, '-1'])
def test_invalid_account_numbers_never_replace_current_snapshot(tmp_path, field, value):
    with Store(tmp_path/'paper.duckdb', clock=Clock()) as store:
        seed(store)
        before = latest_account(store, 'fixture-event-paper')
        payload = dict(before['payload'], source='manual_declaration')
        payload.pop('ledger_hash', None)
        payload[field] = value
        with pytest.raises(ValueError):
            import_snapshot(store, canonical(payload).encode())
        assert latest_account(store, 'fixture-event-paper')['snapshot_id'] == before['snapshot_id']


@pytest.mark.parametrize("command", ["add", "remove", "close"])
def test_legacy_holdings_commands_cannot_open_database(tmp_path, monkeypatch, command):
    import sys
    from scripts import manage_holdings
    db = tmp_path/"never-created.duckdb"
    monkeypatch.setattr(sys, "argv", ["holdings", "--db", str(db), command])
    monkeypatch.setattr(manage_holdings, "inspect_holdings", lambda *a: pytest.fail("writer command opened database"))
    with pytest.raises(SystemExit) as error:
        manage_holdings.main()
    assert error.value.code == 2 and not db.exists()


def test_legacy_holdings_inspection_preserves_notes_and_closed_positions(tmp_path):
    import duckdb
    from scripts.manage_holdings import inspect_holdings
    db = tmp_path/"holdings.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE holdings(stock_code VARCHAR, entry_date DATE, shares INTEGER, entry_price DOUBLE, status VARCHAR, notes VARCHAR)")
        con.execute("INSERT INTO holdings VALUES ('000001','2026-09-01',100,10,'open','manual thesis'),('000002','2026-09-02',200,20,'closed','manual exit')")
    before = db.read_bytes()
    result = inspect_holdings(db)
    assert result['table_present'] and result['database_writes'] == 0 and not result['execution_ready']
    assert [(r['status'],r['notes']) for r in result['rows']] == [('open','manual thesis'),('closed','manual exit')]
    assert all('last_close' not in r and 'pnl' not in r for r in result['rows'])
    assert db.read_bytes() == before


def test_missing_legacy_holdings_is_not_a_confirmed_empty_account(tmp_path):
    import duckdb
    from scripts.manage_holdings import inspect_holdings
    db = tmp_path/"empty.duckdb"
    with duckdb.connect(str(db)):
        pass
    result = inspect_holdings(db)
    assert not result['table_present'] and not result['execution_ready']
    assert result['scope'] == 'historical_holdings_not_current_account'
