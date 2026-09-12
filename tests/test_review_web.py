import duckdb
from pathlib import Path

from trade_system.daily_review import (
    _concept_limit_up_review,
    _derived_limit_board_levels,
    build_review_narrative,
)
from trade_system.review_web import _render_tables
from trade_system.review_web import (
    _render_command_summary,
    _render_concept_limit_up,
    _build_review_lazy_asset,
    _concept_inline_data,
    _render_loop,
    _render_qlib_research,
    _render_sector_trail,
    _render_tomorrow,
)
from trade_system.review_extras import render_first_seal_distribution, render_theme_timeline
from trade_system.web_report import _execution_status


def test_member_detail_uses_clock_formatter_not_raw_epoch():
    from trade_system.review_web import _chart_js
    script = _chart_js({}, {}, [], [])
    assert "const time = lu && lu.time ? fmtTime(lu.time) : '—';" in script
    assert "timeZone: 'Asia/Shanghai'" in script


def test_server_clock_is_explicit_shanghai_time():
    from trade_system.review_web import _fmt_clock
    from datetime import datetime, timezone
    stamp = int(datetime(2026, 9, 10, 1, 35, tzinfo=timezone.utc).timestamp())
    assert _fmt_clock(stamp) == '09:35'
    assert _fmt_clock(stamp * 1000) == '09:35'


def test_dashboard_execution_status_is_fail_closed_for_historical_rows(tmp_path):
    db = tmp_path / "dashboard.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE stock_candidate_stage_signal(
            trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR,
            is_actionable BOOLEAN, is_executable BOOLEAN,
            evidence_json VARCHAR, decision VARCHAR,
            execution_valid_until TIMESTAMP
        )
        """
    )
    con.execute(
        """
        INSERT INTO stock_candidate_stage_signal VALUES
        ('2026-07-31','intraday_strength','000001',true,true,'{}','follow',
         '2026-07-31 14:00:00')
        """
    )
    con.close()

    con = duckdb.connect(str(db), read_only=True)
    try:
        status = _execution_status(con, "2026-07-31")
    finally:
        con.close()

    assert status["candidate_total"] == 0
    assert status["executable_candidates"] == 0
    assert status["execution_ready"] is False


def test_blocked_page_labels_plans_as_research_drafts():
    html = _render_loop({
        "execution_control": {"execution_ready": False, "override": "BLOCK"},
        "plans": [{
            "stock_code": "000017",
            "stock_name": "深中华A",
            "setup_type": "manual_short_term",
            "max_position_pct": 10,
            "status": "planned",
            "entry_condition": "observe",
            "stop_condition": "invalidate",
        }],
    })

    assert "研究计划草案" in html
    assert "研究草案（门禁未开放）" in html
    assert "已列入计划" not in html


def test_derived_limit_board_levels_use_trading_day_continuity(tmp_path):
    db = tmp_path / "board_levels.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE v_limit_pool(trade_date DATE, stock_code VARCHAR)"
    )
    con.execute(
        "CREATE TABLE tushare_trade_cal(cal_date DATE, is_open INTEGER)"
    )
    con.executemany(
        "INSERT INTO tushare_trade_cal VALUES (?, 1)",
        [("2026-07-31",), ("2026-08-03",), ("2026-08-04",), ("2026-08-05",), ("2026-08-06",), ("2026-08-07",)],
    )
    con.executemany(
        "INSERT INTO v_limit_pool VALUES (?, ?)",
        [
            ("2026-07-31", "000001"),
            ("2026-08-03", "000001"),
            ("2026-08-04", "000001"),
            ("2026-08-05", "000001"),
            ("2026-08-07", "000001"),
        ],
    )
    levels = _derived_limit_board_levels(con, "2026-08-07", ["000001"])
    con.close()

    assert levels[("000001", "2026-07-31")] == 1
    assert levels[("000001", "2026-08-05")] == 4
    assert levels[("000001", "2026-08-07")] == 1


def test_review_flow_tables_use_context_field_names():
    html = _render_tables(
        {
            "capital_flow": {
                "stock_inflow": [
                    {"stock_code": "000001", "stock_name": "平安银行", "main_net": 1.2e8}
                ],
                "sector_inflow": [
                    {"sector_name": "银行", "main_net": 3.4e8, "change_pct": 1.2}
                ],
            }
        }
    )
    assert "000001" in html
    assert "平安银行" in html
    assert "银行" in html
    assert "flow-block" in html
    assert "<td class='num'>1</td>" in html
    assert "<th class='mono'>代码</th>" in html


def test_concept_limit_up_review_recomputes_same_date_stocks_from_membership_and_pool(tmp_path):
    db = tmp_path / "concept_review.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE v_default_concept_stock_history(
            trade_date DATE, concept_code VARCHAR, concept_name VARCHAR,
            stock_code VARCHAR, stock_name VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE v_limit_pool(
            trade_date DATE, board_level INTEGER, stock_code VARCHAR,
            stock_name VARCHAR, limit_up_time VARCHAR, fetched_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE v_theme_mainline_evidence(
            trade_date DATE, sector_code VARCHAR, sector_name VARCHAR,
            strength_value DOUBLE, main_net_inflow DOUBLE, mainline_score DOUBLE
        )
        """
    )
    con.execute(
        """
        INSERT INTO v_default_concept_stock_history VALUES
        ('2026-08-12','THS-1','concept-a','000001','stock-a'),
        ('2026-08-12','THS-1','concept-a','000002','stock-b'),
        ('2026-08-12','THS-2','concept-b','000003','stock-c')
        """
    )
    con.execute(
        """
        INSERT INTO v_limit_pool VALUES
        ('2026-08-13',3,'000001','stock-a','09:35',now()),
        ('2026-08-13',1,'000003','stock-c','10:01',now())
        """
    )
    con.execute(
        """
        INSERT INTO v_theme_mainline_evidence VALUES
        ('2026-08-13','THS-1','concept-a',2.0,100000000,80),
        ('2026-08-13','THS-2','concept-b',1.0,50000000,70)
        """
    )
    con.close()

    con = duckdb.connect(str(db), read_only=True)
    try:
        result = _concept_limit_up_review(con, "2026-08-13")
    finally:
        con.close()

    assert result["status"] == "ready"
    assert result["membership_date"] == "2026-08-12"
    assert result["groups"][0]["concept_name"] == "concept-a"
    assert result["groups"][0]["limit_up_count"] == 1
    assert result["groups"][0]["member_count"] == 2
    assert result["groups"][0]["limit_up_stocks"][0]["stock_code"] == "000001"


