from trade_system.data_store import DuckDBStore
from collectors.collect_misc import collect_auction_market
from trade_system.schema import init_schema


class FakeAuctionClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.stats = {'success': 0, 'error': 0}

    def get(self, endpoint, params=None, **kwargs):
        self.calls.append((endpoint, params, kwargs))
        self.stats['success'] += 1
        return self.payload


def test_full_market_payload_writes_ticks_and_final_match(tmp_path, monkeypatch):
    from scripts import collect_auction_market_daily as entry
    monkeypatch.setattr(entry, 'require_api_key', lambda _: None)
    writes = []
    insert = DuckDBStore.insert_rows
    def record_write(self, table, *args, **kwargs):
        writes.append(table)
        return insert(self, table, *args, **kwargs)
    monkeypatch.setattr(DuckDBStore, 'insert_rows', record_write)
    db = tmp_path / "auction-market.duckdb"
    store = DuckDBStore(str(db))
    init_schema(store.conn)
    store.close()
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
    monkeypatch.setattr(entry, 'KPLClient', lambda **_: client)
    try:
        result = entry.collect(db, '2026-08-28')
        store = DuckDBStore(str(db))
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
        assert len(client.calls) == 1
        assert store.conn.execute('SELECT count(*) FROM auction_bidding_anomaly').fetchone()[0] == 0
        client = FakeAuctionClient({'date': '2026-08-28', 'anomalies': [
            {'stock_code': '000002', 'type': 'cancel_buy', 'value': 12}]})
        store.close()
        assert entry.collect(db, '2026-08-28', product='anomaly')['status'] == 'success'
        store = DuckDBStore(str(db))
        assert len(client.calls) == 1 and client.calls[0][0] == '/auction/bidding-anomaly'
        assert store.conn.execute('SELECT anomaly_type FROM auction_bidding_anomaly').fetchall() == [('cancel_buy',)]
        assert store.conn.execute('SELECT count(*) FROM auction_tick').fetchone()[0] == 2
        client = FakeAuctionClient({'date': '2026-08-28', 'auction_ticks': [
            {'time': '09:20:00', 'price': 8, 'volume': 5, 'volume_unit': 'hands'}]})
        store.close()
        assert entry.collect(db, '2026-08-28', product='tick', codes=['000002', '000002'])['status'] == 'success'
        store = DuckDBStore(str(db))
        assert len(client.calls) == 1 and client.calls[0][0] == '/auction/tick'
        assert store.conn.execute('SELECT count(*) FROM auction_tick').fetchone()[0] == 3
        assert store.conn.execute('SELECT count(*) FROM auction_quote_snapshot').fetchone()[0] == 1
        assert writes == ['auction_tick', 'auction_quote_snapshot', 'auction_bidding_anomaly', 'auction_tick']
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
        class MissingRoute(FakeAuctionClient):
            def get(self,*args,**kwargs):
                self.stats.update(error=1,route_error=1)
                return None
        failed=collect_auction_market(MissingRoute(None),store,'2026-08-28')
        assert failed['status']=='route_unavailable' and failed['source_errors']['route_error']==1
        assert store.conn.execute(
            "SELECT count(*) FROM auction_tick"
        ).fetchone()[0] == 0
        from datetime import datetime
        from trade_system.auction_evidence import observed_auction_rows
        from trade_system.hithink_client import CST
        import pytest
        now=datetime.now(); day=now.date().isoformat()
        snapshot=dict(timestamp=int(now.replace(tzinfo=CST).timestamp()*1000),
            auction_phase='closed',data_status='final',total=1,
            item=[dict(thscode='000001.SZ',auction_price=10,auction_volume=2,auction_amount=2000)])
        evidence=observed_auction_rows(snapshot,day,'hithink',now,'raw-hash')
        assert evidence[0]['confirmation']=='final_snapshot_observed'
        assert evidence[0]['tick_rows']==0 and evidence[0]['auction_strength'] is None
        snapshot['data_status']='live'
        with pytest.raises(ValueError,match='unfinished'):
            observed_auction_rows(snapshot,day,'hithink',now,'raw-hash')
        bar=dict(api='stk_auction_o',params={'trade_date':'20260828'},rows=[
            dict(ts_code='920978.BJ',trade_date='20260828',open=11.94,high=18,low=0,
                 close=17.11,vol=3400,amount=58174)])
        evidence=observed_auction_rows(bar,'2026-08-28','xiaodefa',now,'raw-hash')
        assert evidence[0]['confirmation']=='historical_opening_bar_observed'
        assert 'invalid_native_ohlc' in evidence[0]['missing_reason'] and evidence[0]['tick_rows']==0
        bar['rows'][0]['trade_date']='20260827'
        with pytest.raises(ValueError,match='wrong-date'):
            observed_auction_rows(bar,'2026-08-28','xiaodefa',now,'raw-hash')
    finally:
        store.close()
