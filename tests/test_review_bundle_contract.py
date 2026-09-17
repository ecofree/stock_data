import json

import pytest

from scripts.audit_daily_review_artifact import _embedded_json, _trail_payload


def test_compact_review_checks_its_same_date_static_trail(tmp_path):
    path = tmp_path/'daily_review_latest.html'
    main = '<a href="sector_trail_latest.html">板块轨迹</a>'
    trail = {'dates': ['2026-09-16'], 'sectors': [{'id': 'THS-1'}]}
    sibling = tmp_path/'sector_trail_latest.html'
    sibling.write_text('<title>板块轨迹 · 2026-09-16</title>'
        + '<script type="application/json" id="trail-data">'+json.dumps(trail)+'</script>'
        + '<script type="application/json" id="trail-details">{}</script>', encoding='utf-8')
    data, source = _trail_payload(path, main, {}, '2026-09-16')
    assert data['trail-data'] == trail and source == sibling
    assert _trail_payload(path, main, {}, '2026-09-17')[0] == {}
    assert _trail_payload(path, '<p>No trail link</p>', {}, '2026-09-16')[0] == {}


def test_full_inline_payload_does_not_fall_back_to_another_artifact(tmp_path):
    embedded = {'trail-data': {}, 'trail-details': {}}
    path = tmp_path/'daily_review_latest.html'
    data, source = _trail_payload(path, '<a href="sector_trail_latest.html">trail</a>', embedded, '2026-09-16')
    assert data is embedded and source == path


def test_duplicate_payload_is_rejected_before_database_open():
    text = '<script id="trail-data" type="application/json">{}</script>'
    with pytest.raises(ValueError, match='duplicate'):
        _embedded_json(text + text)
def test_legacy_latest_writes_are_refused_before_reading_database(monkeypatch):
    from pathlib import Path
    import pytest
    from trade_system import daily_review, review_web
    def forbidden(*args, **kwargs):
        raise AssertionError('retired publication must refuse before reading')
    monkeypatch.setattr(daily_review, 'build_daily_review_context', forbidden)
    monkeypatch.setattr(review_web, '_render_review_bundle', forbidden)
    root = Path(review_web.__file__).resolve().parents[1] / 'reports'
    with pytest.raises(ValueError, match='retired'):
        daily_review.write_daily_review('missing.duckdb', root / 'daily_review_latest.md')
    with pytest.raises(ValueError, match='retired'):
        review_web.write_review_web('missing.duckdb', root / 'daily_review_latest.html')
