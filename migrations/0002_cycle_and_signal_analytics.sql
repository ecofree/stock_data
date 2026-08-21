CREATE TABLE IF NOT EXISTS market_cycle_phase (
    trade_date DATE PRIMARY KEY,
    phase VARCHAR,
    limit_up_count INTEGER,
    limit_down_count INTEGER,
    blown_rate DOUBLE,
    max_board INTEGER,
    premium_pct DOUBLE,
    promotion_rate DOUBLE,
    score DOUBLE,
    rationale VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS limit_premium_matrix (
    prev_trade_date DATE,
    board_bucket VARCHAR,
    sample_size INTEGER,
    avg_pct DOUBLE,
    median_pct DOUBLE,
    win_rate DOUBLE,
    computed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(prev_trade_date, board_bucket)
);

CREATE TABLE IF NOT EXISTS promotion_rate_matrix (
    trade_date DATE,
    from_board INTEGER,
    candidates INTEGER,
    promoted INTEGER,
    rate DOUBLE,
    computed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, from_board)
);

CREATE TABLE IF NOT EXISTS hot_money_profile (
    broker_name VARCHAR,
    appearances INTEGER,
    total_buy_amount DOUBLE,
    win_rate_3d DOUBLE,
    avg_ret_3d DOUBLE,
    last_seen DATE,
    computed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(broker_name)
);

CREATE TABLE IF NOT EXISTS auction_pattern_stats (
    anomaly_type VARCHAR,
    trade_date DATE,
    occurrences INTEGER,
    day_win_rate DOUBLE,
    day_avg_oc_pct DOUBLE,
    computed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(anomaly_type, trade_date)
);

CREATE TABLE IF NOT EXISTS signal_stage_attribution (
    stage VARCHAR,
    phase VARCHAR,
    horizon VARCHAR,
    n_signals INTEGER,
    win_rate DOUBLE,
    avg_ret_pct DOUBLE,
    median_ret_pct DOUBLE,
    as_of DATE,
    computed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(stage, phase, horizon)
);
