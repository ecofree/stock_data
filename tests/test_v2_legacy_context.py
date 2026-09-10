from pathlib import Path

import duckdb

from trade_system.v2.legacy_context import load_snapshot
from tools.v2.backup_verify import sha256
from tools.v2.run_context_research import run


def seed_legacy(path):
    with duckdb.connect(str(path)) as con:
        con.execute('''CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN,pretrade_date DATE,fetched_at TIMESTAMP);
            INSERT INTO tushare_trade_cal VALUES ('SSE','2026-09-04',true,'2026-09-03','2026-09-04 09:00:00'),
                ('SSE','2026-09-07',true,'2026-09-04','2026-09-07 09:00:00');
            CREATE TABLE tushare_stock_basic(ts_code VARCHAR,stock_code VARCHAR,list_date DATE);
            INSERT INTO tushare_stock_basic VALUES ('600001.SH','600001','2010-01-01');
            CREATE TABLE tushare_daily(ts_code VARCHAR,stock_code VARCHAR,date DATE,close DOUBLE,change_pct DOUBLE,
                turnover DOUBLE,fetched_at TIMESTAMP,amount_unit VARCHAR,adjustment VARCHAR,provider VARCHAR);
            INSERT INTO tushare_daily VALUES ('600001.SH','600001','2026-09-07',10,0,123,'2026-09-07 17:00:00','thousand_yuan','none','xiaodefa');
            CREATE TABLE ths_concept_stock_history(trade_date DATE,concept_code VARCHAR,concept_name VARCHAR,stock_code VARCHAR,
                fetched_at TIMESTAMP,date_verified BOOLEAN,source VARCHAR);
            INSERT INTO ths_concept_stock_history VALUES ('2026-09-07','T-1','正常中文','600001','2026-09-07 17:00:00',true,'hithink_index_api'),
                ('2026-09-07','T-1','正常中文','123456','2026-09-07 17:00:00',true,'hithink_index_api');
            CREATE TABLE official_limit_pool(trade_date DATE,stock_code VARCHAR,continue_day_cnt INTEGER,fetched_at TIMESTAMP,source VARCHAR);
            INSERT INTO official_limit_pool VALUES ('2026-09-07','600001',1,'2026-09-07 17:00:00','hithink')''')


def test_real_adapter_preserves_units_identity_and_unknown_origin_entitlements(tmp_path):
    source = tmp_path/'legacy.duckdb'
    seed_legacy(source)
    before = sha256(source)
    bundle = load_snapshot(source,'2026-09-07',source_sha256=before)
    assert bundle['bars'][0]['amount_cny'] == 123000
    assert bundle['bars'][0]['change_pct'] == 0
    assert bundle['bars'][0]['product'] == 'daily_xiaodefa'
    assert bundle['memberships'][0]['theme_name'] == '正常中文'
    assert bundle['adapter_diagnostics']['unmapped_members'] == 1
    assert bundle['mode'] == 'historical_research'
    assert bundle['universe_certified'] is False
    assert sha256(source) == before


def test_real_entrypoint_retains_empty_candidates_and_self_contained_page(tmp_path):
    source = tmp_path/'legacy.duckdb'
    seed_legacy(source)
    summary = run(source,tmp_path/'result','2026-09-07')
    assert summary['candidates'] == 0  # only one member, no threshold lowering
    assert summary['execution_ready'] is False
    page = Path(summary['review']).read_text(encoding='utf-8')
    assert '历史观测已接入' in page and '正常中文' in page
    assert '没有通过当前实验条件的候选' in page
    assert '不能据此推断空仓' in page
    assert 'fetch(' not in page
