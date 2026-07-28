import duckdb

from trade_system.research.api_events import import_api_research_events


def test_import_api_research_events_upserts_news_radar_items(tmp_path):
    db_path = tmp_path / "api_research.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE advanced_news_flash(date DATE, news_title VARCHAR, news_source VARCHAR, news_url VARCHAR)")
    con.execute("INSERT INTO advanced_news_flash VALUES ('2026-07-06','AI catalyst','wire','https://example.test/a')")
    con.execute("CREATE TABLE news_plate(date DATE, sector_code VARCHAR, news_title VARCHAR, news_url VARCHAR, news_source VARCHAR)")
    con.execute("INSERT INTO news_plate VALUES ('2026-07-06','801001','sector event','https://example.test/b','plate')")
    con.close()

    first = import_api_research_events(db_path, "2026-07-06")
    second = import_api_research_events(db_path, "2026-07-06")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT source, title, related_sector FROM news_radar_item ORDER BY title"
        ).fetchall()
    finally:
        con.close()

    assert first == 2
    assert second == 2
    assert rows == [
        ("wire", "AI catalyst", None),
        ("plate", "sector event", "801001"),
    ]
