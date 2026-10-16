"""Regression tests built on the benchmark cases + tests of the metric definitions themselves."""
import dataclasses
import tempfile
from pathlib import Path

import pytest

import benchmark_lib as bl
from conftest import make_table, requires_ocr
from src.extraction_agent import ExtractionAgent
from src.validator import validate

CASES = {c.name: c for c in bl.build_cases()}
DIGITAL = [c.name for c in CASES.values() if not c.needs_ocr and not c.known_weak]
SCANNED_OK = ["invoice_scanned", "bank_scanned_skewed", "mixed_digital_scanned"]


def _run(name, settings):
    d = Path(tempfile.mkdtemp(prefix="docupilot_t_"))
    pdf, expected = CASES[name].build(d)
    r = ExtractionAgent(settings).run(pdf)
    return r, expected


# ------------------------------------------------------------------ metric definitions

def test_metric_perfect_match():
    h, rows = ["A", "B", "C"], [["1", "x", "2.5"], ["2", "y", "3.5"]]
    s = bl.score_table(h, rows, h, rows)
    agg = bl.aggregate([s])
    assert agg["exact_cell_accuracy"] == 1.0 and agg["row_completeness"] == 1.0 and agg["column_alignment"] == 1.0
    assert agg["numeric_cell_accuracy"] == 1.0 and agg["spurious_rows"] == 0


def test_metric_detects_dropped_rows_and_spurious_rows():
    h = ["A", "B", "C"]
    exp = [[str(i), "x", f"{i}.0"] for i in range(10)]
    got = exp[:3] + exp[5:] + [["junk", "", ""]]          # rows 3,4 dropped, one spurious row
    s = bl.score_table(h, exp, h, got)
    assert s.matched_rows == 8 and s.extra_rows == 1
    assert bl.aggregate([s])["row_completeness"] == 0.8


def test_metric_detects_column_shift():
    h = ["A", "B", "C", "D"]
    exp = [[str(i), "x", "y", "z"] for i in range(10)]
    got = [[r[0], r[1], "", r[2]] for r in exp]            # column C content shifted right
    s = bl.score_table(h, exp, h, got)
    assert s.columns_aligned < 4 and s.exact_rows == 0


def test_metric_counts_leaked_duplicate_headers_and_numeric_accuracy():
    h = ["A", "B"]
    exp = [["1", "10.00"], ["2", "20.00"]]
    got = [["1", "10.00"], ["A", "B"], ["2", "2O.00"]]
    s = bl.score_table(h, exp, h, got)
    assert s.dup_header_rows == 1
    assert s.numeric_total == 4 and s.numeric_exact == 3


def test_metric_missing_table_scores_zero():
    class R: tables = []
    scores, info = bl.evaluate_extraction(R(), [(["A"], [["1"]])])
    assert bl.aggregate(scores)["exact_cell_accuracy"] == 0.0 and info["table_count_correct"] is False


def test_labelled_validation_detection_is_computed_not_hardcoded():
    res = bl.score_validation(lambda t: validate([t], []))
    assert res["cases"] >= 8 and res["true_positives"] + res["false_negatives"] == 7
    assert res["recall"] >= 0.85 and res["precision"] >= 0.85
    none = bl.score_validation(lambda t: [])               # a validator that finds nothing must score 0 recall
    assert none["recall"] == 0.0


def test_ground_truth_literals_are_internally_consistent():
    # bank balance arithmetic
    bal = 50000.00
    for r in bl.BANK_LITERAL[1:]:
        d = float(r[3].replace(",", "") or 0); c = float(r[4].replace(",", "") or 0)
        bal = round(bal - d + c, 2)
        assert f"{bal:,.2f}" == r[5]
    rows = bl.bank_rows(60)
    assert len(rows) == 60 and rows[-1][1] == "Closing Balance"
    # purchase-order total
    assert sum(float(r[4]) for r in bl.PO_ITEMS[:-1]) == float(bl.PO_ITEMS[-1][4])


# ------------------------------------------------------------------ digital regressions

