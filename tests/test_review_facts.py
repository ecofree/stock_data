import duckdb

from trade_system.review_facts import build_review_page_facts


def test_review_page_facts_has_one_stable_payload_on_minimal_database(tmp_path):
    db = tmp_path / "review_facts.duckdb"
    con = duckdb.connect(str(db), read_only=False)
    try:
        facts = build_review_page_facts(con, "2026-08-13")
    finally:
        con.close()

    assert set(facts) == {
        "trend",
        "ladder",
        "rotation",
        "theme_mainline",
        "candidate_flow",
        "emotion_rows",
        "consecutive",
        "metric_contract",
    }
    assert facts["trend"]["dates"] == []
    assert facts["ladder"] == []
    assert facts["candidate_flow"] == []
    assert facts["metric_contract"]["composite_score"]["formal_ready"] is False


def test_same_date_limit_pool_is_authority_for_highest_board(tmp_path):
    db = tmp_path / "highest_board.duckdb"
    con = duckdb.connect(str(db), read_only=False)
    con.execute(
        "CREATE TABLE v_limit_pool(trade_date DATE, board_level INTEGER, stock_code VARCHAR)"
    )
    con.execute(
        "INSERT INTO v_limit_pool VALUES "
        "(DATE '2026-08-27', 1, '000001'), (DATE '2026-08-27', 6, '000017')"
    )
    try:
        facts = build_review_page_facts(con, "2026-08-27")
    finally:
        con.close()

    assert facts["consecutive"] == 6
    assert facts["ladder"] == [{"height": 1, "count": 1}, {"height": 6, "count": 1}]
