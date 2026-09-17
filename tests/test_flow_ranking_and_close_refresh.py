"""Flow source ranking and historical dashboard reads, without signal writes."""

from __future__ import annotations

from trade_system.flow_ranking import is_mega_sector_name, stock_provider_rank


def test_stock_provider_prefers_full_market_over_kpl():
    assert stock_provider_rank("eastmoney_intraday_clist_delay") > stock_provider_rank("kpl")
    assert stock_provider_rank("tushare") > stock_provider_rank("kpl")


def test_mega_sector_filter():
    assert is_mega_sector_name("融资融券")
    assert is_mega_sector_name("深股通")
    assert not is_mega_sector_name("商业航天")
