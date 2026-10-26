"""Explicit observation scope; never an account or execution qualification."""
from pathlib import Path

SCOPE = 'market_research_observation_v1'


def from_config(config):
    value = config.get('acceptance_scope')
    if value is None:
        return None  # Historical workspaces keep their original account checks.
    if value != SCOPE:
        raise ValueError('unknown acceptance scope')
    if any(config.get(k) for k in ('account_database', 'account_id')):
        raise ValueError('observation scope conflicts with account configuration')
    return SCOPE


def for_workspace(output):
    from .gap_evidence import read_json
    path = Path(output) / 'workspace-config.json'
    return from_config(read_json(path)[0] if path.exists() else {})


def account_not_applicable():
    return {'status': 'not_applicable_current_scope', 'acceptance_scope': SCOPE,
            'execution_ready': False, 'real_statement_acceptance': False,
            'positions': [], 'open_orders': []}


def observation_only(account):
    return (account or {}).get('acceptance_scope') == SCOPE and (account or {}).get('status') == 'not_applicable_current_scope'
