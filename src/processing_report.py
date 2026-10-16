"""Builds the human-readable summary rows, CSV/JSON reports and plain-language UI text."""
from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any, List, Tuple

from src import file_manager
from src.models import ExtractionResult

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    """CSV cell that spreadsheet apps will not interpret as a formula."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA_START) else s


def _method_counts(result: ExtractionResult) -> str:
    c = Counter(p.method for p in result.page_results if p.method)
    return ", ".join(f"{m} x{n}" for m, n in sorted(c.items())) or "none"


def summary_rows(result: ExtractionResult) -> List[Tuple[str, Any]]:
    pages = result.page_results
    ok = sum(1 for p in pages if p.status in ("ok", "low_quality", "no_tables", "blank"))
    failed = [p.page for p in pages if p.status == "failed"]
    ocr_kept = [p.page for p in pages if p.ocr_used]
    ocr_tried = [p.page for p in pages if p.ocr_attempted]
    low = [p.page for p in pages if p.status == "low_quality"]
    sev = Counter(i.severity for i in result.issues)
    return [
        ("Input filename", file_manager.display_name(result.filename)),
        ("File SHA-256", result.sha256),
        ("Completion status", result.status),
        ("Status detail", result.status_detail),
        ("Total pages", result.total_pages),
        ("Successfully processed pages", ok),
        ("Failed pages", len(failed)),
        ("Failed page numbers", ", ".join(map(str, failed)) or "none"),
        ("Pages needing review (low heuristic quality)", ", ".join(map(str, low)) or "none"),
        ("OCR pages (OCR output used)", len(ocr_kept)),
        ("OCR pages (OCR attempted)", len(ocr_tried)),
        ("Document type", result.doc_kind),
        ("Extracted tables", len(result.tables)),
        ("Extracted records (rows)", result.n_records),
        ("Validation issues (total)", len(result.issues)),
        ("  Errors", sev.get("Error", 0)),
        ("  Warnings", sev.get("Warning", 0)),
        ("  Info", sev.get("Info", 0)),
        ("Automatic corrections applied", len(result.corrections)),
        ("Extraction methods used", _method_counts(result)),
        ("Processing duration (seconds)", round(result.duration_s, 2)),
        ("OCR engine", result.ocr_version and f"Tesseract {result.ocr_version}" or "not available"),
        ("Notes", "Quality values are heuristics, not probabilities. OCR-derived values are unverified."),
    ]


def table_index_rows(result: ExtractionResult) -> List[List[Any]]:
    return [[t.sheet_name, t.table_id, _pages(t.pages), t.n_rows, t.n_cols] for t in result.tables]


def _pages(pages: List[int]) -> str:
    return ", ".join(map(str, pages))


def page_rows(result: ExtractionResult) -> List[List[Any]]:
    out: List[List[Any]] = []
    for p in result.page_results:
        note = p.failure_reason or "; ".join(p.warnings)
        out.append([p.page, p.kind, p.status, p.method or "-", len([a for a in p.attempts if a.reason != "OCR unavailable (Tesseract not found)"]), note])
    return out


# --------------------------------------------------------------------------- files

def validation_csv_bytes(result: ExtractionResult) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Issue ID", "Severity", "Category", "Page", "Sheet", "Row Reference", "Column Name",
                "Issue Description", "Suggested Action", "Resolution Status"])
    for i in result.issues:
        w.writerow([csv_safe(x) for x in (
            i.issue_id, i.severity, i.category, i.page or "", i.sheet or "",
            f"Row {i.excel_row}" if i.excel_row else "", i.column or "", i.description, i.action, i.status)])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel on Windows opens UTF-8 correctly


def write_validation_csv(result: ExtractionResult, out_dir: Path, base: str) -> Path:
    tmp = file_manager.unique_path(Path(out_dir), base, ".csv")
    tmp.write_bytes(validation_csv_bytes(result))
    return tmp


def write_processing_log(result: ExtractionResult, out_dir: Path, base: str) -> Path:
    tmp = file_manager.unique_path(Path(out_dir), base, ".json")
    tmp.write_text(json.dumps(result.to_log_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return tmp


def plain_summary(result: ExtractionResult) -> str:
    """Markdown for the UI. States only facts that were measured."""
    sev = Counter(i.severity for i in result.issues)
    icon = {"Complete": "✅", "Complete (review needed)": "⚠️", "Partial": "⚠️", "No tables found": "ℹ️", "Failed": "❌"}.get(result.status, "")
    lines = [
        f"### {icon} {result.status}",
        result.status_detail,
        "",
        f"- **Pages:** {result.total_pages} ({sum(1 for p in result.page_results if p.ocr_used)} read with OCR)",
        f"- **Tables:** {len(result.tables)}  ·  **Records:** {result.n_records}",
        f"- **Validation:** {sev.get('Error', 0)} error(s), {sev.get('Warning', 0)} warning(s), {sev.get('Info', 0)} note(s)",
        f"- **Time:** {result.duration_s:.1f} s",
    ]
    if result.status == "Partial":
        lines.append("- **This workbook is PARTIAL.** Check the failed pages listed in the Validation_Report sheet.")
    if any(p.ocr_used for p in result.page_results):
        lines.append("- OCR values are **not verified**. Spot-check the numbers against the PDF.")
    for w in result.warnings:
        lines.append(f"- ⚠️ {w}")
    return "\n".join(lines)