def test_theme_timeline_counts_limit_pool_not_all_concept_members(tmp_path):
    db = tmp_path / "theme_timeline.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE ths_concept_stock_history("
        "trade_date DATE, concept_code VARCHAR, concept_name VARCHAR, "
        "stock_code VARCHAR, date_verified BOOLEAN)"
    )
    con.execute(
        "CREATE TABLE ths_concept_daily(trade_date DATE, concept_code VARCHAR)"
    )
    con.execute("CREATE TABLE v_limit_pool(trade_date DATE, stock_code VARCHAR)")
    con.executemany(
        "INSERT INTO ths_concept_stock_history VALUES (?, ?, ?, ?, true)",
        [
            ("2026-08-28", "THS-1", "题材A", "000001",),
            ("2026-08-28", "THS-1", "题材A", "000002",),
        ],
    )
    con.execute("INSERT INTO ths_concept_daily VALUES ('2026-08-28', 'THS-1')")
    con.execute("INSERT INTO v_limit_pool VALUES ('2026-08-28', '000001')")
    html = render_theme_timeline(con, "2026-08-28", days=1, top_n=1)
    con.close()

    assert "题材A 08-28：1 只涨停" in html
    assert "概念覆盖 1/1" in html


def test_first_seal_distribution_parses_seconds_in_limit_up_time(tmp_path):
    db = tmp_path / "first_seal.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE v_limit_pool(trade_date DATE, limit_up_time VARCHAR)")
    con.executemany(
        "INSERT INTO v_limit_pool VALUES ('2026-08-28', ?)",
        [("09:25:30",), ("09:44:18",), ("14:28:30",)],
    )
    html = render_first_seal_distribution(con, "2026-08-28")
    con.close()

    assert "集合竞价秒板 1只" in html
    assert "早盘抢板 1只" in html
    assert "尾盘偷袭 1只" in html


