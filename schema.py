"""Database schema definitions for KPL data storage."""
import duckdb
from datetime import datetime


def _ensure_business_indexes(db: duckdb.DuckDBPyConnection) -> None:
    """Enforce the canonical business keys after legacy dedupe migrations.

    Older project databases may still contain duplicates.  Initialization must
    remain readable in that state, so a duplicate table receives a temporary
    non-unique performance index and the repair command can promote it later.
    New and repaired databases get unique indexes and reject duplicate writes.
    """
    indexes = (
        ("uq_multi_source_stock_flow", "multi_source_stock_flow", "source_date, stock_code, provider"),
        ("uq_multi_source_sector_flow", "multi_source_sector_flow", "source_date, sector_code, provider"),
        ("uq_multi_source_kline", "multi_source_kline", "source_date, asset_type, asset_code, provider"),
        ("uq_tushare_daily", "tushare_daily", "date, ts_code"),
        ("uq_tushare_daily_basic", "tushare_daily_basic", "date, ts_code"),
        ("uq_tushare_moneyflow", "tushare_moneyflow", "date, ts_code"),
    )
    for name, table, columns in indexes:
        try:
            db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({columns})")
        except Exception:
            # Preserve startup/readability for a legacy duplicate database;
            # the repair script will drop this fallback and promote it.
            try:
                db.execute(f"CREATE INDEX IF NOT EXISTS ix_{name[3:]} ON {table}({columns})")
            except Exception:
                pass


