import duckdb

from base import DuckDBStore
from collectors.collect_misc import collect_auction_market
from schema import init_schema


class FakeAuctionClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, endpoint, params=None, **kwargs):
        self.calls.append((endpoint, params, kwargs))
        return self.payload


def test_full_market_payload_writes_ticks_and_final_match(tmp_path):
    db = tmp_path / "auction-market.duckdb"
    store = DuckDBStore(str(db))
    init_schema(store.conn)
    client = FakeAuctionClient({
        "date": "2026-08-28",
        "total": 2,
        "data": {
            "000001": {
                "code": "000001",
                "source": "realtime",
                "auction_ticks": [
                    {"time": "09:15:00", "price": 10.1, "volume": 20},
                    {"time": "09:25:00", "price": 10.2, "volume": 30},
                ],
                "auction_count": 2,
                "matched_price": 10.2,
                "matched_volume": 30,
                "total_amount": 30600,
                "total_ticks": 999,
            },
            "bad": {"code": "not-a-stock", "auction_ticks": []},
        },
    })
    try:
        result = collect_auction_market(client, store, "2026-08-28")
        assert result["status"] == "success"
        assert result["stock_rows"] == 1
        assert result["tick_rows"] == 2
        assert result["quote_rows"] == 1
        assert client.calls[0][0] == "/auction/market"
        assert client.calls[0][1] == {"date": "2026-08-28"}
        assert store.conn.execute("SELECT count(*) FROM auction_tick").fetchone()[0] == 2
        assert store.conn.execute(
            "SELECT indicative_price,cumulative_volume,provider FROM auction_quote_snapshot"
        ).fetchone() == (10.2, 30, "kpl_auction_market")
    finally:
        store.close()


def test_market_date_mismatch_is_not_published(tmp_path):
    db = tmp_path / "auction-mismatch.duckdb"
    store = DuckDBStore(str(db))
    init_schema(store.conn)
    try:
        result = collect_auction_market(
            FakeAuctionClient({"date": "2026-08-27", "data": {}}),
            store,
            "2026-08-28",
        )
        assert result["status"] == "source_date_mismatch"
        assert store.conn.execute(
            "SELECT count(*) FROM auction_tick"
        ).fetchone()[0] == 0
    finally:
        store.close()
