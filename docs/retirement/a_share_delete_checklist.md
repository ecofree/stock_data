# A-share Legacy Project Deletion Checklist

Legacy project: `D:\accio\A-share\kpl-qds`
Surviving project: `D:\accio\stock_data`

Delete the legacy project only after all checks are true:

- [ ] `scripts\audit_a_share_project.py` has generated `reports\integration_audit_latest.md`.
- [ ] `scripts\import_legacy_a_share.py` has generated `reports\legacy_import_latest.md`.
- [ ] `legacy_qds_daily_watchlist`, `legacy_qds_sector_strength`, `legacy_qds_limit_up_stocks`, and `legacy_qds_lhb_detail` exist in `kpl_data.duckdb`.
- [ ] `v_operator_candidates` contains both `stock_data` and `legacy_qds` rows.
- [ ] `scripts\run_integrated_daily.py --db kpl_data.duckdb` exits with code `0`.
- [ ] `pytest -q` passes.
- [ ] `reports\trading_dashboard_latest.html` opens and shows integrated candidates.
- [ ] No required API key exists only in `D:\accio\A-share\kpl-qds\config\settings.yaml`.
- [ ] User has explicitly confirmed deletion.

Recommended deletion method after confirmation:

```powershell
Remove-Item -LiteralPath 'D:\accio\A-share\kpl-qds' -Recurse
```
