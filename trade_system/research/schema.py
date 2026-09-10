"""DuckDB schema helpers for research and review data."""

from __future__ import annotations

from pathlib import Path



def ensure_research_tables(db_path: str | Path) -> None:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS news_radar_item (
                news_id VARCHAR PRIMARY KEY,
                trade_date VARCHAR,
                source VARCHAR,
                title VARCHAR,
                url VARCHAR,
                published_at VARCHAR,
                industry VARCHAR,
                related_sector VARCHAR,
                related_symbol VARCHAR,
                keywords VARCHAR,
                risk_tags VARCHAR,
                catalyst_tags VARCHAR,
                summary VARCHAR,
                raw_payload_hash VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS research_note (
                note_id VARCHAR PRIMARY KEY,
                trade_date VARCHAR,
                symbol VARCHAR,
                sector VARCHAR,
                theme VARCHAR,
                note_type VARCHAR,
                content VARCHAR,
                evidence_refs VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS research_report_file (
                report_id VARCHAR PRIMARY KEY,
                symbol VARCHAR,
                title VARCHAR,
                publisher VARCHAR,
                report_date VARCHAR,
                file_path VARCHAR,
                source_url VARCHAR,
                summary VARCHAR,
                risk_tags VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp
            )
            """
        )
        con.execute(
            """
            CREATE OR REPLACE VIEW v_operator_research_context AS
            SELECT
                'news' AS context_type,
                trade_date AS context_date,
                related_symbol AS symbol,
                related_sector AS sector,
                title,
                summary,
                news_id AS ref_id
            FROM news_radar_item
            UNION ALL
            SELECT
                'note' AS context_type,
                trade_date AS context_date,
                symbol,
                sector,
                theme AS title,
                content AS summary,
                note_id AS ref_id
            FROM research_note
            UNION ALL
            SELECT
                'report' AS context_type,
                report_date AS context_date,
                symbol,
                CAST(NULL AS VARCHAR) AS sector,
                title,
                summary,
                report_id AS ref_id
            FROM research_report_file
            """
        )
    finally:
        con.close()
