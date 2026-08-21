from __future__ import annotations

import hashlib
import duckdb

import trade_system.ths_history as ths_history
from trade_system.ths_history import THSConceptHistoryCollector, _concept_code


def test_ths_member_parser_prefers_paginated_constituent_table():
    html = """
    <table class="series-table">
      <tr><td><a href="http://stockpage.10jqka.com.cn/600001/">600001</a></td></tr>
      <tr><td><a href="http://stockpage.10jqka.com.cn/600002/">600002</a></td></tr>
    </table>
    <table class="m-table m-pager-table">
      <tr><th>代码</th><th>名称</th></tr>
      <tr><td><a href="http://stockpage.10jqka.com.cn/000001/">000001</a></td>
          <td><a href="http://stockpage.10jqka.com.cn/000001">平安银行</a></td></tr>
    </table>
    """
    assert ths_history._parse_ths_members(html) == [{"code": "000001", "name": "平安银行"}]


def test_ths_detail_members_does_not_count_blocked_page_as_fetched(monkeypatch):
    page = '<div class="m-pager"><span class="page_info">1/3</span></div>'
    table = (
        '<table class="m-table m-pager-table"><tr>'
        '<td><a href="http://stockpage.10jqka.com.cn/000001/">000001</a></td>'
        '<td><a href="http://stockpage.10jqka.com.cn/000001">平安银行</a></td>'
        '</tr></table>'
    )
    monkeypatch.setattr(
        ths_history,
        "_ths_html",
        lambda url, _referer: page + table if "/page/" not in url else "<script>location.href=\"//upass.10jqka.com.cn/login\"</script>",
    )
    members, expected, fetched = ths_history._ths_detail_members_with_meta("300001", 0)
    assert len(members) == 1
    assert (expected, fetched) == (3, 1)


def test_ths_blockrank_members_migrated_from_adata_endpoint(monkeypatch):
    calls = []

    def fake_blockrank(index_code, amount):
        calls.append((index_code, amount))
        if len(calls) == 1:
            return {"block": {"subcodeCount": 2}, "items": [{"5": "000001", "55": "平安银行"}]}
        return {"block": {"subcodeCount": 2}, "items": [
            {"5": "000001", "55": "平安银行"},
            {"5": "600000", "55": "浦发银行"},
        ]}

    monkeypatch.setattr(ths_history, "_ths_blockrank_json", fake_blockrank)
    rows, total = ths_history._ths_index_members("885887")
    assert total == 2
    assert rows == [
        {"code": "000001", "name": "平安银行"},
        {"code": "600000", "name": "浦发银行"},
    ]
    assert calls == [("885887", 15), ("885887", 15)]


def test_ths_detail_prefers_blockrank_when_index_code_is_embedded(monkeypatch):
    html = '<input type="hidden" id="clid" value=\'885887\'><span class="page_info">1/65</span>' \
           '<table class="m-pager-table"><tr><td><a href="http://stockpage.10jqka.com.cn/000001">平安银行</a></td></tr></table>'
    monkeypatch.setattr(ths_history, "_ths_html", lambda *_args: html)
    monkeypatch.setattr(ths_history, "_ths_index_members", lambda _index: (
        [{"code": "000001", "name": "平安银行"}, {"code": "600000", "name": "浦发银行"}], 2
    ))
    result = ths_history._ths_detail_members_with_meta("308642", 0)
    members, expected, fetched = result
    assert members[1]["code"] == "600000"
    assert (expected, fetched) == (65, 65)
    assert result.provider == "ths_index_blockrank"


def test_ths_full_snapshot_is_weekly_and_skips_member_crawl(monkeypatch, tmp_path):
    db = tmp_path / "ths_weekly.duckdb"
    catalog = [(f"C{i:03d}", f"概念{i:03d}") for i in range(374)]
    monkeypatch.setattr(ths_history, "_ths_catalog", lambda: catalog)
    collector = THSConceptHistoryCollector(db, mode="full", max_member_pages=0)
    try:
        collector.store.conn.executemany(
            "INSERT INTO ths_concept_member_checkpoint(trade_date,concept_code,concept_name,status,pages_expected,pages_fetched,member_rows) VALUES (?,?,?,?,?,?,?)",
            [("2026-07-15", f"THS-{code}", name, "success", 1, 1, 1) for code, name in catalog],
        )
        collector.store.conn.commit()
        result = collector.collect_snapshot("2026-07-16")
    finally:
        collector.close()
    assert result["status"] == "skipped"
    assert result["weekly_source_date"] == "2026-07-15"
    assert result["catalog_count"] == 374


