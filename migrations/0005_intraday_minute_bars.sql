CREATE TABLE IF NOT EXISTS intraday_minute_bars (
    trade_date DATE,
    stock_code VARCHAR,
    bar_time VARCHAR,
    price DOUBLE,
    volume BIGINT,
    fetched_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, stock_code, bar_time)
);