def test_concept_limit_up_review_derives_streak_from_limit_dates(tmp_path):
    db = tmp_path / "concept_board_levels.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE v_default_concept_stock_history(
            trade_date DATE, concept_code VARCHAR, concept_name VARCHAR,
            stock_code VARCHAR, stock_name VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE v_limit_pool(
            trade_date DATE, board_level INTEGER, stock_code VARCHAR,
            stock_name VARCHAR, limit_up_time VARCHAR, fetched_at TIMESTAMP
        )
        """
    )
    con.execute(
        "CREATE TABLE tushare_trade_cal(cal_date DATE, is_open INTEGER)"
    )
    con.executemany(
        "INSERT INTO tushare_trade_cal VALUES (?, 1)",
        [("2026-08-11",), ("2026-08-12",), ("2026-08-13",)],
    )
    con.execute(
        "INSERT INTO v_default_concept_stock_history VALUES "
        "('2026-08-12','THS-1','concept-a','000001','stock-a')"
    )
    con.executemany(
        "INSERT INTO v_limit_pool VALUES (?, ?, '000001', 'stock-a', '09:35', now())",
        [("2026-08-11", 99), ("2026-08-12", 88), ("2026-08-13", 77)],
    )
    con.close()

    con = duckdb.connect(str(db), read_only=True)
    try:
        result = _concept_limit_up_review(con, "2026-08-13")
    finally:
        con.close()

    assert result["status"] == "ready"
    assert result["groups"][0]["max_board"] == 3
    assert result["groups"][0]["limit_up_stocks"][0]["board_level"] == 3


def test_limit_up_stock_can_appear_under_every_concept_it_belongs_to(tmp_path):
    db = tmp_path / "multi_concept.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE v_default_concept_stock_history(
            trade_date DATE, concept_code VARCHAR, concept_name VARCHAR,
            stock_code VARCHAR, stock_name VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE v_limit_pool(
            trade_date DATE, board_level INTEGER, stock_code VARCHAR,
            stock_name VARCHAR, limit_up_time VARCHAR, fetched_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        INSERT INTO v_default_concept_stock_history VALUES
        ('2026-08-12','THS-1','算力租赁','000001','stock-a'),
        ('2026-08-12','THS-2','数据中心','000001','stock-a'),
        ('2026-08-12','THS-2','数据中心','000002','stock-b')
        """
    )
    con.execute(
        """
        INSERT INTO v_limit_pool VALUES
        ('2026-08-13',2,'000001','stock-a','09:35',now())
        """
    )
    con.close()

    con = duckdb.connect(str(db), read_only=True)
    try:
        result = _concept_limit_up_review(con, "2026-08-13")
    finally:
        con.close()

    names = {group["concept_name"] for group in result["groups"]}
    assert names == {"算力租赁", "数据中心"}
    for group in result["groups"]:
        assert [row["stock_code"] for row in group["limit_up_stocks"]] == ["000001"]


def test_concept_drilldown_and_qlib_section_are_research_only():
    ctx = {
        "concept_limit_up": {
            "membership_date": "2026-08-12",
            "groups": [{
                "concept_name": "concept-a", "mainline_score": 80,
                "limit_up_count": 1, "max_board": 3, "member_count": 2,
                "limit_up_stocks": [{
                    "stock_code": "000001", "stock_name": "stock-a",
                    "board_level": 3, "limit_up_time": "09:35"
                }]
            }]
        },
        "data_sources": {"qlib": [{
            "model_id": "shadow-v4", "stage": "shadow", "sample_count": 100,
            "hit_rate": 0.55, "avg_forward_return_pct": 1.2,
            "ic": 0.03, "rank_ic": 0.05, "signal_impact": "disabled"
        }]}
    }
    concept_html = _render_concept_limit_up(ctx)
    qlib_html = _render_qlib_research(ctx)
    assert "主线概念" not in concept_html  # renderer returns the component, page owns the heading
    assert "concept-a" in concept_html and "stock-a" in concept_html
    assert "research_only / shadow" in qlib_html
    assert "shadow-v4" in qlib_html


def test_day_aware_limit_up_annotation_key_is_used():
    source = Path(__file__).resolve().parents[1] / "trade_system" / "review_web.py"
    text = source.read_text(encoding="utf-8")
    assert "luByDateCode" in text
    assert "`${{day}}|${{stock.code}}`" in text
    assert "const luByCode = {{}}" not in text


