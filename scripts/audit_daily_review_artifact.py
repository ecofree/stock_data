"""Fail-closed audit for the published self-contained daily review HTML."""
from __future__ import annotations

import argparse
from html import escape
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import duckdb


class _EmbeddedJsonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.current: str | None = None
        self.buffers: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("type") == "application/json" and values.get("id"):
            self.current = str(values["id"])
            self.buffers.setdefault(self.current, [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current:
            self.buffers[self.current].append(data)


def _embedded_json(html_text: str) -> dict[str, Any]:
    parser = _EmbeddedJsonParser()
    parser.feed(html_text)
    result: dict[str, Any] = {}
    for key, chunks in parser.buffers.items():
        result[key] = json.loads("".join(chunks).replace("<\\/", "</"))
    return result


def audit_artifact(db_path: str | Path, html_path: str | Path, trade_date: str) -> dict[str, Any]:
    path = Path(html_path)
    text = path.read_text(encoding="utf-8")
    embedded = _embedded_json(text)
    trail = embedded.get("trail-data") or {}
    details = embedded.get("trail-details") or {}
    concept_groups: list[dict[str, Any]] = []
    concept_match = re.search(
        r"let conceptData = (.*?);\s*const lazyAsset", text, re.S
    )
    if "review-concept-data" in embedded:
        concept_groups = embedded["review-concept-data"]
        failures = [] if isinstance(concept_groups, list) else ["concept_data_json_invalid"]
        if failures:
            concept_groups = []
    elif concept_match:
        try:
            concept_groups = json.loads(concept_match.group(1))
        except json.JSONDecodeError:
            failures = ["concept_data_json_invalid"]
        else:
            failures = []
    else:
        failures = ["concept_data_missing"]
    sectors = trail.get("sectors") or []
    dates = trail.get("dates") or []
    latest_day = str(dates[0]) if dates else ""
    shared_stock_pct = trail.get("stock_pct") or {}
    warnings: list[str] = []

    if f"<title>盘后复盘 · {trade_date}</title>" not in text:
        failures.append("title_trade_date_mismatch")
    if latest_day != trade_date:
        failures.append(f"trail_latest_day={latest_day or 'missing'}")
    if "\ufffd" in text:
        failures.append("unicode_replacement_character_present")
    if len(path.read_bytes()) > 25 * 1024 * 1024:
        failures.append("artifact_larger_than_25MiB")
    if not {"trail-data", "trail-details"}.issubset(embedded):
        failures.append("required_inline_payload_missing")

    # Initial DOM remains bounded. Full data is inline and only rendered after
    # the operator clicks the explicit expansion control.
    # Server output contains one latest-day card; later cards/rows are created
    # from JSON by JavaScript and use double-quoted class attributes.
    initial_rows = len(re.findall(r"<button type='button' class='day-row", text))
    if initial_rows > 50:
        failures.append(f"initial_concept_rows={initial_rows}")
    if len(sectors) > 50 and "加载全部" not in text:
        failures.append("concept_expand_control_missing")

    payload_ids = {str(row.get("id") or "") for row in sectors if row.get("id")}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        snap_row = con.execute(
            "SELECT max(CAST(trade_date AS DATE)) FROM v_default_concept_daily "
            "WHERE CAST(trade_date AS DATE)<=CAST(? AS DATE)",
            [trade_date],
        ).fetchone()
        snapshot = str(snap_row[0]) if snap_row and snap_row[0] else ""
        db_concepts = {
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT CAST(concept_code AS VARCHAR) "
                "FROM v_default_concept_daily WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)",
                [snapshot],
            ).fetchall()
        } if snapshot else set()
        unknown = sorted(payload_ids - db_concepts)
        if unknown:
            failures.append(f"unknown_concept_ids={len(unknown)}")
        coverage = (100.0 * len(payload_ids) / len(db_concepts)) if db_concepts else 0.0
        if coverage < 90.0:
            failures.append(f"concept_coverage_pct={coverage:.2f}")

        member_rows = con.execute(
            "SELECT CAST(concept_code AS VARCHAR), "
            "regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') "
            "FROM v_default_concept_stock_history "
            "WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)",
            [snapshot],
        ).fetchall() if snapshot else []
        db_members: dict[str, set[str]] = {}
        for concept_code, stock_code in member_rows:
            db_members.setdefault(str(concept_code), set()).add(str(stock_code))

        member_mismatches = 0
        for sector in sectors:
            sid = str(sector.get("id") or "")
            members = sector.get("members") or {}
            codes = [str(code) for code in (members.get("codes") or [])]
            if set(codes) != db_members.get(sid, set()):
                member_mismatches += 1
        if member_mismatches:
            failures.append(f"concept_member_set_mismatches={member_mismatches}")
        # Return data is serialized once per stock/date and shared by all
        # overlapping concepts.  This replaced the old dense per-concept
        # vectors, which duplicated hundreds of thousands of values and made
        # post-QLib rendering prone to OOM.
        if not isinstance(shared_stock_pct, dict) or latest_day not in shared_stock_pct:
            failures.append("shared_stock_pct_latest_day_missing")
        elif not isinstance(shared_stock_pct.get(latest_day), dict):
            failures.append("shared_stock_pct_latest_day_invalid")

        # The page must contain every same-date THS concept/limit-up
        # relationship. A percentage coverage threshold hid the four broad
        # concepts and their 118 valid stock relationships.
        expected_limit_rows = con.execute(
            """
            SELECT DISTINCT CAST(m.concept_code AS VARCHAR),
                   regexp_replace(CAST(m.stock_code AS VARCHAR), '[.].*$', '')
            FROM v_default_concept_stock_history m
            JOIN v_limit_pool l
              ON regexp_replace(CAST(m.stock_code AS VARCHAR), '[.].*$', '') =
                 regexp_replace(CAST(l.stock_code AS VARCHAR), '[.].*$', '')
             AND CAST(l.trade_date AS DATE) = CAST(? AS DATE)
            WHERE CAST(m.trade_date AS DATE) = CAST(? AS DATE)
            """,
            [trade_date, snapshot],
        ).fetchall() if snapshot else []
        expected_pairs = {(str(a), str(b)) for a, b in expected_limit_rows}
        page_concept_pairs = {
            (str(group.get("concept_code") or ""),
             re.sub(r"[.].*$", "", str(stock.get("stock_code") or "")))
            for group in concept_groups
            for stock in (group.get("limit_up_stocks") or [])
        }
        if expected_pairs != page_concept_pairs:
            failures.append(
                "concept_limit_up_pairs_mismatch="
                f"expected_{len(expected_pairs)}_actual_{len(page_concept_pairs)}"
            )
        expected_concepts = {pair[0] for pair in expected_pairs}
        page_concepts = {str(group.get("concept_code") or "") for group in concept_groups}
        if expected_concepts != page_concepts:
            failures.append(
                "concept_limit_up_concepts_mismatch="
                f"expected_{len(expected_concepts)}_actual_{len(page_concepts)}"
            )

        # Check the serialized annotation itself. Browser rendering is
        # date-aware after the corresponding JS fix, so no valid pair may
        # arrive without board/time fields.
        annotation_pairs = set()
        annotation_missing = 0
        for sid, pack in ((details.get("daily") or {}).items()):
            for stock in (((pack.get("daily") or {}).get(trade_date) or {}).get("stocks") or []):
                code = re.sub(r"[.].*$", "", str(stock.get("stock_code") or ""))
                annotation_pairs.add((str(sid), code))
                if stock.get("board_level") is None or not str(stock.get("limit_up_time") or "").strip():
                    annotation_missing += 1
        if expected_pairs != annotation_pairs:
            failures.append(
                "trail_limit_up_pairs_mismatch="
                f"expected_{len(expected_pairs)}_actual_{len(annotation_pairs)}"
            )
        if annotation_missing:
            failures.append(f"trail_limit_up_annotations_missing={annotation_missing}")

        # The lifecycle timeline is a separate presentation surface from the
        # drill-down payload. Verify that its displayed counts are limit-up
        # counts, not raw concept-member counts.
        timeline_start = text.find('id="s-extra-theme_timeline"')
        timeline_end = text.find('id="s-extra-first_seal"', timeline_start)
        if timeline_start >= 0 and timeline_end > timeline_start:
            timeline_text = text[timeline_start:timeline_end]
            expected_timeline = {
                (escape(str(name), quote=False), str(day)[5:], int(count))
                for name, day, count in con.execute(
                    """
                    SELECT h.concept_name, CAST(h.trade_date AS DATE), count(DISTINCT l.stock_code)
                    FROM ths_concept_stock_history h
                    LEFT JOIN v_limit_pool l
                      ON CAST(l.trade_date AS DATE)=CAST(h.trade_date AS DATE)
                     AND regexp_replace(CAST(l.stock_code AS VARCHAR), '[.].*$', '') =
                         regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '')
                    WHERE h.date_verified
                      AND CAST(h.trade_date AS DATE) IN (
                          SELECT DISTINCT CAST(trade_date AS DATE)
                          FROM ths_concept_stock_history
                          WHERE date_verified AND CAST(trade_date AS DATE)<=CAST(? AS DATE)
                          ORDER BY 1 DESC LIMIT 20
                      )
                    GROUP BY 1, 2
                    """,
                    [trade_date],
                ).fetchall()
            }
            timeline_counts = {
                (str(name), f"{month_day}", int(count))
                for name, month_day, count in re.findall(
                    r"title='([^']*) (\d{2}-\d{2})：([0-9]+) 只涨停'", timeline_text
                )
            }
            for name, day_suffix, count in timeline_counts:
                expected_for_day = [
                    value for title, suffix, value in expected_timeline
                    if title == name and suffix == day_suffix
                ]
                expected_value = expected_for_day[0] if expected_for_day else 0
                if count != expected_value:
                    failures.append(
                        f"theme_timeline_count_mismatch={name}_{day_suffix}_displayed_{count}_expected_{expected_value}"
                    )
                    break

        # The first-seal buckets must read the canonical date-aware limit-up
        # pool and parse HH:MM:SS values at minute precision.
        seal_start = text.find('id="s-extra-first_seal"')
        seal_end = text.find('id="s-extra-qlib_screen"', seal_start)
        if seal_start >= 0 and seal_end > seal_start:
            seal_text = text[seal_start:seal_end]
            expected_seal = []
            for lo, hi in ((925, 926), (926, 1000), (1000, 1130), (1300, 1400), (1400, 1500)):
                expected_seal.append(int(con.execute(
                    """SELECT count(*) FROM v_limit_pool
                       WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)
                         AND limit_up_time IS NOT NULL
                         AND TRY_CAST(replace(substr(limit_up_time, 1, 5), ':', '') AS INTEGER)
                             BETWEEN ? AND ?""",
                    [trade_date, lo, hi],
                ).fetchone()[0] or 0))
            displayed_seal = [
                int(value) for value in re.findall(r"title='[^']* ([0-9]+)只\(", seal_text)
            ]
            if displayed_seal != expected_seal:
                failures.append(
                    f"first_seal_bucket_mismatch=displayed_{displayed_seal}_expected_{expected_seal}"
                )

        max_board_row = con.execute(
            "SELECT max(CAST(board_level AS INTEGER)) FROM v_limit_pool "
            "WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)",
            [trade_date],
        ).fetchone()
        max_board = int(max_board_row[0] or 0) if max_board_row else 0
        board_pattern = re.compile(
            r"当前最高连板.*?<span[^>]*>\s*" + re.escape(str(max_board)) + r"\s*</span>\s*板",
            re.S,
        )
        if max_board and not board_pattern.search(text):
            failures.append(f"displayed_highest_board_mismatch_db={max_board}")

        limit_codes = {
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') "
                "FROM v_limit_pool WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)",
                [trade_date],
            ).fetchall()
        }
        bad_detail_rows = 0
        for sid, pack in ((details.get("daily") or {}).items()):
            for stock in (((pack.get("daily") or {}).get(trade_date) or {}).get("stocks") or []):
                code = re.sub(r"[.].*$", "", str(stock.get("stock_code") or ""))
                if code not in limit_codes or code not in db_members.get(str(sid), set()):
                    bad_detail_rows += 1
        if bad_detail_rows:
            failures.append(f"invalid_latest_limitup_detail_rows={bad_detail_rows}")
    finally:
        con.close()

    if not sectors:
        failures.append("sector_payload_empty")
    return {
        "status": "pass" if not failures else "fail",
        "trade_date": trade_date,
        "artifact": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "latest_day": latest_day,
        "concepts": len(sectors),
        "limit_up_concepts": len({str(group.get("concept_code") or "") for group in concept_groups}),
        "limit_up_pairs": len({
            (str(group.get("concept_code") or ""), str(stock.get("stock_code") or ""))
            for group in concept_groups for stock in (group.get("limit_up_stocks") or [])
        }),
        "initial_concept_rows": initial_rows,
        "membership_snapshot": snapshot,
        "concept_coverage_pct": round(coverage, 2),
        "failures": failures,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True)
    parser.add_argument("--html", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = audit_artifact(args.db, args.html, args.date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Daily Review Artifact Audit - {args.date}", "",
        f"- Status: `{result['status']}`",
        f"- Artifact: `{result['artifact']}`",
        f"- Size: `{result['size_bytes']}` bytes",
        f"- Concepts: `{result['concepts']}` (canonical coverage `{result['concept_coverage_pct']}%`)",
        f"- Initial concept rows: `{result['initial_concept_rows']}`",
        f"- Membership snapshot: `{result['membership_snapshot']}`", "",
        "## Failures", "",
    ]
    lines.extend(f"- `{item}`" for item in result["failures"])
    if not result["failures"]:
        lines.append("- None")
    lines.extend(["", "## Machine result", "", "```json", json.dumps(result, ensure_ascii=False, indent=2), "```", ""])
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
