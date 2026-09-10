import json
from tools.v2.run_paper_demo import run_demo
from trade_system.v2.publisher import read_current
from trade_system.v2.reporting import render_review


def test_close_to_next_close_demo_preserves_separate_ledgers(tmp_path):
    output = tmp_path / 'demo'
    result = run_demo(output)
    assert result['signal_events'] == 6
    _, artifacts = read_current(output/'reports')
    data = json.loads(artifacts['evidence.json'])
    # Previous-session observation is retained separately, not overwritten by
    # today's terminal state for the same algorithm/instrument.
    assert {s['state'] for s in data['signals'] if s['session_id']=='2026-09-10'} == {'expired','invalidated','armed'}
    assert {s['state'] for s in data['signals'] if s['session_id']=='2026-09-09'} == {'watch'}
    assert data['account']['positions'][0]['quantity'] == 600
    assert data['reserved_fen'] == 100000
    assert data['execution_ready'] is False and data['fixture_only'] is True
    assert any(d['hypothetical_ready'] for d in data['decisions'])
    assert all(d['execution_ready'] is False for d in data['decisions'])
    assert '2026-09-10-plans.csv' in artifacts
    page = artifacts['review.html'].decode()
    assert 'fetch(' not in page and 'src="http' not in page
    data['products'][0]['origin'] = '<script>alert(1)</script>'
    escaped = render_review(data)
    assert '<script>alert(1)</script>' not in escaped
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in escaped
