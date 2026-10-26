# Historical operator outcomes

The legacy CSV import is permanently retired. Both
`scripts/import_operator_trade_outcomes.py` and
`trade_system.operator_outcomes.import_operator_trade_outcomes` reject before
opening a CSV or database. No environment variable or command flag reopens it.
It cannot update `trade_plan`, `trade_journal`, or `operator_trade_outcome`.

Existing records and read-only historical reports remain available. Their
recorded returns are historical operator declarations, not verified current
account facts or evidence of live execution. Proxy K-line samples remain
diagnostic data, not realized operator performance.

`create_operator_outcome_template.py` can export a new review file without
overwriting another file. Filling that file does not authorize import and does
not close the current research feedback loop.

Current account management uses the V2 `account_import` workflow on its explicit
independent database, with a complete manual declaration or broker export and
reconciliation. This does not replace outcome import and must not automatically
infer cash, sellable holdings or pending orders from historical legacy tables.
A future outcome-ingestion workflow requires its own provenance, validation and
authorization. No production migration or resumed operation is implied.
