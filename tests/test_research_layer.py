import duckdb

from trade_system.adapters.vibe_research import discover_vibe_news_sources, load_vibe_news_config, radar_skeleton_from_config
from trade_system.research.news_radar import normalize_radar_items, upsert_news_items
from trade_system.research.research_notes import upsert_research_note
from trade_system.research.report_registry import upsert_research_report
from trade_system.research.schema import ensure_research_tables


def test_vibe_news_config_loads_industries_and_sources(tmp_path):
    config_path = tmp_path / "news_sources.json"
    config_path.write_text(
        """
        {
          "fetch": {"per_source": 2, "recent_days": 3},
          "industries": [{"key": "ai", "name": "AI", "accent": "#fff"}],
          "sources": [{"name": "OpenAI", "hint": "ai", "type": "rss", "url": "https://example.com/rss.xml"}]
        }
        """,
        encoding="utf-8",
    )

    config = load_vibe_news_config(config_path)
    skeleton = radar_skeleton_from_config(config)

    assert skeleton["recent_days"] == 3
    assert skeleton["industries"][0]["key"] == "ai"
    assert skeleton["industries"][0]["total"] == 1


def test_discover_vibe_news_sources_prefers_explicit_and_environment(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}", encoding="utf-8")
    env_root = tmp_path / "Vibe-Research"
    (env_root / "backend").mkdir(parents=True)
    env_config = env_root / "backend" / "news_sources.json"
    env_config.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("VIBE_RESEARCH_ROOT", str(env_root))

    assert discover_vibe_news_sources(explicit_path=explicit) == explicit
    assert discover_vibe_news_sources() == env_config


def test_normalize_radar_items_creates_stable_news_rows():
    radar = {
        "generated_at": "2026-07-07 09:00",
        "industries": [
            {
                "key": "ai",
                "name": "AI",
                "items": [
                    {
                        "source": "OpenAI",
                        "title": "模型发布",
                        "url": "https://example.com/news",
                        "time": "07-07 08:30",
                        "summary": "AI catalyst",
                    }
                ],
            }
        ],
    }

    rows = normalize_radar_items(radar, trade_date="2026-07-07")

    assert len(rows) == 1
    assert rows[0]["news_id"]
    assert rows[0]["industry"] == "AI"
    assert rows[0]["related_sector"] == "ai"
    assert rows[0]["catalyst_tags"] == "AI"


def test_research_tables_upsert_news_notes_reports_and_context_view(tmp_path):
    db_path = tmp_path / "research.duckdb"
    ensure_research_tables(db_path)
    news_rows = normalize_radar_items(
        {
            "generated_at": "2026-07-07 09:00",
            "industries": [
                {
                    "key": "robot",
                    "name": "机器人",
                    "items": [
                        {
                            "source": "Test RSS",
                            "title": "机器人产业链事件",
                            "url": "https://example.com/robot",
                            "time": "07-07 08:30",
                            "summary": "板块催化",
                        }
                    ],
                }
            ],
        },
        trade_date="2026-07-07",
    )

    assert upsert_news_items(db_path, news_rows) == 1
    assert upsert_research_note(
        db_path,
        trade_date="2026-07-07",
        symbol="300001",
        sector="robot",
        theme="机器人",
        note_type="catalyst",
        content="竞价前关注机器人主线持续性",
        evidence_refs="news:robot",
    )
    assert upsert_research_report(
        db_path,
        symbol="300001",
        title="机器人行业跟踪",
        publisher="internal",
        report_date="2026-07-07",
        file_path="D:/reports/robot.pdf",
        source_url="",
        summary="产业催化",
        risk_tags="样本不足",
    )

    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM news_radar_item").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM research_note").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM research_report_file").fetchone()[0] == 1
        context_rows = con.execute(
            "SELECT context_type, symbol, sector FROM v_operator_research_context ORDER BY context_type"
        ).fetchall()
        assert ("news", None, "robot") in context_rows
        assert ("note", "300001", "robot") in context_rows
        assert ("report", "300001", None) in context_rows
    finally:
        con.close()