def test_ths_snapshot_aggregates_concepts_and_members(tmp_path):
    db = tmp_path / "ths.duckdb"

    def fake_fetcher(period):
        assert period == "day"
        return [
            {"rank": 1, "code": "SZ000001", "name": "平安银行", "concepts": ["数字金融", "银行"], "heat": 99},
            {"rank": 2, "code": "600000", "name": "浦发银行", "concepts": ["银行"], "heat": 98},
        ]

    with THSConceptHistoryCollector(db, fetcher=fake_fetcher) as collector:
        result = collector.collect_snapshot("2026-07-14")

    assert result["status"] == "success"
    assert result["date_verified"] is False
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select count(*) from ths_concept_daily").fetchone()[0] == 2
        assert con.execute("select stock_count from ths_concept_daily where concept_name='银行'").fetchone()[0] == 2
        assert con.execute("select count(*) from ths_concept_stock_history").fetchone()[0] == 3
        assert con.execute("select date_verified from ths_concept_stock_history limit 1").fetchone()[0] is False
        assert con.execute("select concept_code from ths_concept_daily where concept_name='银行'").fetchone()[0] == _concept_code("银行")
    finally:
        con.close()


def test_ths_recovery_skips_cached_stale_boards_by_default(tmp_path, monkeypatch):
    db = tmp_path / "ths-stale-recovery.duckdb"
    catalog = [("300001", "板块一"), ("300002", "板块二")]
    monkeypatch.setattr(ths_history, "_ths_catalog", lambda: catalog)
    calls = []

    def fake_members(code, _max_pages):
        calls.append(code)
        return ([{"code": "000001", "name": "平安银行"}], 1, 1)

    monkeypatch.setattr(ths_history, "_ths_detail_members_with_meta", fake_members)
    with THSConceptHistoryCollector(db, mode="full", max_member_pages=0) as collector:
        catalog_hash = hashlib.sha256("300001|板块一\n300002|板块二".encode("utf-8")).hexdigest()
        collector.store.conn.execute(
            "INSERT INTO ths_concept_member_checkpoint "
            "(trade_date,concept_code,concept_name,status,pages_expected,pages_fetched,member_rows,provider,crawler_version,catalog_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ["2026-07-14", "THS-300001", "板块一", "success_stale", 1, 1, 1,
             "ths_cached_weekly", "ths_web_v2", catalog_hash],
        )
        collector.store.conn.execute(
            "INSERT INTO ths_concept_daily(trade_date,concept_code,concept_name,rank,stock_count,source,raw_json,date_verified) "
            "VALUES ('2026-07-14','THS-300001','板块一',1,1,'ths_cached_weekly','{}',false)"
        )
        collector.store.conn.execute(
            "INSERT INTO ths_concept_stock_history(trade_date,concept_code,concept_name,stock_code,stock_name,concept_rank,source,raw_json,date_verified) "
            "VALUES ('2026-07-14','THS-300001','板块一','000001','平安银行',1,'ths_cached_weekly','{}',false)"
        )
        collector.store.conn.commit()
        result = collector.collect_snapshot("2026-07-14", force=False)
    assert result["status"] == "partial"
    assert calls == ["300002"]


def test_ths_run_reports_unavailable_history(tmp_path):
    db = tmp_path / "ths-gap.duckdb"
    with THSConceptHistoryCollector(db, fetcher=lambda _period: []) as collector:
        result = collector.run("2026-01-01", "2026-07-14")
    assert result["historical_supported"] is False
    assert len(result["missing_historical_dates"]) > 100
    assert result["snapshot"]["status"] == "empty"


def test_ths_full_snapshot_checkpoints_each_board_and_rejects_empty(tmp_path, monkeypatch):
    db = tmp_path / "ths-full.duckdb"
    monkeypatch.setattr(ths_history, "_ths_catalog", lambda: [("300001", "板块一"), ("300002", "板块二")])
    monkeypatch.setattr(
        ths_history,
        "_ths_detail_members_with_meta",
        lambda code, _max_pages: (
            ([{"code": "000001", "name": "平安银行"}], 2, 2)
            if code == "300001" else ([], 1, 1)
        ),
    )
    with THSConceptHistoryCollector(db, mode="full", max_member_pages=0) as collector:
        result = collector.collect_snapshot("2026-07-14", force=True)
    assert result["status"] == "partial"
    assert result["catalog_count"] == 2
    assert result["checkpoint_success"] == 1
    assert result["missing_member_concepts"] == 1
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select count(*) from ths_concept_daily").fetchone()[0] == 2
        assert con.execute("select count(*) from ths_concept_stock_history").fetchone()[0] == 1
        assert con.execute("select status from ths_concept_member_checkpoint where concept_code='THS-300002'").fetchone()[0] == "empty"
    finally:
        con.close()