def test_review_narrative_is_blocked_when_close_source_missing():
    ctx = {
        "trade_date": "2026-08-13",
        "regime": {"regime": "震荡", "suggested_position_pct": 25},
        "readiness": {"certified_ready": False, "missing_groups": ["kline", "close_source"]},
        "execution_control": {
            "override": "BLOCK",
            "execution_ready": False,
            "effective_position_pct": 0,
        },
        "concept_limit_up": {"groups": [{"concept_name": "共封装光学(CPO)", "limit_up_count": 8, "member_count": 40}]},
        "capital_flow": {
            "stock_inflow": [{"stock_code": "300394", "stock_name": "天孚通信"}],
            "stock_outflow": [{"stock_code": "000636", "stock_name": "风华高科"}],
        },
        "outcomes": [],
        "plans": [],
        "alerts": [],
        "watchlist": [],
        "journal": [],
        "market_context": {
            "breadth": [
                {"limit_up_count": 71, "limit_down_count": 3, "rise_count": 2265, "fall_count": 3092}
            ]
        },
        "risk": {"risk_state": "cautious"},
    }
    story = build_review_narrative(ctx)
    assert story["stance"] == "blocked"
    assert story["stance_label"] == "仅可复盘"
    assert "共封装光学(CPO)" in story["headline"]
    assert any("kline" in item for item in story["tomorrow"])
    assert any("没有导入成交" in item for item in story["bullets"])

    html = _render_command_summary(ctx)
    assert "仅可复盘" in html
    assert "共封装光学(CPO)" in html
    assert "没有导入成交" in _render_loop(ctx)
    tomorrow = _render_tomorrow(ctx)
    assert "先补齐 kline, close_source" in tomorrow


def test_review_narrative_distinguishes_analysis_ready_from_execution_ready():
    ctx = {
        "trade_date": "2026-08-28",
        "regime": {"regime": "高潮", "suggested_position_pct": 30},
        "readiness": {"analysis_ready": True, "missing_groups": []},
        "execution_control": {
            "override": "ALLOW_REVIEW_ONLY",
            "execution_ready": False,
            "effective_position_pct": 0,
        },
        "concept_limit_up": {"groups": [{"concept_name": "人工智能", "limit_up_count": 19}]},
        "capital_flow": {}, "outcomes": [], "plans": [], "alerts": [],
        "watchlist": [], "journal": [], "market_context": {}, "risk": {},
    }
    story = build_review_narrative(ctx)
    assert story["stance"] == "observe"
    assert story["stance_label"] == "观察核验"
    assert "复盘事实已形成" in story["lede"]
    assert "数据门禁未开放" not in story["headline"]


def test_named_ladder_and_yday_surface():
    from trade_system.review_web import _render_broken, _render_named_ladder, _render_yday_limitup

    ctx = {
        "ecology": {
            "ladder_groups": [{
                "height": 5, "count": 1, "note": "龙头",
                "names": ["秦安股份"],
                "stocks": [{"stock_name": "秦安股份", "board_level": 5}],
            }],
            "yday": {
                "n": 149, "avg_ret": 0.08, "pos_rate": 36.2,
                "still_limit_up": 23, "limit_down": 1,
                "first_avg": -0.5, "multi_avg": 1.89, "prev_date": "2026-08-12",
            },
            "broken": [{"stock_name": "大众交通", "board_level": 2, "change_pct": -6.86}],
        }
    }
    assert "秦安股份" in _render_named_ladder(ctx)
    assert "149" in _render_yday_limitup(ctx)
    assert "大众交通" in _render_broken(ctx)


def test_sector_trail_cards_render_across_dates():
    html = _render_sector_trail(
        {
            "sector_trail": {
                "dates": ["2026-08-13", "2026-08-12"],
                "membership_date": "2026-08-12",
                "sectors": [{
                    "id": "THS-1",
                    "name": "中国AI 50",
                    "daily": {
                        "2026-08-13": {"limit_up": 3, "pct_chg": 0.52, "main_net": 1},
                        "2026-08-12": {"limit_up": 5, "pct_chg": 1.1, "main_net": 2},
                    },
                }],
            }
        }
    )
    assert "中国AI 50" in html
    assert "2026-08-13" in html and "2026-08-12" in html
    assert "trail-data" in html
    assert "data-trail-mode='week'" in html
    assert "trail-periods" in html


def test_review_sidecar_bounds_initial_payload_and_removes_inline_details():
    stocks = [
        {"stock_code": f"{index:06d}", "stock_name": f"stock-{index}", "board_level": 1}
        for index in range(60)
    ]
    ctx = {
        "concept_limit_up": {
            "groups": [
                {"concept_name": "concept-a", "limit_up_count": 60, "limit_up_stocks": stocks},
                {"concept_name": "concept-b", "limit_up_count": 1, "limit_up_stocks": stocks[:1]},
            ]
        },
        "sector_trail": {
            "dates": ["2026-08-13"],
            "sectors": [{
                "id": "THS-1",
                "name": "concept-a",
                "daily": {"2026-08-13": {"limit_up": 1, "stocks": stocks[:1]}},
                "window_stocks": stocks[:1],
            }],
        },
        "sector_periods": {},
    }
    inline = _concept_inline_data(ctx)
    assert len(inline[0]["limit_up_stocks"]) == 50
    assert inline[1]["limit_up_stocks"] == []

    lazy = _build_review_lazy_asset(ctx)
    assert "window.__REVIEW_LAZY_DATA__" in lazy
    assert "stock-59" in lazy

    external_html = _render_sector_trail(ctx, external_lazy=True)
    assert "trail-data" in external_html
    assert "trail-detail-THS-1" not in external_html


