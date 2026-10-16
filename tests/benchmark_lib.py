"""Benchmark cases, ground truth and metric definitions.

GROUND TRUTH
  * ``BANK_LITERAL`` and ``PO_*`` are hand-written literal tables (arithmetic re-checked by hand).
  * Other datasets come from seeded generators in ``fixture_factory`` / below: the expected cells ARE
    the strings printed into the PDF (ground truth by construction).
  * All documents are SYNTHETIC. Results say nothing certain about real-world documents.

METRICS (all computed from actual pipeline output, nothing hard-coded) - see docs/accuracy_report.md
"""
from __future__ import annotations

import random
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import fixture_factory as ff
from src.schema_normalizer import parse_number

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table as RLTable, TableStyle

# --------------------------------------------------------------------------- ground-truth data

BANK_HEADERS = ["Date", "Description", "Ref No", "Debit", "Credit", "Balance"]
BANK_LITERAL = [  # hand-written; running balance verified by hand
    ["01/04/2026", "Opening Balance", "", "", "", "50,000.00"],
    ["03/04/2026", "UPI/Grocery Mart", "000451", "1,250.00", "", "48,750.00"],
    ["05/04/2026", "Salary Credit", "000452", "", "45,000.00", "93,750.00"],
    ["07/04/2026", "Electricity Bill", "000453", "2,310.50", "", "91,439.50"],
    ["09/04/2026", "ATM Withdrawal", "000454", "5,000.00", "", "86,439.50"],
    ["12/04/2026", "NEFT Refund", "000455", "", "750.00", "87,189.50"],
    ["14/04/2026", "Rent Payment", "000456", "18,000.00", "", "69,189.50"],
    ["16/04/2026", "Mobile Recharge", "000457", "399.00", "", "68,790.50"],
    ["18/04/2026", "Interest Credit", "000458", "", "132.75", "68,923.25"],
    ["21/04/2026", "Insurance Premium", "000459", "6,420.00", "", "62,503.25"],
    ["23/04/2026", "Fuel Station", "000460", "2,150.00", "", "60,353.25"],
    ["25/04/2026", "Freelance Payment", "000461", "", "12,500.00", "72,853.25"],
    ["28/04/2026", "Water Bill", "000462", "640.00", "", "72,213.25"],
]
_BANK_DESC = [("Grocery Mart", 0), ("Salary Credit", 1), ("Electricity Bill", 0), ("Cash Withdrawal", 0), ("Refund", 1),
              ("Online Shopping", 0), ("Dividend Credit", 1), ("Pharmacy", 0), ("Restaurant", 0), ("Transfer In", 1)]


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def bank_rows(total: int = 60, seed: int = 11) -> List[List[str]]:
    """BANK_LITERAL, then seeded transactions with a consistent running balance, then a closing row."""
    rows = [r[:] for r in BANK_LITERAL]
    bal = 72213.25
    rng = random.Random(seed)
    ref = 463
    while len(rows) < total - 1:
        desc, credit = rng.choice(_BANK_DESC)
        amt = round(rng.uniform(80, 9000), 2)
        bal = round(bal + amt if credit else bal - amt, 2)
        day, month = rng.randint(13, 28), 5 + (len(rows) // 25)
        rows.append([f"{day:02d}/{month:02d}/2026", desc, f"{ref:06d}", "" if credit else _fmt(amt), _fmt(amt) if credit else "", _fmt(bal)])
        ref += 1
    rows.append(["30/06/2026", "Closing Balance", "", "", "", _fmt(bal)])
    return rows


PO_ITEM_HEADERS = ["Item Code", "Description", "Qty", "Unit Price", "Amount"]
PO_ITEMS = [  # hand-written; 250+120+70+225+75 = 740.00
    ["P-0010", "Steel Bolts M8", "100", "2.50", "250.00"],
    ["P-0020", "Hex Nuts M8", "100", "1.20", "120.00"],
    ["P-0030", "Washers 8mm", "200", "0.35", "70.00"],
    ["P-0040", "Drill Bits Set", "5", "45.00", "225.00"],
    ["P-0050", "Safety Gloves", "20", "3.75", "75.00"],
    ["Total", "", "", "", "740.00"],
]
PO_SCHED_HEADERS = ["Site", "Delivery Date", "Quantity", "Status"]
PO_SCHED = [
    ["Anantapur Depot", "2026-05-14", "150", "Scheduled"],
    ["Kadapa Store", "2026-05-21", "100", "Scheduled"],
    ["Tirupati Yard", "2026-05-28", "120", "Pending"],
    ["Chennai Hub", "2026-06-04", "55", "Pending"],
]

INV_HEADERS = ["SKU", "Product", "Category", "Unit", "In Stock", "Reorder Level", "Unit Cost", "Supplier", "Location", "Last Updated"]


def inventory_rows(n: int = 55, seed: int = 21) -> List[List[str]]:
    rng = random.Random(seed)
    prods = ["Wheat Seed", "Paddy Seed", "Urea", "DAP", "Drip Tape", "Sprayer Nozzle", "Hand Hoe", "Tarpaulin", "Gloves", "Pump Set"]
    cats = ["Seeds", "Fertiliser", "Irrigation", "Tools"]
    sup = ["Sri Agro", "Bharat Supplies", "Rayala Traders", ""]
    rows = []
    for i in range(1, n + 1):
        rows.append([
            f"{i:05d}", f"{rng.choice(prods)} {rng.choice(['A', 'B', 'C'])}", rng.choice(cats), rng.choice(["kg", "pcs", "box"]),
            str(rng.randint(0, 900)), "" if rng.random() < 0.12 else str(rng.randint(10, 120)),
            f"{rng.uniform(5, 800):.2f}", rng.choice(sup), f"R{rng.randint(1, 9)}-S{rng.randint(1, 20)}",
            f"2026-{rng.randint(1, 12):02d}-{rng.randint(13, 28):02d}",
        ])
    return rows


WRAP_HEADERS = ["Ticket", "Priority", "Owner", "Remarks"]


def wrapped_rows(n: int = 14, seed: int = 5) -> List[List[str]]:
    rng = random.Random(seed)
    remarks = ["Customer reported intermittent failures after the overnight batch update completed",
               "Replacement part ordered from the regional warehouse, delivery expected next week",
               "Issue could not be reproduced; monitoring enabled and follow-up scheduled",
               "Escalated to the vendor support team with full logs and configuration details attached"]
    return [[f"TK-{100 + i}", rng.choice(["High", "Medium", "Low"]), rng.choice(["Asha", "Ravi", "Meera"]), rng.choice(remarks)] for i in range(n)]


def make_wrapped_pdf(path: Path, rows, grid: bool) -> Path:
    styles = getSampleStyleSheet()
    cell = styles["BodyText"].clone("cell")
    cell.fontSize, cell.leading = 9, 11
    data = [WRAP_HEADERS] + [[r[0], r[1], r[2], Paragraph(r[3], cell)] for r in rows]
    t = RLTable(data, colWidths=[60, 60, 60, 180], repeatRows=1)
    st = [("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    if grid:
        st.append(("GRID", (0, 0), (-1, -1), 0.5, colors.black))
    t.setStyle(TableStyle(st))
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=40, rightMargin=40).build([t])
    return path


# --------------------------------------------------------------------------- cases

@dataclass
class Case:
    name: str
    doc_type: str
    description: str
    build: Callable[[Path], Tuple[Path, List[Tuple[List[str], List[List[str]]]]]]
    needs_ocr: bool = False
    known_weak: bool = False      # documented limitation: expected to degrade; still reported, never hidden


def _inv(rows_n=40):
    return ff.invoice_dataset(rows_n, seed=7)


def build_cases() -> List[Case]:
    cases: List[Case] = []

    def invoice_ruled(d):
        rows = _inv(); p = ff.make_table_pdf(d / "invoice_ruled.pdf", ff.INVOICE_HEADERS, rows, rows_per_page=20, grid=True,
                                             title="Tax Invoice Register (synthetic)")
        return p, [(ff.INVOICE_HEADERS, rows)]

    def invoice_unruled(d):
        rows = _inv(); p = ff.make_table_pdf(d / "invoice_unruled.pdf", ff.INVOICE_HEADERS, rows, rows_per_page=20, grid=False)
        return p, [(ff.INVOICE_HEADERS, rows)]

    def inventory_ruled(d):
        rows = inventory_rows(); p = ff.make_table_pdf(d / "inventory_ruled.pdf", INV_HEADERS, rows, rows_per_page=999, grid=True,
                                                       title="Stock Report (synthetic)")
        return p, [(INV_HEADERS, rows)]

    def bank_ruled(d):
        rows = bank_rows(); p = ff.make_table_pdf(d / "bank_ruled.pdf", BANK_HEADERS, rows, rows_per_page=999, grid=True,
                                                  repeat_header=False, landscape_page=False, font_size=9, title="Statement of Account (synthetic)")
        return p, [(BANK_HEADERS, rows)]

    def bank_unruled(d):
        rows = bank_rows(); p = ff.make_table_pdf(d / "bank_unruled.pdf", BANK_HEADERS, rows, rows_per_page=999, grid=False,
                                                  repeat_header=True, landscape_page=False, font_size=9, title="Statement of Account (synthetic)")
        return p, [(BANK_HEADERS, rows)]

    def sparse_unruled(d):
        rows = _inv(40); rng = random.Random(77)
        rows = [[c if (j == 0 or rng.random() > 0.25) else "" for j, c in enumerate(r)] for r in rows]
        p = ff.make_table_pdf(d / "sparse_unruled.pdf", ff.INVOICE_HEADERS, rows, rows_per_page=20, grid=False)
        return p, [(ff.INVOICE_HEADERS, rows)]

    def wide_16col(d):
        base = _inv(30); rng = random.Random(3)
        extra_h = ["Region", "Currency", "Approved By", "Batch"]
        headers = ff.INVOICE_HEADERS + extra_h
        rows = [r + [rng.choice(["South", "West", "North"]), "INR", rng.choice(["Asha", "Ravi", "Meera"]), f"B{rng.randint(100, 999)}"] for r in base]
        p = ff.make_table_pdf(d / "wide_16col.pdf", headers, rows, rows_per_page=999, grid=True, font_size=6.5)
        return p, [(headers, rows)]

    def wrapped_ruled(d):
        rows = wrapped_rows(); return make_wrapped_pdf(d / "wrapped_ruled.pdf", rows, True), [(WRAP_HEADERS, rows)]

    def wrapped_unruled(d):
        rows = wrapped_rows(); return make_wrapped_pdf(d / "wrapped_unruled.pdf", rows, False), [(WRAP_HEADERS, rows)]

    def purchase_order(d):
        p = ff.make_table_pdf(d / "purchase_order.pdf", PO_ITEM_HEADERS, PO_ITEMS, rows_per_page=50, grid=True, landscape_page=False,
                              font_size=10, title="Purchase Order PO-2026-0457 (synthetic)",
                              extra_tables=[(PO_SCHED_HEADERS, PO_SCHED, "Delivery Schedule")])
        return p, [(PO_ITEM_HEADERS, PO_ITEMS), (PO_SCHED_HEADERS, PO_SCHED)]

    def invoice_scan(d):
        rows = _inv(40); src = ff.make_table_pdf(d / "_inv_src.pdf", ff.INVOICE_HEADERS, rows, rows_per_page=20, grid=True)
        p = ff.make_scanned_pdf(src, d / "invoice_scanned.pdf", dpi=220)
        src.unlink()
        return p, [(ff.INVOICE_HEADERS, rows)]

    def bank_scan_skew(d):
        rows = bank_rows(30); src = ff.make_table_pdf(d / "_bank_src.pdf", BANK_HEADERS, rows, rows_per_page=999, grid=True,
                                                      landscape_page=False, font_size=9)
        p = ff.make_scanned_pdf(src, d / "bank_scanned_skewed.pdf", dpi=220, rotate_deg=1.0, noise=10.0)
        src.unlink()
        return p, [(BANK_HEADERS, rows)]

    def bank_scan_degraded(d):
        rows = bank_rows(30); src = ff.make_table_pdf(d / "_bankd_src.pdf", BANK_HEADERS, rows, rows_per_page=999, grid=True,
                                                      landscape_page=False, font_size=9)
        p = ff.make_scanned_pdf(src, d / "bank_scanned_degraded.pdf", dpi=130, rotate_deg=-1.5, noise=28.0, blur=1.0)
        src.unlink()
        return p, [(BANK_HEADERS, rows)]

    def mixed_doc(d):
        rows = _inv(20)
        a = ff.make_table_pdf(d / "_m1.pdf", ff.INVOICE_HEADERS, rows[:10], rows_per_page=999, grid=True)
        b0 = ff.make_table_pdf(d / "_m2.pdf", ff.INVOICE_HEADERS, rows[10:], rows_per_page=999, grid=True)
        b = ff.make_scanned_pdf(b0, d / "_m2s.pdf", dpi=220)
        p = ff.combine_pdfs([a, b], d / "mixed_digital_scanned.pdf")
        for t in (a, b0, b):
            t.unlink()
        return p, [(ff.INVOICE_HEADERS, rows)]

    cases += [
        Case("invoice_ruled", "digital_ruled", "12-col invoice register, ruled, header repeated, title + footer", invoice_ruled),
        Case("inventory_ruled", "digital_ruled", "10-col stock report, ruled, SKU leading zeros, blank cells, 3+ pages", inventory_ruled),
        Case("bank_ruled", "digital_ruled", "bank statement, ruled, header only on page 1, blank debit/credit cells", bank_ruled),
        Case("invoice_unruled", "digital_unruled", "12-col invoice, NO ruling lines", invoice_unruled),
        Case("bank_unruled", "digital_unruled", "bank statement, no ruling lines, header repeated", bank_unruled),
        Case("sparse_unruled", "digital_unruled", "12-col invoice, no ruling lines, ~25% of cells blank", sparse_unruled),
        Case("wide_16col", "digital_ruled", "16-column ruled table at 6.5 pt", wide_16col),
        Case("wrapped_ruled", "digital_wrapped_text", "ruled table, remarks wrap over 2-3 lines", wrapped_ruled),
        Case("wrapped_unruled", "digital_wrapped_text", "UN-ruled table, remarks wrap over 2-3 lines (documented weak spot)", wrapped_unruled, known_weak=True),
        Case("purchase_order", "multi_table", "two independent tables (line items with total row; delivery schedule)", purchase_order),
        Case("invoice_scanned", "scanned", "12-col invoice, rasterised at 220 dpi, no text layer", invoice_scan, needs_ocr=True),
        Case("bank_scanned_skewed", "scanned", "bank statement scan, 1 degree skew + noise", bank_scan_skew, needs_ocr=True),
        Case("bank_scanned_degraded", "scanned_low_quality", "bank statement scan, 130 dpi, -1.5 degree skew, heavy noise + blur", bank_scan_degraded, needs_ocr=True, known_weak=True),
        Case("mixed_digital_scanned", "mixed", "page 1 digital, page 2 scanned; one table spans both", mixed_doc, needs_ocr=True),
    ]
    return cases


# --------------------------------------------------------------------------- metrics

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _num_eq(a: str, b: str) -> bool:
    x, y = parse_number(a), parse_number(b)
    return x is not None and y is not None and abs(x - y) < 1e-9


@dataclass
class TableScore:
    expected_rows: int
    got_rows: int
    matched_rows: int
    exact_rows: int
    cells_total: int
    cells_exact: int
    numeric_total: int
    numeric_exact: int
    columns_total: int
    columns_aligned: int
    header_exact: bool
    dup_header_rows: int
    extra_rows: int


def score_table(exp_headers: Sequence[str], exp_rows: Sequence[Sequence[str]], got_headers: Sequence[str], got_rows: Sequence[Sequence[str]]) -> TableScore:
    """Ordered greedy alignment of expected rows to extracted rows (window of 8 after the last match).

    A row counts as *matched* when at least half of its cells equal the expected cells at the same
    column index. Unmatched expected rows contribute zero correct cells.
    """
    ncols = len(exp_headers)
    last = -1
    matches: List[Optional[int]] = []
    for e in exp_rows:
        best, best_hits = None, -1
        for gi in range(last + 1, min(len(got_rows), last + 9)):
            g = got_rows[gi]
            hits = sum(1 for i, c in enumerate(e) if i < len(g) and norm(g[i]) == norm(c))
            if hits > best_hits:
                best, best_hits = gi, hits
        if best is not None and best_hits * 2 >= ncols:
            matches.append(best); last = best
        else:
            matches.append(None)
    cells_exact = numeric_total = numeric_exact = exact_rows = 0
    col_hits = [0] * ncols
    matched = 0
    for e, gi in zip(exp_rows, matches):
        for i, c in enumerate(e):
            is_num = parse_number(c) is not None
            numeric_total += is_num
            if gi is not None:
                g = got_rows[gi]
                gc = g[i] if i < len(g) else ""
                if norm(gc) == norm(c):
                    cells_exact += 1; col_hits[i] += 1
                if is_num and _num_eq(gc, c):
                    numeric_exact += 1
        if gi is not None:
            matched += 1
            g = got_rows[gi]
            if len(g) == ncols and all(norm(g[i]) == norm(e[i]) for i in range(ncols)):
                exact_rows += 1
    aligned = sum(1 for h in col_hits if matched and h / matched >= 0.9)
    hdr = [norm(h).lower() for h in exp_headers]
    dup = sum(1 for g in got_rows if [norm(c).lower() for c in g] == hdr)
    return TableScore(
        expected_rows=len(exp_rows), got_rows=len(got_rows), matched_rows=matched, exact_rows=exact_rows,
        cells_total=len(exp_rows) * ncols, cells_exact=cells_exact, numeric_total=numeric_total, numeric_exact=numeric_exact,
        columns_total=ncols, columns_aligned=aligned,
        header_exact=[norm(h) for h in got_headers] == [norm(h) for h in exp_headers],
        dup_header_rows=dup, extra_rows=max(0, len(got_rows) - matched),
    )


def aggregate(scores: Sequence[TableScore]) -> Dict[str, float]:
    """Micro-averages over tables (sum of hits / sum of totals)."""
    def ratio(a, b): return round(a / b, 4) if b else None
    s = scores
    return {
        "exact_cell_accuracy": ratio(sum(x.cells_exact for x in s), sum(x.cells_total for x in s)),
        "numeric_cell_accuracy": ratio(sum(x.numeric_exact for x in s), sum(x.numeric_total for x in s)),
        "row_completeness": ratio(sum(x.matched_rows for x in s), sum(x.expected_rows for x in s)),
        "exact_row_rate": ratio(sum(x.exact_rows for x in s), sum(x.expected_rows for x in s)),
        "column_alignment": ratio(sum(x.columns_aligned for x in s), sum(x.columns_total for x in s)),
        "header_exact_rate": ratio(sum(1 for x in s if x.header_exact), len(s)),
        "spurious_rows": sum(x.extra_rows for x in s),
        "duplicate_header_rows_leaked": sum(x.dup_header_rows for x in s),
    }


def evaluate_extraction(result, expected_tables) -> Tuple[List[TableScore], Dict]:
    """Match expected tables to extracted tables in order; missing tables score zero."""
    scores: List[TableScore] = []
    for k, (eh, er) in enumerate(expected_tables):
        if k < len(result.tables):
            t = result.tables[k]
            scores.append(score_table(eh, er, t.headers, t.rows))
        else:
            scores.append(score_table(eh, er, [], []))
    return scores, {"expected_tables": len(expected_tables), "extracted_tables": len(result.tables),
                    "table_count_correct": len(expected_tables) == len(result.tables)}


# --------------------------------------------------------------------------- labelled validation cases

def _clean_table(seed: int):
    from conftest import make_table
    rows = [[f"Item {i}", str(rng_i), f"{(100 + i * 7 + (seed % 5)):.2f}", f"2026-{(i % 12) + 1:02d}-{(i % 15) + 13:02d}"]
            for i, rng_i in enumerate(range(10, 34), start=1)]
    return make_table(["Name", "Qty", "Amount", "Date"], rows)


def labelled_validation_cases() -> List[Tuple[object, set]]:
    """(table, expected {(check, 1-based row)}) pairs with injected anomalies; row 0 means table-level."""
    out = []
    t = _clean_table(1); t.rows[5] = t.rows[4][:]; out.append((t, {("duplicate_record", 6)}))
    t = _clean_table(2); t.rows[9][2] = ""; out.append((t, {("missing_value", 10)}))
    t = _clean_table(3); t.rows[7][2] = "1O7.5x"; out.append((t, {("numeric_parse", 8)}))
    t = _clean_table(4); t.rows[12][2] = "987654.00"; out.append((t, {("suspicious_value", 13)}))
    t = _clean_table(5); t.rows[3][3] = "2026-02-31"; out.append((t, {("invalid_date", 4)}))
    t = _clean_table(6); t.rows[15][1] = "Pending"; out.append((t, {("column_misalignment", 16)}))
    t = _clean_table(7)
    total = sum(float(r[2]) for r in t.rows)
    t.rows.append(["Total", "", f"{total + 50:.2f}", ""]); t.row_pages.append(1); t.row_methods.append("pdfplumber_lines")
    t.row_conf.append(None); t.row_y.append(None)
    out.append((t, {("total_mismatch", len(t.rows))}))
    t = _clean_table(8)
    total = sum(float(r[2]) for r in t.rows)
    t.rows.append(["Total", "", f"{total:.2f}", ""]); t.row_pages.append(1); t.row_methods.append("pdfplumber_lines")
    t.row_conf.append(None); t.row_y.append(None)
    out.append((t, set()))                      # correct total -> nothing expected
    for s in (9, 10):
        out.append((_clean_table(s), set()))   # clean tables -> nothing expected
    return out


LABELLED_CHECKS = {"duplicate_record", "missing_value", "numeric_parse", "suspicious_value", "invalid_date",
                   "column_misalignment", "total_mismatch"}


def score_validation(validate_fn) -> Dict:
    tp = fp = fn = 0
    detail = []
    for table, expected in labelled_validation_cases():
        issues = validate_fn(table)
        found = {(i.check, i.row or 0) for i in issues if i.check in LABELLED_CHECKS}
        tp += len(found & expected); fp += len(found - expected); fn += len(expected - found)
        detail.append({"expected": sorted(expected), "found": sorted(found)})
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {"true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": None if prec is None else round(prec, 4), "recall": None if rec is None else round(rec, 4),
            "cases": len(detail), "detail": detail}
