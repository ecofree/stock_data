import duckdb

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
from trade_system.web_report import _execution_status


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
    assert result["groups"][0]["limit_up_stocks"][0]["stock_code"] == "000001"


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
        "concept_limit_up": {"groups": [{"concept_name": "共封装光学(CPO)", "limit_up_count": 8}]},
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
