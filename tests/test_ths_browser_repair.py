from scripts import repair_ths_via_browser as repair


def test_browser_fetch_discovers_live_pager_after_stale_one_page_checkpoint(monkeypatch):
    calls = []

    def fake_chunk(target, concept_id, start, end, delay_ms=120):
        calls.append((start, end))
        return {
            "fetched_pages": end,
            "advertised_pages": 3 if start == 1 else 0,
            "rows": [{"code": f"00000{start}", "name": "x"}],
            "errors": [],
        }

    monkeypatch.setattr(repair, "_browser_fetch_chunk", fake_chunk)
    result = repair._browser_fetch("target", "300900", 1, chunk_pages=2)

    assert calls == [(1, 1), (2, 3)]
    assert result["expected_pages"] == 3
    assert result["fetched_pages"] == 3
