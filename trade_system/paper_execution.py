"""Retired V1 paper writer; historical implementation remains in Git.

No flag or caller-supplied approval can restore this writer. New paper
activity must use V2 certificates and the paper ledger.
"""


def _retired(*args, **kwargs):
    raise RuntimeError('retired legacy paper writer; use stock-data-paper-desk with a V2 certificate')


ensure_paper_tables = _retired
submit_paper_order = _retired
simulate_fill = _retired
release_t1_sellable = _retired
