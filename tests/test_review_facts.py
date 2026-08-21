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
    }
    assert facts["trend"]["dates"] == []
    assert facts["ladder"] == []
    assert facts["candidate_flow"] == []
