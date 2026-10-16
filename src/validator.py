"""Validation of extracted tables and page outcomes.

Findings are classified as
* **Confirmed**  - a hard invariant is violated (impossible date, failed page, empty table ...)
* **Potential**  - suspicious; a human should look (outliers, duplicates, misalignment ...)
* **Unverified** - nothing is known to be wrong, but the value was not independently verified (OCR output ...)

The validator NEVER edits values. Deterministic corrections live in
``schema_normalizer.apply_ocr_digit_fixes`` and are recorded separately.
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence

from config.settings import SETTINGS, Settings
from src.models import Correction, Issue, PageResult, Table
from src.schema_normalizer import (
    ColumnType, infer_column_types, parse_date_parts, parse_number, resolve_date,
)

log = logging.getLogger(__name__)

_TOTAL_WORDS = re.compile(r"\b(sub\s*-?\s*total|grand\s+total|total|net\s+total)\b", re.I)


class _Collector:
    def __init__(self, cap: int):
        self.items: List[Issue] = []
        self.cap = cap
        self.per_check: Counter = Counter()
        self.suppressed: Counter = Counter()

    def add(self, **kw) -> None:
        key = (kw.get("check"), kw.get("table_id"))
        if self.per_check[key] >= self.cap:
            self.suppressed[key] += 1
            return
        self.per_check[key] += 1
        self.items.append(Issue(issue_id="", **kw))


def validate(
    tables: Sequence[Table],
    page_results: Sequence[PageResult],
    corrections: Sequence[Correction] = (),
    settings: Settings = SETTINGS,
    ocr_available: bool = True,
    aborted_for_time: bool = False,
    total_pages: Optional[int] = None,
) -> List[Issue]:
    col = _Collector(settings.max_issues_per_check)

    _validate_pages(col, page_results, ocr_available, aborted_for_time)
    for t in tables:
        _validate_table(col, t, settings)

    for (check, tid), n in col.suppressed.items():
        col.items.append(Issue(
            issue_id="", severity="Info", category="Potential", check="issue_cap", table_id=tid,
            description=f"{n} further '{check}' finding(s) were not listed (limit {settings.max_issues_per_check} per check per table).",
            action="Resolve the most common cause first, then re-run.",
        ))

    for c in corrections:
        col.items.append(Issue(
            issue_id="", severity="Info", category="Confirmed", check="auto_correction",
            description=f"Automatically corrected '{c.original}' -> '{c.corrected}' ({c.rule}). {c.reason}",
            action="Compare with the source page if the value is critical.",
            page=c.page, table_id=c.table_id, row=c.row, column=c.column, status="Auto-corrected",
        ))

    issues = col.items
    order = {"Error": 0, "Warning": 1, "Info": 2}
    issues.sort(key=lambda i: (order.get(i.severity, 3), i.page or 0, i.table_id or "", i.row or 0))
    for n, i in enumerate(issues, start=1):
        i.issue_id = f"V{n:03d}"
    return issues


# --------------------------------------------------------------------------- page-level

def _validate_pages(col: _Collector, pages: Sequence[PageResult], ocr_available: bool, aborted: bool) -> None:
    for p in pages:
        if p.status == "failed":
            col.add(
                severity="Error", category="Confirmed", check="failed_page", page=p.page,
                description=f"Page {p.page} could not be processed: {p.failure_reason or 'unknown reason'}.",
                action="Check this page in the PDF manually; its data is NOT in the workbook.",
            )
        elif p.status == "low_quality":
            col.add(
                severity="Warning", category="Potential", check="low_quality_page", page=p.page,
                description=f"Extraction quality for page {p.page} is below the acceptance threshold (heuristic score {p.score:.2f}; method {p.method}). Columns or rows may be wrong.",
                action="Compare the extracted rows with the source page.",
            )
        elif p.status == "no_tables" and p.kind in ("scanned", "unreliable_text", "mixed") and p.ocr_attempted is False and not ocr_available:
            col.add(
                severity="Error", category="Confirmed", check="ocr_required", page=p.page,
                description=f"Page {p.page} needs OCR but Tesseract is not available.",
                action="Install Tesseract (see README) and convert again.",
            )
        elif p.status == "no_tables":
            col.add(
                severity="Info", category="Unverified", check="no_tables_on_page", page=p.page,
                description=f"No table was found on page {p.page} ({p.kind} page).",
                action="If this page should contain a table, check the source.",
            )
        if p.ocr_used:
            conf = f" (mean Tesseract word confidence {p.ocr_mean_conf:.0f}/100)" if p.ocr_mean_conf is not None else ""
            col.add(
                severity="Info", category="Unverified", check="ocr_unverified", page=p.page,
                description=f"Values on page {p.page} come from OCR{conf} and have not been independently verified.",
                action="Spot-check OCR-derived values against the source, especially numbers.",
            )
        for w in p.warnings:
            col.add(severity="Warning", category="Potential", check="page_warning", page=p.page,
                    description=f"Page {p.page}: {w}", action="Review this page.")
    if aborted:
        col.add(
            severity="Error", category="Confirmed", check="time_budget", description="Processing stopped early because the time budget was exhausted; later pages were not processed.",
            action="Split the document into smaller parts or raise DOCUPILOT_MAX_SECONDS.",
        )


# --------------------------------------------------------------------------- table-level

def _sheet_ref(t: Table) -> str:
    return t.sheet_name or t.table_id


def _validate_table(col: _Collector, t: Table, s: Settings) -> None:
    common = dict(table_id=t.table_id, sheet=t.sheet_name or None)

    if not t.rows:
        col.add(severity="Error", category="Confirmed", check="empty_table",
                description=f"{t.table_id} has a header but no data rows.", action="Check the source page.",
                page=(t.pages[0] if t.pages else None), **common)
        return

    for note in t.notes:
        if "Possible continuation" in note:
            col.add(severity="Warning", category="Potential", check="possible_continuation",
                    description=f"{t.table_id}: {note}", action="Decide manually whether this table continues the previous one.",
                    page=(t.pages[0] if t.pages else None), **common)
        elif note.startswith("No header row found") or "had no header in the source" in note:
            col.add(severity="Info", category="Unverified", check="inferred_headers",
                    description=f"{t.table_id}: {note}", action="Rename the neutral columns if you know their meaning.",
                    page=(t.pages[0] if t.pages else None), **common)

    _removed_content(col, t, common)
    types = infer_column_types(t)
    _rows_ragged(col, t, common)
    _duplicates(col, t, common)
    _repeated_header_rows(col, t, common)
    _missing_values(col, t, types, common)
    _numeric_checks(col, t, types, common, s)
    _date_checks(col, t, types, common)
    _alignment(col, t, types, common)
    _totals(col, t, types, common, s)
    _wrapped_text(col, t, common)
    _low_ocr_conf(col, t, common, s)


def _row_issue_kwargs(t: Table, ri: int, common: dict) -> dict:
    return dict(row=ri + 1, excel_row=ri + 2, page=t.row_pages[ri], **common)


def _removed_content(col, t: Table, common) -> None:
    """Make every omission auditable: lines/rows/columns the reconstructor removed are reported (Info)."""
    furniture = headers = empty_cols = 0
    for n in t.notes:
        m = re.search(r"removed (\d+) page header/footer/page-number line", n)
        if m:
            furniture += int(m.group(1))
        if "repeated header row removed" in n:
            headers += 1
        m = re.search(r"Removed (\d+) repeated header row", n)
        if m:
            headers += int(m.group(1))
        m = re.search(r"dropped (\d+) completely empty column", n)
        if m:
            empty_cols += int(m.group(1))
    pg = t.pages[0] if t.pages else None
    if furniture:
        col.add(severity="Info", category="Unverified", check="removed_page_furniture", page=pg,
                description=f"{t.table_id}: {furniture} single-cell line(s) identified as page numbers or repeated running headers/footers were removed.",
                action="Check the source if one of these lines was actually data.", **common)
    if headers:
        col.add(severity="Info", category="Confirmed", check="removed_repeated_headers", page=pg,
                description=f"{t.table_id}: {headers} repeated header row(s) were removed (the table continues across pages).",
                action="No action needed unless the table was not meant to continue.", **common)
    if empty_cols:
        col.add(severity="Info", category="Unverified", check="dropped_empty_columns", page=pg,
                description=f"{t.table_id}: {empty_cols} column(s) that were completely empty (header and cells) were dropped.",
                action="Check the source if a column of values was expected there.", **common)


def _rows_ragged(col, t: Table, common) -> None:
    for ri, notes in sorted(t.row_notes.items()):
        for n in notes:
            col.add(severity="Warning", category="Confirmed", check="row_length",
                    description=f"Inconsistent row length: {n}. Cells may be shifted.",
                    action="Compare this row with the source and correct the cell positions.",
                    column=None, **_row_issue_kwargs(t, ri, common))


def _duplicates(col, t: Table, common) -> None:
    seen: Dict[tuple, int] = {}
    for ri, r in enumerate(t.rows):
        key = tuple(c.strip().lower() for c in r)
        if not any(key):
            continue
        if key in seen:
            col.add(severity="Warning", category="Potential", check="duplicate_record",
                    description=f"Row is identical to row {seen[key] + 1} (possible duplicate record).",
                    action="Confirm whether the duplicate is genuine; remove if it is an extraction artefact.",
                    **_row_issue_kwargs(t, ri, common))
        else:
            seen[key] = ri


def _repeated_header_rows(col, t: Table, common) -> None:
    hdr = [h.strip().lower() for h in t.headers]
    for ri, r in enumerate(t.rows):
        if [c.strip().lower() for c in r] == hdr:
            col.add(severity="Warning", category="Confirmed", check="repeated_header",
                    description="Row repeats the table header.", action="Delete this row.",
                    **_row_issue_kwargs(t, ri, common))


def _missing_values(col, t: Table, types: List[ColumnType], common) -> None:
    n = len(t.rows)
    for ct in types:
        if ct.kind == "empty":
            col.add(severity="Info", category="Potential", check="empty_column",
                    description=f"Column '{ct.name}' is empty in every row.", action="Check whether the column was split incorrectly.",
                    column=ct.name, page=(t.pages[0] if t.pages else None), **common)
            continue
        fill = ct.n_filled / n
        if n >= 5 and fill >= 0.8 and ct.n_filled < n:
            for ri, r in enumerate(t.rows):
                v = r[ct.index] if ct.index < len(r) else ""
                if not v.strip() and not _TOTAL_WORDS.search(" ".join(r)):  # total rows are legitimately sparse
                    col.add(severity="Warning", category="Potential", check="missing_value",
                            description=f"Blank cell in column '{ct.name}' (the column is filled in {fill:.0%} of rows).",
                            action="Check the source: the value may be genuinely blank or it may have been missed.",
                            column=ct.name, **_row_issue_kwargs(t, ri, common))


def _numeric_checks(col, t: Table, types: List[ColumnType], common, s: Settings) -> None:
    for ct in types:
        if ct.kind != "numeric":
            continue
        vals: List[tuple] = []
        for ri, r in enumerate(t.rows):
            v = r[ct.index] if ct.index < len(r) else ""
            if not v.strip():
                continue
            num = parse_number(v)
            if num is None and re.search(r"[A-Za-z]{3,}", v):
                continue  # words in a numeric column are reported once, by the misalignment check
            if num is None:
                col.add(severity="Warning", category="Potential", check="numeric_parse",
                        description=f"'{v}' could not be read as a number in numeric column '{ct.name}'.",
                        action="Check for OCR errors, misplaced text or an unusual number format.",
                        column=ct.name, **_row_issue_kwargs(t, ri, common))
            else:
                vals.append((ri, num))
        body = [v for ri, v in vals if not _TOTAL_WORDS.search(" ".join(t.rows[ri]))]
        if len(body) >= 8:
            q = statistics.quantiles(body, n=4)
            iqr = q[2] - q[0]
            lo, hi = q[0] - s.outlier_iqr_factor * iqr, q[2] + s.outlier_iqr_factor * iqr
            if iqr > 0:
                for ri, num in vals:
                    if _TOTAL_WORDS.search(" ".join(t.rows[ri])):
                        continue
                    if num < lo or num > hi:
                        col.add(severity="Warning", category="Potential", check="suspicious_value",
                                description=f"Value {t.rows[ri][ct.index]} in '{ct.name}' is far outside the typical range of this column (possible OCR or alignment error).",
                                action="Verify against the source.", column=ct.name, **_row_issue_kwargs(t, ri, common))


def _date_checks(col, t: Table, types: List[ColumnType], common) -> None:
    for ct in types:
        if ct.kind != "date":
            continue
        if ct.date_order == "ambiguous":
            col.add(severity="Warning", category="Potential", check="ambiguous_dates",
                    description=f"Dates in column '{ct.name}' could be day/month or month/day (no value above 12 settles it). They were kept as text.",
                    action="Confirm the date convention of the source document.", column=ct.name,
                    page=(t.pages[0] if t.pages else None), **common)
        elif ct.date_order == "conflict":
            col.add(severity="Error", category="Confirmed", check="date_order_conflict",
                    description=f"Column '{ct.name}' contains values that only make sense as day-first AND values that only make sense as month-first.",
                    action="Check these dates manually; some may be misread.", column=ct.name,
                    page=(t.pages[0] if t.pages else None), **common)
        for ri, r in enumerate(t.rows):
            v = r[ct.index] if ct.index < len(r) else ""
            parts = parse_date_parts(v) if v.strip() else None
            if parts is None:
                if v.strip():
                    col.add(severity="Warning", category="Potential", check="date_parse",
                            description=f"'{v}' does not look like a date in date column '{ct.name}'.", action="Check the source.",
                            column=ct.name, **_row_issue_kwargs(t, ri, common))
                continue
            _d, valid = resolve_date(parts, ct.date_order)
            if not valid:
                col.add(severity="Error", category="Confirmed", check="invalid_date",
                        description=f"'{v}' is not a valid calendar date.", action="Correct the date after checking the source.",
                        column=ct.name, **_row_issue_kwargs(t, ri, common))


def _alignment(col, t: Table, types: List[ColumnType], common) -> None:
    n = len(t.rows)
    for ct in types:
        if ct.kind == "numeric":
            for ri, r in enumerate(t.rows):
                v = r[ct.index] if ct.index < len(r) else ""
                if v.strip() and parse_number(v) is None and re.search(r"[A-Za-z]{3,}", v) and not _TOTAL_WORDS.search(" ".join(r)):
                    col.add(severity="Warning", category="Potential", check="column_misalignment",
                            description=f"Text '{v}' appears in numeric column '{ct.name}' - the cell may belong to a neighbouring column.",
                            action="Check the cell positions against the source.", column=ct.name, **_row_issue_kwargs(t, ri, common))
        elif ct.kind == "text" and not ct.id_like and ct.n_filled >= 8:
            alpha = sum(1 for r in t.rows if ct.index < len(r) and re.search(r"[A-Za-z]{2,}", r[ct.index] or ""))
            if alpha / max(ct.n_filled, 1) >= 0.9:
                for ri, r in enumerate(t.rows):
                    v = r[ct.index] if ct.index < len(r) else ""
                    if v.strip() and parse_number(v) is not None:
                        col.add(severity="Warning", category="Potential", check="column_misalignment",
                                description=f"Number '{v}' appears in text column '{ct.name}' - the cell may belong to a neighbouring column.",
                                action="Check the cell positions against the source.", column=ct.name, **_row_issue_kwargs(t, ri, common))


def _totals(col, t: Table, types: List[ColumnType], common, s: Settings) -> None:
    total_rows = [ri for ri, r in enumerate(t.rows) if _TOTAL_WORDS.search(" ".join(c for c in r if c)) and sum(1 for c in r if c) <= max(3, len(r) // 2 + 1)]
    if not total_rows:
        return
    is_total = set(total_rows)
    for ct in types:
        if ct.kind != "numeric":
            continue
        nums = {ri: parse_number(r[ct.index]) for ri, r in enumerate(t.rows) if ct.index < len(r) and r[ct.index].strip()}
        for ri in total_rows:
            tv = nums.get(ri)
            if tv is None:
                continue
            prev_total = max([x for x in total_rows if x < ri], default=-1)
            cands = []
            segment = [v for k, v in nums.items() if prev_total < k < ri and k not in is_total and v is not None]
            allbody = [v for k, v in nums.items() if k < ri and k not in is_total and v is not None]
            subs = [nums[k] for k in total_rows if k < ri and nums.get(k) is not None]
            if segment:
                cands.append(sum(segment))
            if allbody:
                cands.append(sum(allbody))
            if subs:
                cands.append(sum(subs))
                if segment:
                    cands.append(sum(subs) + sum(segment))
            if not cands:
                continue
            tol = max(s.total_tolerance_abs, abs(tv) * 1e-6)
            if not any(abs(c - tv) <= tol for c in cands):
                col.add(severity="Warning", category="Potential", check="total_mismatch",
                        description=f"Total {t.rows[ri][ct.index]} in '{ct.name}' does not equal the sum of the rows above it (nearest sum: {cands[0]:,.2f}).",
                        action="Check for missed rows, misread digits or a total that includes hidden items.",
                        column=ct.name, **_row_issue_kwargs(t, ri, common))


def _wrapped_text(col, t: Table, common) -> None:
    """Rows holding text in a single non-first column right below a full row look like wrapped cell text
    that was read as a separate row. They are flagged, never merged (merging could corrupt real rows)."""
    for ri in range(1, len(t.rows)):
        r, prev = t.rows[ri], t.rows[ri - 1]
        filled = [j for j, c in enumerate(r) if c.strip()]
        if len(filled) != 1 or filled[0] == 0 or r[0].strip():
            continue
        j = filled[0]
        if (sum(1 for c in prev if c.strip()) >= 2 and prev[0].strip() and prev[j].strip()
                and parse_number(r[j]) is None and not _TOTAL_WORDS.search(" ".join(r))):
            col.add(severity="Warning", category="Potential", check="possible_wrapped_text",
                    description=f"Row has text only in column '{t.headers[j]}' - it may be the continuation of wrapped text from row {ri} (a cell that spans several lines).",
                    action="If so, append this text to the cell above and delete this row.",
                    column=t.headers[j], **_row_issue_kwargs(t, ri, common))


def _low_ocr_conf(col, t: Table, common, s: Settings) -> None:
    for ri, c in enumerate(t.row_conf):
        if c is not None and c < s.low_ocr_conf and t.row_methods[ri].startswith("ocr"):
            col.add(severity="Warning", category="Potential", check="low_ocr_confidence",
                    description=f"OCR word confidence for this row is low ({c:.0f}/100; threshold {s.low_ocr_conf:.0f}). Characters may be misread.",
                    action="Verify this row against the source.", **_row_issue_kwargs(t, ri, common))
