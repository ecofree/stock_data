import duckdb

from scripts.collect_intraday_capital_flow import infer_stock_codes, infer_sector_codes


def test_intraday_universe_uses_generated_candidate_scores_and_sector_capital(tmp_path):
    db = tmp_path / "candidate_fallback.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE stock_candidate_score(trade_date VARCHAR, stock_code VARCHAR, score DOUBLE)")
    con.execute("INSERT INTO stock_candidate_score VALUES ('2026-07-14','000001',90),('2026-07-14','600519',80)")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-14','BK0001'),('2026-07-14','BK0002')")
    con.close()

    assert infer_stock_codes(db, "2026-07-14", 2) == ["000001", "600519"]
    assert infer_sector_codes(db, "2026-07-14", 2) == ["BK0001", "BK0002"]
