import duckdb

import scripts.collect_intraday_sector_flow_full as collector


def _row(code: str) -> dict:
    return {
        "sector_code": code,
        "sector_name": f"board-{code}",
        "main_net": 100.0,
        "super_net": 40.0,
        "large_net": 60.0,
        "mid_net": 0.0,
        "small_net": 0.0,
        "sector_type": "em_industry",
        "amount_unit": "yuan",
    }


def test_taxonomy_success_threshold_is_uniformly_99_point_5_percent():
    assert collector._taxonomy_coverage_status(200, 199) == (99.5, "success")
    assert collector._taxonomy_coverage_status(201, 200) == (99.5, "success")
    assert collector._taxonomy_coverage_status(496, 486) == (97.98, "partial")
    assert collector._taxonomy_coverage_status(
        496, 496, unverified=True
    ) == (100.0, "unverified")


def test_reverse_code_reconciliation_closes_dynamic_page_gap(tmp_path, monkeypatch):
    db = tmp_path / "sector-pages.duckdb"
    calls = []

    def fake_page(*, page, page_size, return_meta, sort_field, sort_order):
        calls.append((page, sort_field, sort_order))
        if sort_order == "0":
            rows = {
                1: [_row("BK0001"), _row("BK0002")],
                # Simulate a board moving across a page boundary while the
                # first sweep is running.
                2: [_row("BK0002"), _row("BK0003")],
            }.get(page, [])
        else:
            rows = {
                1: [_row("BK0004"), _row("BK0003")],
                2: [_row("BK0002"), _row("BK0001")],
            }.get(page, [])
        return rows, {"total": 4, "returned_rows": len(rows)}

    monkeypatch.setattr(collector, "_from_em_sector_flow_page", fake_page)
    monkeypatch.setattr(collector.shared_host_limiter, "acquire", lambda *args, **kwargs: None)

    result = collector.collect_full_sector_flow(
        db,
        "2026-07-24",
        page_size=500,
        max_pages=2,
        pause_seconds=0,
    )

    assert result["status"] == "success_with_optional_gap"
    assert result["coverage_pct"] == 100.0
    assert result["reconciliation_pages"] == 1
    assert calls == [
        (1, "f12", "0"),
        (2, "f12", "0"),
        (1, "f12", "1"),
    ]
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT count(DISTINCT sector_code) FROM multi_source_sector_flow "
            "WHERE source_date='2026-07-24' AND provider='eastmoney_sector_full'"
        ).fetchone()[0] == 4
        assert con.execute(
            "SELECT status FROM intraday_sector_flow_batch "
            "WHERE trade_date='2026-07-24'"
        ).fetchone()[0] == "success_with_optional_gap"
        assert con.execute(
            "SELECT count(*) FROM sector_capital WHERE date='2026-07-24'"
        ).fetchone()[0] == 4
    finally:
        con.close()