def test_daily_sector_overview_keeps_only_latest_rows_without_full_payload():
    html = _render_sector_trail(
        {
            "sector_trail": {
                "dates": ["2026-08-13", "2026-08-12"],
                "membership_date": "2026-08-12",
                "concept_source": "THS",
                "sectors": [
                    {
                        "id": "THS-1",
                        "name": "概念A",
                        "daily": {
                            "2026-08-13": {"limit_up": 3, "strength": 2.0, "pct_chg": 1.2},
                            "2026-08-12": {"limit_up": 1, "strength": 1.0, "pct_chg": 0.5},
                        },
                    }
                ],
            }
        },
        compact_overview=True,
    )

    assert "概念A" in html
    assert "2026-08-13" in html
    assert "<div class='day-title'>2026-08-12" not in html
    assert "trail-data" not in html
    assert "trail-details" not in html


def test_theme_table_marks_composite_score_as_experimental():
    from trade_system.review_web import _render_themes

    html = _render_themes(
        {"theme_mainline": [{"code": "THS-1", "name": "概念A", "score": 80}]}
    )

    assert "experimental_unvalidated" in html
    assert "不作为正式口径" in html


def test_concept_selector_only_builds_first_fifty_buttons():
    from trade_system.review_web import _render_concept_limit_up

    groups = [
        {"concept_name": f"概念{index}", "limit_up_count": 1, "limit_up_stocks": []}
        for index in range(60)
    ]
    html = _render_concept_limit_up({"concept_limit_up": {"groups": groups}})

    assert html.count("class='concept-tab") == 50
    assert "加载全部概念（剩余 10 个）" in html
    assert "data-hidden-concept" not in html


def test_daily_picks_renderer_escapes_dates_and_does_not_raise(tmp_path):
    from trade_system.review_extras import render_daily_picks

    db = tmp_path / "daily_picks.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE daily_stock_picks("
        "rank INTEGER, stock_code VARCHAR, stock_name VARCHAR, board VARCHAR, "
        "total_score DOUBLE, limit_up_reason VARCHAR, llm_bull_case VARCHAR, "
        "llm_risk VARCHAR, llm_watch_condition VARCHAR, factor_json VARCHAR, trade_date DATE)"
    )
    con.execute(
        "INSERT INTO daily_stock_picks VALUES "
        "(1, '000001', '示例', '1', 80, '原因', '逻辑', '风险', '观察', '{}', DATE '2026-08-28')"
    )
    html = render_daily_picks(con, "2026-08-28")
    con.close()

    assert "2026-08-28" in html
    assert "示例" in html


def test_period_frames_group_newest_first():
    from trade_system.daily_review import _compound_pct, _period_frames, _period_label

    assert _period_label("2026-08-13", "week").startswith("2026-W")
    assert _period_label("2026-08-13", "month") == "2026-08"
    assert _period_label("2026-08-13", "quarter") == "2026-Q3"
    assert _compound_pct([10, -10]) == -1.0
    dates = ["2026-08-13", "2026-08-12", "2026-08-07", "2026-08-06"]
    weeks = _period_frames(dates, "week", 8)
    assert weeks[0]["end"] == "2026-08-13"
    assert weeks[0]["trading_days"] >= 1
def test_flow_source_column_keeps_provider_cell():
    from trade_system.review_web import _flow_table
    text = _flow_table('sources', [{'provider': 'eastmoney_intraday_clist_delay', 'definition': 'provider_main_net',
                                   'amount_unit': 'yuan', 'rows': 12}],
                       [('provider', 'source', ''), ('definition', 'metric', ''),
                        ('amount_unit', 'unit', ''), ('rows', 'rows', 'num')])
    assert text.count('<td ') == 4
    assert 'provider_main_net' in text and 'yuan' in text
    from trade_system.i18n_labels import PROVIDER_CN, cn
    assert cn(PROVIDER_CN, 'eastmoney_intraday_clist_delay') in text