def init_schema(db: duckdb.DuckDBPyConnection):
    """Initialize all tables for KPL data storage."""
    
    # ========== 市场情绪 (3) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS market_mood (
            date DATE,
            rise_count INTEGER,
            fall_count INTEGER,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            total_float BIGINT,
            prev_float BIGINT,
            rise_fall_ratio DOUBLE,
            market_color VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("ALTER TABLE market_mood ADD COLUMN IF NOT EXISTS source_kind VARCHAR")
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS market_rise_fall (
            date DATE,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            broken_limit_up_count INTEGER,
            blown_limit_up_count INTEGER,
            blown_limit_up_rate FLOAT,
            raw_field_5 INTEGER,
            raw_json TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("ALTER TABLE market_rise_fall ADD COLUMN IF NOT EXISTS source_kind VARCHAR")
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS market_limit_up_down (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct DOUBLE,
            turnover BIGINT,
            market_cap BIGINT,
            is_limit_up BOOLEAN,
            is_limit_down BOOLEAN,
            is_blown BOOLEAN,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 市场数据 (2) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS market_emotion_money (
            date DATE,
            cgl DOUBLE,
            yll DOUBLE,
            success_rate DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS market_emotion_detail (
            date DATE,
            metric_name VARCHAR,
            metric_value DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 大盘指数 (4) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS index_list (
            date DATE,
            index_code VARCHAR,
            index_name VARCHAR,
            price DOUBLE,
            change_pct DOUBLE,
            change_amt DOUBLE,
            turnover BIGINT,
            volume BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS index_intraday (
            date DATE,
            index_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            avg_price DOUBLE,
            volume BIGINT,
            turnover BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS index_kline (
            date DATE,
            index_code VARCHAR,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            turnover BIGINT,
            change_pct DOUBLE,
            ktype VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS index_full_info (
            date DATE,
            section VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 连板梯队 (7) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS ladder_market (
            date DATE,
            board_level INTEGER,
            stock_code VARCHAR,
            stock_name VARCHAR,
            consecutive_days INTEGER,
            consecutive_count INTEGER,
            is_first_board BOOLEAN,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS ladder_consecutive (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            board_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS ladder_sector (
            date DATE,
            sector_code VARCHAR,
            sector_name VARCHAR,
            board_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS ladder_broken (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            broken_time VARCHAR,
            max_price DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS ladder_sharp_withdrawal (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            withdrawal_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS ladder_board_stocks (
            date DATE,
            board_type VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS ladder_realtime_boards (
            date DATE,
            board_type VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            limit_up_time VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 板块数据 (19) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_plates (
            sector_code VARCHAR PRIMARY KEY,
            sector_name VARCHAR,
            sector_type VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_ranking (
            date DATE,
            sector_code VARCHAR,
            sector_name VARCHAR,
            stock_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_strength (
            date DATE,
            sector_code VARCHAR,
            strength_value DOUBLE,
            zhangting INTEGER,
            fengban_rate DOUBLE,
            dieting INTEGER,
            up_count INTEGER,
            down_count INTEGER,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_stocks (
            date DATE,
            sector_code VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct DOUBLE,
            turnover BIGINT,
            market_cap BIGINT,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_capital (
            date DATE,
            sector_code VARCHAR,
            main_net_inflow BIGINT,
            super_net_inflow BIGINT,
            big_net_inflow BIGINT,
            mid_net_inflow BIGINT,
            small_net_inflow BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_boom_reason (
            date DATE,
            sector_code VARCHAR,
            reason VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_strength_history (
            date DATE,
            sector_code VARCHAR,
            strength_value DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_son_plates (
            parent_code VARCHAR,
            son_code VARCHAR,
            son_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_sub_concepts (
            sector_code VARCHAR,
            concept_code VARCHAR,
            concept_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_parent_plate (
            sector_code VARCHAR,
            parent_code VARCHAR,
            parent_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_plate_info_qj (
            date DATE,
            sector_code VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_bk_fenshi_zhibo (
            date DATE,
            sector_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_strength_batch (
            date DATE,
            sector_code VARCHAR,
            strength_value DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_strength_ndays (
            date DATE,
            sector_code VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_strength_dataframe (
            date DATE,
            sector_code VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_all_stocks (
            date DATE,
            sector_code VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS sector_son_plate_direct (
            parent_code VARCHAR,
            son_code VARCHAR,
            son_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 龙虎榜 (7) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS lhb_list (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct VARCHAR,
            turnover BIGINT,
            reason VARCHAR,
            buy_amount BIGINT,
            sell_amount BIGINT,
            net_amount BIGINT,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS lhb_detail (
            date DATE,
            stock_code VARCHAR,
            broker_name VARCHAR,
            buy_amount BIGINT,
            sell_amount BIGINT,
            net_amount BIGINT,
            is_buy BOOLEAN,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS lhb_dataframe (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS lhb_update_list (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS lhb_raw_list (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS lhb_youzi_dongxiang (
            date DATE,
            broker_name VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            buy_amount BIGINT,
            sell_amount BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS lhb_top_title (
            date DATE,
            title VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 每日数据 (4) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            date DATE PRIMARY KEY,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            rise_count INTEGER,
            fall_count INTEGER,
            consecutive_count INTEGER,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("ALTER TABLE daily_summary ADD COLUMN IF NOT EXISTS source_kind VARCHAR")
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_new_high (
            date DATE,
            count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_sentiment (
            date DATE,
            sentiment_score DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS daily_export (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 个股深度 (8) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_institutional_positions (
            date DATE,
            stock_code VARCHAR,
            institution_name VARCHAR,
            shares BIGINT,
            change_shares BIGINT,
            season VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_holding_funds (
            date DATE,
            stock_code VARCHAR,
            fund_name VARCHAR,
            shares BIGINT,
            season VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_institutional_dates (
            stock_code VARCHAR,
            report_date VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_message_bar (
            date DATE,
            stock_code VARCHAR,
            msg_type VARCHAR,
            title VARCHAR,
            content VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_articles (
            date DATE,
            stock_code VARCHAR,
            article_title VARCHAR,
            article_url VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_tags (
            stock_code VARCHAR,
            tag_name VARCHAR,
            tag_type VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_company_info (
            stock_code VARCHAR PRIMARY KEY,
            company_name VARCHAR,
            industry VARCHAR,
            market_cap BIGINT,
            description VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # /market/limit-up-down currently returns a dated market aggregate rather
    # than a stock-level list.  Keep that payload in a separate relation so a
    # summary row can never be mistaken for an individual stock.
    db.execute("""
        CREATE TABLE IF NOT EXISTS market_limit_up_down_summary (
            date DATE PRIMARY KEY,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            actual_limit_up_count INTEGER,
            actual_limit_down_count INTEGER,
            blown_limit_up_rate DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS kpl_concept_daily (
            trade_date DATE,
            concept_code VARCHAR,
            concept_name VARCHAR,
            rank INTEGER,
            stock_count INTEGER,
            source VARCHAR DEFAULT 'kpl',
            raw_json VARCHAR,
            date_verified BOOLEAN DEFAULT FALSE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS kpl_concept_stock_history (
            trade_date DATE,
            concept_code VARCHAR,
            concept_name VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            concept_rank INTEGER,
            source VARCHAR DEFAULT 'kpl',
            raw_json VARCHAR,
            date_verified BOOLEAN DEFAULT FALSE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Existing databases created before the historical KPL collector need the
    # verification flag added without destructive migrations.
    # DuckDB versions used by the local runtime can rebuild an existing table
    # when ``ADD COLUMN IF NOT EXISTS`` is issued repeatedly.  That would
    # silently replace populated verification flags with the FALSE default.
    # Inspect the schema first and only alter genuinely old tables.
    for _table in ("kpl_concept_daily", "kpl_concept_stock_history"):
        _columns = {row[1] for row in db.execute(f"PRAGMA table_info('{_table}')").fetchall()}
        if "date_verified" not in _columns:
            db.execute(f"ALTER TABLE {_table} ADD COLUMN date_verified BOOLEAN DEFAULT FALSE")

    # 同花顺概念快照。THS 热榜接口能稳定返回“概念标签 -> 热榜个股”，
    # 但当前接口没有历史日期参数，因此必须单独落表并保留 date_verified，
    # 防止把当天快照伪装成 2026 年历史成分。
    db.execute("""
        CREATE TABLE IF NOT EXISTS ths_concept_daily (
            trade_date DATE,
            concept_code VARCHAR,
            concept_name VARCHAR,
            rank INTEGER,
            stock_count INTEGER,
            source VARCHAR DEFAULT 'ths_hot_list',
            raw_json VARCHAR,
            date_verified BOOLEAN DEFAULT FALSE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS ths_concept_stock_history (
            trade_date DATE,
            concept_code VARCHAR,
            concept_name VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            concept_rank INTEGER,
            source VARCHAR DEFAULT 'ths_hot_list',
            raw_json VARCHAR,
            date_verified BOOLEAN DEFAULT FALSE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    for _table in ("ths_concept_daily", "ths_concept_stock_history"):
        _columns = {row[1] for row in db.execute(f"PRAGMA table_info('{_table}')").fetchall()}
        if "date_verified" not in _columns:
            db.execute(f"ALTER TABLE {_table} ADD COLUMN date_verified BOOLEAN DEFAULT FALSE")
    db.execute("""
        CREATE TABLE IF NOT EXISTS ths_concept_member_checkpoint (
            trade_date DATE,
            concept_code VARCHAR,
            concept_name VARCHAR,
            status VARCHAR,
            pages_expected INTEGER DEFAULT 0,
            pages_fetched INTEGER DEFAULT 0,
            member_rows INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            provider VARCHAR,
            crawler_version VARCHAR,
            catalog_hash VARCHAR,
            PRIMARY KEY (trade_date, concept_code)
        )
    """)
    for _column, _type in (
        ("provider", "VARCHAR"),
        ("crawler_version", "VARCHAR"),
        ("catalog_hash", "VARCHAR"),
    ):
        db.execute(
            f"ALTER TABLE ths_concept_member_checkpoint ADD COLUMN IF NOT EXISTS {_column} {_type}"
        )
    # Source-aware default relations: THS wins for a date; KPL is retained as
    # a per-date compatibility fallback only when THS has no snapshot.
    db.execute("""
        CREATE OR REPLACE VIEW v_default_concept_daily AS
        SELECT trade_date, concept_code, concept_name, rank, stock_count, source, raw_json, date_verified
        FROM ths_concept_daily
        UNION ALL
        SELECT k.trade_date, k.concept_code, k.concept_name, k.rank, k.stock_count, k.source, k.raw_json, k.date_verified
        FROM kpl_concept_daily k
        WHERE NOT EXISTS (
            SELECT 1 FROM ths_concept_daily t WHERE t.trade_date = k.trade_date
        )
    """)
    db.execute("""
        CREATE OR REPLACE VIEW v_default_concept_stock_history AS
        SELECT trade_date, concept_code, concept_name, stock_code, stock_name, concept_rank, source, raw_json, date_verified
        FROM ths_concept_stock_history
        UNION ALL
        SELECT k.trade_date, k.concept_code, k.concept_name, k.stock_code, k.stock_name, k.concept_rank, k.source, k.raw_json, k.date_verified
        FROM kpl_concept_stock_history k
        WHERE NOT EXISTS (
            SELECT 1 FROM ths_concept_stock_history t WHERE t.trade_date = k.trade_date
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_trade_cal (
            exchange VARCHAR,
            cal_date DATE,
            is_open BOOLEAN,
            pretrade_date DATE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_stock_basic (
            ts_code VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            area VARCHAR,
            industry VARCHAR,
            market VARCHAR,
            list_date DATE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_daily (
            ts_code VARCHAR,
            stock_code VARCHAR,
            date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            turnover DOUBLE,
            change_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_daily_basic (
            ts_code VARCHAR,
            stock_code VARCHAR,
            date DATE,
            turnover_rate DOUBLE,
            volume_ratio DOUBLE,
            pe DOUBLE,
            pb DOUBLE,
            total_mv DOUBLE,
            circ_mv DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_adj_factor (
            ts_code VARCHAR,
            stock_code VARCHAR,
            date DATE,
            adj_factor DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_index_daily (
            ts_code VARCHAR,
            index_code VARCHAR,
            date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            turnover DOUBLE,
            change_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_gap_status (
            data_kind VARCHAR,
            code VARCHAR,
            start_date DATE,
            end_date DATE,
            expected_rows INTEGER,
            existing_rows INTEGER,
            missing_rows INTEGER,
            status VARCHAR,
            source_table VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_backfill_task (
            task_id VARCHAR,
            data_kind VARCHAR,
            code VARCHAR,
            start_date DATE,
            end_date DATE,
            status VARCHAR,
            attempts INTEGER DEFAULT 0,
            rows_inserted INTEGER DEFAULT 0,
            last_error VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # ========== 多源迁移层 (6) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_moneyflow (
            ts_code VARCHAR,
            stock_code VARCHAR,
            date DATE,
            buy_sm_amount DOUBLE,
            sell_sm_amount DOUBLE,
            buy_md_amount DOUBLE,
            sell_md_amount DOUBLE,
            buy_lg_amount DOUBLE,
            sell_lg_amount DOUBLE,
            buy_elg_amount DOUBLE,
            sell_elg_amount DOUBLE,
            net_mf_amount DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS tushare_moneyflow_industry (
            trade_date DATE,
            ts_code VARCHAR,
            sector_name VARCHAR,
            change_pct DOUBLE,
            close DOUBLE,
            net_amount DOUBLE,
            buy_elg_amount DOUBLE,
            sell_elg_amount DOUBLE,
            buy_lg_amount DOUBLE,
            sell_lg_amount DOUBLE,
            buy_md_amount DOUBLE,
            sell_md_amount DOUBLE,
            buy_sm_amount DOUBLE,
            sell_sm_amount DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS history_fetch_checkpoint (
            dataset VARCHAR,
            trade_date DATE,
            page_no INTEGER DEFAULT 0,
            status VARCHAR,
            rows_written INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(dataset, trade_date, page_no)
        )
    """)

    # These tables are intentionally source-aware.  Existing kline/sector
    # tables remain the curated trading views; the migration layer preserves
    # provider, freshness and raw payload so a failed provider never erases
    # usable evidence from another provider.
    db.execute("""
        CREATE TABLE IF NOT EXISTS multi_source_observation (
            observed_at TIMESTAMP DEFAULT current_timestamp,
            source_date DATE,
            data_type VARCHAR,
            asset_type VARCHAR,
            asset_code VARCHAR,
            provider VARCHAR,
            status VARCHAR,
            latency_ms INTEGER,
            is_stale BOOLEAN DEFAULT FALSE,
            payload_json VARCHAR,
            payload_hash VARCHAR
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS multi_source_kline (
            source_date DATE,
            asset_type VARCHAR,
            asset_code VARCHAR,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            amount DOUBLE,
            change_pct DOUBLE,
            provider VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp,
            is_stale BOOLEAN DEFAULT FALSE,
            raw_json VARCHAR
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS multi_source_stock_flow (
            source_date DATE,
            stock_code VARCHAR,
            main_net DOUBLE,
            net_total DOUBLE,
            super_net DOUBLE,
            large_net DOUBLE,
            mid_net DOUBLE,
            small_net DOUBLE,
            close DOUBLE,
            change_pct DOUBLE,
            turnover DOUBLE,
            provider VARCHAR,
            amount_unit VARCHAR,
            flow_definition VARCHAR,
            source_api VARCHAR,
            origin_provider VARCHAR,
            field_mapping_version VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp,
            is_stale BOOLEAN DEFAULT FALSE,
            raw_json VARCHAR
        )
    """)
    db.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN IF NOT EXISTS net_total DOUBLE")
    db.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN IF NOT EXISTS amount_unit VARCHAR")
    db.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN IF NOT EXISTS flow_definition VARCHAR")
    db.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN IF NOT EXISTS source_api VARCHAR")
    db.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN IF NOT EXISTS origin_provider VARCHAR")
    db.execute("ALTER TABLE multi_source_stock_flow ADD COLUMN IF NOT EXISTS field_mapping_version VARCHAR")
    db.execute("""
        CREATE TABLE IF NOT EXISTS multi_source_sector_flow (
            source_date DATE,
            sector_code VARCHAR,
            sector_name VARCHAR,
            main_net DOUBLE,
            super_net DOUBLE,
            large_net DOUBLE,
            mid_net DOUBLE,
            small_net DOUBLE,
            change_pct DOUBLE,
            main_ratio DOUBLE,
            provider VARCHAR,
            sector_type VARCHAR,
            amount_unit VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp,
            is_stale BOOLEAN DEFAULT FALSE,
            raw_json VARCHAR
        )
    """)
    db.execute("ALTER TABLE multi_source_sector_flow ADD COLUMN IF NOT EXISTS sector_type VARCHAR")
    db.execute("ALTER TABLE multi_source_sector_flow ADD COLUMN IF NOT EXISTS amount_unit VARCHAR")
    db.execute("""
        CREATE TABLE IF NOT EXISTS multi_source_quote (
            source_date DATE,
            asset_type VARCHAR,
            asset_code VARCHAR,
            name VARCHAR,
            price DOUBLE,
            change_pct DOUBLE,
            pe_ttm DOUBLE,
            pb DOUBLE,
            total_mv DOUBLE,
            circ_mv DOUBLE,
            provider VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp,
            is_stale BOOLEAN DEFAULT FALSE,
            raw_json VARCHAR
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS multi_source_sync_status (
            run_id VARCHAR,
            run_started_at TIMESTAMP,
            run_finished_at TIMESTAMP,
            trade_date DATE,
            data_type VARCHAR,
            asset_scope VARCHAR,
            requested_count INTEGER,
            success_count INTEGER,
            stale_count INTEGER,
            failed_count INTEGER,
            providers_json VARCHAR,
            error_json VARCHAR
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS multi_source_task_checkpoint (
            run_id VARCHAR,
            trade_date DATE,
            stage VARCHAR,
            task_name VARCHAR,
            data_type VARCHAR,
            asset_code VARCHAR,
            status VARCHAR,
            attempts INTEGER DEFAULT 0,
            rows_written INTEGER DEFAULT 0,
            provider VARCHAR,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            last_error VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(trade_date, stage, task_name, data_type, asset_code)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS stock_gudong (
            date DATE,
            stock_code VARCHAR,
            shareholder_name VARCHAR,
            shares BIGINT,
            ratio DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== L2分时 (7) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_stock_intraday (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            avg_price DOUBLE,
            volume BIGINT,
            turnover BIGINT,
            main_fund_net BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_stock_bigorder (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            big_net_amount BIGINT,
            big_buy BIGINT,
            big_sell BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_sector_intraday (
            date DATE,
            sector_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            turnover BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_sector_volume (
            date DATE,
            sector_code VARCHAR,
            time VARCHAR,
            volume BIGINT,
            turnover BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_tick_orders (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            order_type VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_tick_orders_all (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            order_type VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_tick_history (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            direction VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== L2实时 (4) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_realtime_index_trend (
            date DATE,
            index_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_realtime_sharp_withdrawal (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            withdrawal_pct DOUBLE,
            time VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_realtime_all_boards (
            date DATE,
            board_level INTEGER,
            stock_code VARCHAR,
            stock_name VARCHAR,
            limit_up_time VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS l2_realtime_index_list (
            date DATE,
            index_code VARCHAR,
            index_name VARCHAR,
            price DOUBLE,
            change_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 主题 (1) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS theme_hot (
            date DATE,
            theme_name VARCHAR,
            theme_code VARCHAR,
            change_pct DOUBLE,
            leader_stock VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 复盘 (3) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS fengk_list (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct DOUBLE,
            turnover BIGINT,
            market_cap BIGINT,
            main_net BIGINT,
            reason VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS fengk_yd_plate (
            date DATE,
            sector_name VARCHAR,
            main_net BIGINT,
            change_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS fengk_yd_plate_info (
            date DATE,
            sector_name VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 盘面数据 (5) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_module_versatile (
            date DATE,
            module_name VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_northbound_close_date (
            date DATE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_southbound_close_date (
            date DATE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_art_title (
            date DATE,
            stock_code VARCHAR,
            article_title VARCHAR,
            article_url VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_radar (
            date DATE,
            time VARCHAR,
            event_type VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            event_desc VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 盯盘 (3) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_weipan (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            amount BIGINT,
            change_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_jijin (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            fund_name VARCHAR,
            shares BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS dingpan_all (
            date DATE,
            category VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 竞价 (2) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS auction_tick (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS auction_bidding_anomaly (
            date DATE,
            stock_code VARCHAR,
            anomaly_type VARCHAR,
            anomaly_value DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 现货 (1) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS xianhuo_list (
            date DATE,
            product_name VARCHAR,
            price DOUBLE,
            change_pct DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== ETF (2) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS etf_ranking (
            date DATE,
            etf_code VARCHAR,
            etf_name VARCHAR,
            change_pct DOUBLE,
            turnover BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS etf_all (
            date DATE,
            etf_code VARCHAR,
            etf_name VARCHAR,
            change_pct DOUBLE,
            turnover BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 财务 (5) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS finance_summary (
            stock_code VARCHAR,
            report_date VARCHAR,
            revenue BIGINT,
            net_profit BIGINT,
            eps DOUBLE,
            roe DOUBLE,
            gross_margin DOUBLE,
            net_margin DOUBLE,
            debt_ratio DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS finance_income (
            stock_code VARCHAR,
            report_date VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS finance_balance (
            stock_code VARCHAR,
            report_date VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS finance_cashflow (
            stock_code VARCHAR,
            report_date VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS finance_compare (
            stock_code VARCHAR,
            report_date VARCHAR,
            period_type VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # Resumable finance gap-fill checkpoint.  Financial statements are
    # quarterly; keeping the attempt status prevents daily runs from
    # repeatedly fanning out to every listed company and preserves gaps for
    # controlled retries.
    db.execute("""
        CREATE TABLE IF NOT EXISTS finance_fetch_checkpoint (
            stock_code VARCHAR,
            target_date DATE,
            status VARCHAR,
            source_income VARCHAR,
            source_balance VARCHAR,
            source_cashflow VARCHAR,
            income_rows INTEGER,
            balance_rows INTEGER,
            cashflow_rows INTEGER,
            last_error VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 资讯 (15) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS news_plate (
            date DATE,
            sector_code VARCHAR,
            news_title VARCHAR,
            news_url VARCHAR,
            news_source VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS news_columns (
            column_id VARCHAR,
            column_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS news_concept_jxbk (
            sector_code VARCHAR,
            sector_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS topic_list (
            date DATE,
            topic_id VARCHAR,
            topic_title VARCHAR,
            heat_score INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS news_theme (
            date DATE,
            news_title VARCHAR,
            news_url VARCHAR,
            theme_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            date DATE,
            stock_id VARCHAR,
            comment_content VARCHAR,
            comment_author VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS tuyere_by_stock (
            date DATE,
            stock_code VARCHAR,
            report_title VARCHAR,
            report_type VARCHAR,
            report_url VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS tuyere_tags (
            tag_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS forums_column (
            column_id VARCHAR,
            column_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS forums_sel_list (
            date DATE,
            post_id VARCHAR,
            post_title VARCHAR,
            post_author VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS forums_focus (
            date DATE,
            msg_id VARCHAR,
            msg_content VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS topic_detail (
            topic_id VARCHAR PRIMARY KEY,
            topic_title VARCHAR,
            topic_content VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS topic_vote (
            topic_id VARCHAR,
            vote_type VARCHAR,
            vote_count INTEGER,
            vote_ratio DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS news_index_plate (
            date DATE,
            sector_code VARCHAR,
            sector_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== K线 (1) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS kline (
            date DATE,
            stock_code VARCHAR,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            turnover BIGINT,
            change_pct DOUBLE,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 首页 (1) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS index_info (
            date DATE,
            section VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 高级数据 (75) ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_pankou (
            date DATE,
            stock_code VARCHAR,
            buy1_price DOUBLE,
            buy1_volume BIGINT,
            buy2_price DOUBLE,
            buy2_volume BIGINT,
            buy3_price DOUBLE,
            buy3_volume BIGINT,
            buy4_price DOUBLE,
            buy4_volume BIGINT,
            buy5_price DOUBLE,
            buy5_volume BIGINT,
            sell1_price DOUBLE,
            sell1_volume BIGINT,
            sell2_price DOUBLE,
            sell2_volume BIGINT,
            sell3_price DOUBLE,
            sell3_volume BIGINT,
            sell4_price DOUBLE,
            sell4_volume BIGINT,
            sell5_price DOUBLE,
            sell5_volume BIGINT,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_dadan_kline (
            date DATE,
            stock_code VARCHAR,
            big_net_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_dadan_kline_today (
            date DATE,
            stock_code VARCHAR,
            big_net_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_trend_min (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            avg_price DOUBLE,
            volume BIGINT,
            direction VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_pianlizhi (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_pmsl (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            tag VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_gudong_info (
            date DATE,
            stock_code VARCHAR,
            shareholder_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_gudong_renshu (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            shareholder_count INTEGER,
            change_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_corporate_news (
            date DATE,
            stock_code VARCHAR,
            news_title VARCHAR,
            news_type VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_company_count (
            stock_code VARCHAR,
            news_count INTEGER,
            report_count INTEGER,
            announcement_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_relation (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_interviews (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            institution_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_his_ranking (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct DOUBLE,
            ranking INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_newhigh_group_count (
            date DATE,
            group_type VARCHAR,
            count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_newhigh_group_stocks (
            date DATE,
            group_type VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_news_flash (
            date DATE,
            time VARCHAR,
            news_title VARCHAR,
            news_source VARCHAR,
            news_url VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_news_flash_top (
            date DATE,
            news_title VARCHAR,
            news_source VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_market_radar (
            date DATE,
            time VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            event_type VARCHAR,
            event_desc VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_weipan_qiangchou (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            amount BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_fengk_best (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            score DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_on_the_lhb (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            probability DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_agency_list (
            date DATE,
            agency_name VARCHAR,
            buy_count INTEGER,
            buy_amount BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_business_list (
            date DATE,
            business_name VARCHAR,
            buy_count INTEGER,
            buy_amount BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_market_mood_count (
            date DATE,
            rise_count INTEGER,
            fall_count INTEGER,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_concept_point (
            date DATE,
            concept_name VARCHAR,
            change_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_morning_bidding_summary (
            date DATE,
            total_amount BIGINT,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_morning_bidding_list (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            bidding_amount BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_bkjj_bl (
            date DATE,
            sector_code VARCHAR,
            sector_name VARCHAR,
            bidding_ratio DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_convertible_bonds_option (
            bond_code VARCHAR,
            bond_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_fenshi_kline_option (
            option_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_dadan_trend_incremental (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            big_net_amount BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_rqz_data (
            date DATE,
            stock_code VARCHAR,
            rq_balance BIGINT,
            rq_sell BIGINT,
            rq_repay BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_kline_today (
            date DATE,
            stock_code VARCHAR,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            turnover BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_holiday (
            year INTEGER,
            holiday_date DATE,
            holiday_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_pianlizhi_many (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_pianlizhi_w32 (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_chouma (
            date DATE,
            stock_code VARCHAR,
            price_level DOUBLE,
            shares BIGINT,
            chouma_type VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_zjmm_min (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            main_net_inflow BIGINT,
            super_net_inflow BIGINT,
            big_net_inflow BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_fenbi2 (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            volume BIGINT,
            direction VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_duidao_kline (
            date DATE,
            stock_code VARCHAR,
            duidao_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_tuoyadan_kline (
            date DATE,
            stock_code VARCHAR,
            tuo_amount BIGINT,
            ya_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_gujia_kline (
            date DATE,
            stock_code VARCHAR,
            gujia_value DOUBLE,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_bidvol_kline (
            date DATE,
            stock_code VARCHAR,
            bidding_volume BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_main_activity_kline (
            date DATE,
            stock_code VARCHAR,
            main_activity_score DOUBLE,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_kline_volume_forecast (
            date DATE,
            stock_code VARCHAR,
            forecast_volume BIGINT,
            is_jj BOOLEAN,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_kline_today_tyd (
            date DATE,
            stock_code VARCHAR,
            tuo_amount BIGINT,
            ya_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_zhangting_gene (
            stock_code VARCHAR,
            gene_score DOUBLE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_stock_plate_new (
            stock_code VARCHAR,
            sector_code VARCHAR,
            sector_name VARCHAR,
            is_t BOOLEAN,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_stock_trend_incremental_ph (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            incremental_value DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_kline_zhangting_reason (
            date DATE,
            stock_code VARCHAR,
            reason VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_his_ranking_info (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct DOUBLE,
            ranking INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_his_zhangfu_detail (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_disk_review (
            date DATE,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_market_scln (
            date DATE,
            total_volume BIGINT,
            total_turnover BIGINT,
            mtype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_his_sharp_withdrawal (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            withdrawal_pct DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_weight_performance (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            change_pct DOUBLE,
            weight DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_zhangting_expression (
            date DATE,
            expression_type VARCHAR,
            count INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_zs_real (
            date DATE,
            index_code VARCHAR,
            close_price DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_zs_trend_narrow (
            date DATE,
            index_code VARCHAR,
            time VARCHAR,
            price DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_dadan_kline_new (
            date DATE,
            stock_code VARCHAR,
            big_net_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_kline_today_dadan_new (
            date DATE,
            stock_code VARCHAR,
            big_net_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_kline_today_main_activity (
            date DATE,
            stock_code VARCHAR,
            main_activity_score DOUBLE,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_kline_today_duidao (
            date DATE,
            stock_code VARCHAR,
            duidao_amount BIGINT,
            ktype VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_big_reminder (
            date DATE,
            stock_code VARCHAR,
            reminder_type VARCHAR,
            reminder_content VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_f10_concept_jxbk (
            stock_code VARCHAR,
            concept_code VARCHAR,
            concept_name VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_f10_index (
            stock_code VARCHAR,
            raw_json VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_bid_history (
            date DATE,
            stock_code VARCHAR,
            bid_data VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_voltur_history (
            date DATE,
            stock_code VARCHAR,
            volume_ratio DOUBLE,
            turnover_rate DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_dp_explain (
            stock_code VARCHAR,
            explain_content VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_dp_realdata (
            date DATE,
            stock_code VARCHAR,
            event_type VARCHAR,
            event_time VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_main_monitor (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            main_net_inflow BIGINT,
            ranking INTEGER,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_bk_dj_arrange (
            date DATE,
            sector_code VARCHAR,
            sector_name VARCHAR,
            bidding_amount BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_vol_tur (
            date DATE,
            stock_code VARCHAR,
            time VARCHAR,
            volume_ratio DOUBLE,
            turnover_rate DOUBLE,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    db.execute("""
        CREATE TABLE IF NOT EXISTS advanced_turnover_ten (
            date DATE,
            stock_code VARCHAR,
            turnover_level INTEGER,
            shares BIGINT,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    
    # ========== 采集日志 ==========
    db.execute("""
        CREATE TABLE IF NOT EXISTS collect_log (
            date DATE,
            module VARCHAR,
            endpoint VARCHAR,
            status VARCHAR,
            rows_inserted INTEGER,
            error_msg VARCHAR,
            fetched_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # Explicit schema/data-quality contract.  These tables make migrations and
    # provider failures queryable instead of relying on ad-hoc repair scripts.
    db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migration (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT current_timestamp,
            description VARCHAR
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS data_quality_event (
            event_id VARCHAR,
            trade_date DATE,
            data_type VARCHAR,
            provider VARCHAR,
            severity VARCHAR,
            code VARCHAR,
            message VARCHAR,
            observed_at TIMESTAMP DEFAULT current_timestamp,
            details_json VARCHAR,
            PRIMARY KEY(event_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS source_contract_observation (
            trade_date DATE,
            data_type VARCHAR,
            provider VARCHAR,
            source_event_time TIMESTAMP,
            collected_at TIMESTAMP DEFAULT current_timestamp,
            row_count INTEGER DEFAULT 0,
            coverage_pct DOUBLE,
            quality_status VARCHAR,
            units_json VARCHAR,
            payload_hash VARCHAR,
            PRIMARY KEY(trade_date, data_type, provider, collected_at)
        )
    """)
    for _table in ("multi_source_stock_flow", "multi_source_sector_flow", "multi_source_kline", "multi_source_quote"):
        if table_exists := bool(db.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [_table]
        ).fetchone()[0]):
            for _column, _type in (("source_event_time", "TIMESTAMP"), ("collected_at", "TIMESTAMP")):
                db.execute(f"ALTER TABLE {_table} ADD COLUMN IF NOT EXISTS {_column} {_type}")
    db.execute(
        "INSERT INTO schema_migration(version,description) VALUES (1,'P0-P2 data freshness, provenance and operational contract') "
        "ON CONFLICT(version) DO NOTHING"
    )

    _ensure_business_indexes(db)
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Schema initialized successfully")