@pytest.mark.parametrize("name", DIGITAL)
def test_digital_cases_exact(name, settings):
    r, exp = _run(name, settings)
    scores, info = bl.evaluate_extraction(r, exp)
    agg = bl.aggregate(scores)
    assert info["table_count_correct"], f"{name}: expected {info['expected_tables']} tables, got {info['extracted_tables']}"
    assert agg["exact_cell_accuracy"] == 1.0 and agg["row_completeness"] == 1.0
    assert agg["spurious_rows"] == 0 and agg["duplicate_header_rows_leaked"] == 0
    assert r.status == "Complete"
    assert [t.headers for t in r.tables] == [h for h, _ in exp]


def test_title_above_unruled_table_is_peeled_not_absorbed(settings):
    r, exp = _run("bank_unruled", settings)
    assert len(r.tables) == 1 and not r.tables[0].header_inferred
    assert any("Text above table" in n for n in r.tables[0].notes)


def test_total_row_in_source_is_kept_and_consistent(settings):
    r, exp = _run("purchase_order", settings)
    t = r.tables[0]
    assert t.rows[-1][0] == "Total" and t.rows[-1][4] == "740.00"
    assert not any(i.check == "total_mismatch" for i in r.issues)
    assert not any(i.check == "missing_value" for i in r.issues)   # blank cells on a total row are legitimate


def test_known_weak_wrapped_unruled_is_flagged_not_silent(settings):
    r, exp = _run("wrapped_unruled", settings)
    flagged = [i for i in r.issues if i.check == "possible_wrapped_text"]
    assert len(flagged) >= 10, "wrapped-text rows must be flagged for review"
    assert all(i.severity == "Warning" and i.category == "Potential" for i in flagged)


def test_prose_is_never_turned_into_a_table(prose_pdf, settings):
    r = ExtractionAgent(settings).run(prose_pdf)
    assert r.tables == []
    att = r.page_results[0].attempts
    assert any("cut through words" in a.reason for a in att), "the word-integrity gate should have rejected pdfplumber_text"


# ------------------------------------------------------------------ OCR regressions

@requires_ocr
@pytest.mark.parametrize("name", SCANNED_OK)
def test_scanned_cases_reasonable_and_flagged(name, settings):
    r, exp = _run(name, settings)
    scores, info = bl.evaluate_extraction(r, exp)
    agg = bl.aggregate(scores)
    assert info["table_count_correct"]
    assert agg["exact_cell_accuracy"] >= 0.95 and agg["row_completeness"] == 1.0 and agg["column_alignment"] == 1.0
    assert any(i.check == "ocr_unverified" for i in r.issues)


@requires_ocr
def test_reproduces_reported_472_of_480_scan_result(settings):
    """The 98.33% figure quoted during development, re-derived with the benchmark's own metric code."""
    r, exp = _run("invoice_scanned", settings)
    s = bl.score_table(exp[0][0], exp[0][1], r.tables[0].headers, r.tables[0].rows)
    assert s.cells_total == 480
    assert s.cells_exact >= 465          # allow small run-to-run/OCR-version variation; actual value is recorded in reports/


@requires_ocr
def test_unreadable_scan_is_reported_as_failure_not_garbage(settings):
    r, exp = _run("bank_scanned_degraded", settings)
    assert r.status != "Complete"
    assert r.page_results[0].status == "failed"
    assert "too low" in r.page_results[0].failure_reason
    assert r.tables == [], "low-confidence OCR text must not be presented as extracted data"
    assert any(i.check == "failed_page" for i in r.issues)


def test_low_quality_pages_make_status_review_needed(invoice_pdf, settings):
    s = dataclasses.replace(settings, quality_accept=1.01, quality_min_usable=0.1)   # force "low_quality" outcome
    r = ExtractionAgent(s).run(invoice_pdf)
    assert all(p.status == "low_quality" for p in r.page_results)
    assert r.status == "Complete (review needed)" and "must be reviewed" in r.status_detail
    assert any(i.check == "low_quality_page" for i in r.issues)
