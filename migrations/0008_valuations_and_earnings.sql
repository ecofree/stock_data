CREATE TABLE IF NOT EXISTS stock_valuations (
    thscode VARCHAR PRIMARY KEY,
    stock_code VARCHAR,
    pe_ttm DOUBLE,
    pe_mrq DOUBLE,
    pb_mrq DOUBLE,
    ps_ttm DOUBLE,
    pcf_ttm DOUBLE,
    fetched_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS earnings_calendar (
    trade_date DATE,
    stock_code VARCHAR,
    report_period VARCHAR,
    revenue_yoy DOUBLE,
    net_profit_yoy DOUBLE,
    eps DOUBLE,
    roe DOUBLE,
    fetched_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, stock_code)
);
