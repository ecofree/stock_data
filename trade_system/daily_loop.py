"""Retired V1 plan authority. Historical implementation remains at f8067b1.

The rejection boundary preserves manual and account facts and has no imports
with database, logging, or network side effects. No opt-in bypass exists.
"""


def run_daily_operator_loop(db_path, trade_date, limit=20, stage="close"):
    raise RuntimeError('retired legacy operator loop; manual plans and watchlists are protected')
