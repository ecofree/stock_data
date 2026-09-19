import csv

import duckdb

from trade_system.backfill import import_professional_csvs
from trade_system.normalize import build_normalized_views


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_professional_csv_backfill_populates_real_sources_and_is_idempotent(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    kline_csv = tmp_path / "kline.csv"
    index_csv = tmp_path / "index_kline.csv"
    sector_capital_csv = tmp_path / "sector_capital.csv"
    auction_csv = tmp_path / "auction_tick.csv"

    _write_csv(
        kline_csv,
        [
            {
                "date": "2026-07-06",
                "stock_code": "000001",
                "open": "9.5",
                "high": "10.1",
                "low": "9.4",
                "close": "10.0",
                "volume": "100000",
                "turnover": "1000000",
                "change_pct": "5.5",
                "ktype": "D",
            }
        ],
    )
    _write_csv(
        index_csv,
        [
            {
                "date": "2026-07-06",
                "index_code": "000001.SH",
                "open": "3000",
                "high": "3050",
                "low": "2990",
                "close": "3040",
                "volume": "1000000",
                "turnover": "2000000",
                "change_pct": "1.2",
                "ktype": "D",
            }
        ],
    )
    _write_csv(
        sector_capital_csv,
        [
            {
                "date": "2026-07-06",
                "sector_code": "801001",
                "main_net_inflow": "500000000",
                "super_net_inflow": "120000000",
                "big_net_inflow": "90000000",
                "mid_net_inflow": "60000000",
                "small_net_inflow": "-20000000",
            }
        ],
    )
    _write_csv(
        auction_csv,
        [
            {
                "date": "2026-07-06",
                "stock_code": "000001",
                "time": "09:25",
                "price": "10.0",
                "volume": "50000",
            }
        ],
    )

    first = import_professional_csvs(
        str(db_path),
        {
            "kline": str(kline_csv),
            "index_kline": str(index_csv),
            "sector_capital": str(sector_capital_csv),
            "auction_tick": str(auction_csv),
        },
    )
    second = import_professional_csvs(str(db_path), {"kline": str(kline_csv)})

    assert first["kline"] == 1
    assert first["index_kline"] == 1
    assert first["sector_capital"] == 1
    assert first["auction_tick"] == 1
    assert second["kline"] == 1

    build_normalized_views(str(db_path))
    con = duckdb.connect(str(db_path))
    assert con.execute("SELECT count(*) FROM kline").fetchone()[0] == 1
    assert con.execute("SELECT source_table, is_fallback FROM v_kline_daily").fetchone() == ("kline", False)
    assert con.execute("SELECT source_table, is_fallback FROM v_index_state").fetchone() == ("index_kline", False)
    assert con.execute("SELECT source_table, is_fallback FROM v_sector_capital").fetchone() == ("sector_capital", False)
    assert con.execute("SELECT source_table, is_fallback FROM v_auction_status").fetchone() == ("auction_tick", False)
    con.close()
