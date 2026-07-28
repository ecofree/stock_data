from scripts.collect_tushare_basic_data import SKIPPED, _format_collection_line


def test_format_collection_line_marks_skipped_tables():
    line = _format_collection_line("tushare_daily", SKIPPED, 123)

    assert line == "tushare_daily: skipped total_rows=123"
