import dataclasses
import re

import pytest

import fixture_factory as ff
from conftest import requires_ocr
from src import ocr_engine
from src.document_classifier import classify_document, classify_page
from src.extraction_agent import ExtractionAgent, plan_for, score_tables
from src.models import PageMetrics, RawTable


def _cell_accuracy(table, expected_rows):
    got = 0
    for a, b in zip(table.rows, expected_rows):
        got += sum(1 for x, y in zip(a, b) if x == y)
    return got / (len(expected_rows) * len(expected_rows[0]))


# ------------------------------------------------------------------ digital

def test_digital_ruled_table_exact(invoice_pdf, invoice_rows, settings):
    r = ExtractionAgent(settings).run(invoice_pdf)
    assert r.status == "Complete"
    assert len(r.tables) == 1
    t = r.tables[0]
    assert t.headers == ff.INVOICE_HEADERS
    assert t.n_cols >= 10          # 10+ columns requirement
    assert t.rows == invoice_rows  # every cell exact
    assert t.row_pages[0] == 1 and t.row_pages[-1] == r.total_pages
    assert all(p.kind == "digital" for p in r.page_results)


def test_unruled_table_uses_geometry_and_is_exact(invoice_unruled_pdf, invoice_rows, settings):
    r = ExtractionAgent(settings).run(invoice_unruled_pdf)
    t = r.tables[0]
    assert t.headers == ff.INVOICE_HEADERS and t.rows == invoice_rows
    assert {p.method for p in r.page_results} <= {"words_geometry", "pdfplumber_text"}


def test_multipage_table_headerless_continuation(invoice_noheader_pdf, invoice_rows, settings):
    r = ExtractionAgent(settings).run(invoice_noheader_pdf)
    assert len(r.tables) == 1
    assert r.tables[0].rows == invoice_rows
    assert any("headerless continuation merged" in n for n in r.tables[0].notes)


def test_repeated_headers_removed(invoice_pdf, settings):
    r = ExtractionAgent(settings).run(invoice_pdf)
    t = r.tables[0]
    assert all([c.lower() for c in row] != [h.lower() for h in t.headers] for row in t.rows)
    assert any("repeated header" in n for n in t.notes)


def test_prose_page_yields_no_tables_honestly(prose_pdf, settings):
    r = ExtractionAgent(settings).run(prose_pdf)
    assert r.status == "No tables found"
    assert r.tables == [] and r.n_records == 0
    assert all(p.status == "no_tables" for p in r.page_results)


def test_blank_page_marked_blank(pdf_dir, settings):
    p = ff.make_blank_pdf(pdf_dir / "blank.pdf", 2)
    r = ExtractionAgent(settings).run(p)
    assert [pg.status for pg in r.page_results] == ["blank", "blank"]
    assert r.status == "No tables found"


def test_missing_values_preserved_as_blank(pdf_dir, settings):
    rows = ff.invoice_dataset(12, seed=5)
    rows[4][2] = ""
    rows[7][10] = ""
    p = ff.make_table_pdf(pdf_dir / "missing.pdf", ff.INVOICE_HEADERS, rows, rows_per_page=30)
    r = ExtractionAgent(settings).run(p)
    assert r.tables[0].rows[4][2] == "" and r.tables[0].rows[7][10] == ""
    assert r.tables[0].rows == rows  # nothing invented


def test_leading_zero_identifiers_not_converted(pdf_dir, settings):
    h, rows = ff.simple_dataset(10)
    p = ff.make_table_pdf(pdf_dir / "codes.pdf", h, rows, landscape_page=False, font_size=10)
    r = ExtractionAgent(settings).run(p)
    assert [row[0] for row in r.tables[0].rows][:3] == ["001", "002", "003"]


def test_multiple_independent_tables(pdf_dir, settings):
    h1, r1 = ff.simple_dataset(8)
    h2 = ["Region", "Target", "Actual"]
    r2 = [[f"Zone {i}", str(100 * i), str(95 * i)] for i in range(1, 8)]
    p = ff.make_table_pdf(pdf_dir / "two.pdf", h1, r1, landscape_page=False, font_size=10,
                          extra_tables=[(h2, r2, "Regional totals")])
    r = ExtractionAgent(settings).run(p)
    assert len(r.tables) == 2
    assert r.tables[0].headers == h1 and r.tables[1].headers == h2
    assert r.tables[0].sheet_name != r.tables[1].sheet_name


# ------------------------------------------------------------------ classification / planning / scoring

def test_classify_page_rules():
    m = lambda **k: PageMetrics(page=1, **k)
    assert classify_page(m(n_chars=500)) == "digital"
    assert classify_page(m(n_chars=500, n_images=1, image_coverage=0.9)) == "mixed"
    assert classify_page(m(n_chars=0, n_images=1, image_coverage=0.95)) == "scanned"
    assert classify_page(m(n_chars=0)) == "blank"
    assert classify_page(m(n_chars=300, garbled_ratio=0.6)) == "unreliable_text"
    assert classify_page(m(n_chars=0, n_drawings=500)) == "scanned"


def test_classify_document():
    assert classify_document(["digital", "digital"]) == "digital"
    assert classify_document(["scanned", "scanned"]) == "scanned"
    assert classify_document(["digital", "scanned"]) == "mixed"
    assert classify_document(["blank"]) == "blank"


def test_plans_are_per_kind_and_bounded():
    assert plan_for("blank") == []
    assert plan_for("scanned")[0].startswith("ocr")
    assert plan_for("digital")[0] == "pdfplumber_lines"
    assert not any(s.startswith("ocr") for s in plan_for("digital", 0.0))
    for kind in ("digital", "mixed", "scanned", "unreliable_text"):
        assert len(plan_for(kind, 0.5)) <= 5


def test_score_is_heuristic_in_unit_range_and_penalises_sparse_tables():
    good = RawTable(1, 0, [["A", "B", "C"]] + [[str(i), "x", "y"] for i in range(8)], "m")
    sparse = RawTable(1, 0, [["", "", "t"], ["", "b", ""], ["", "", ""], ["a", "", ""], ["", "", "z"], ["", "q", ""]], "m")
    assert 0.8 <= score_tables([good]) <= 1.0
    assert score_tables([sparse]) < 0.3
    assert score_tables([]) == 0.0
    assert score_tables([good], mean_conf=30) < score_tables([good], mean_conf=95)


def test_attempts_are_bounded(invoice_pdf, settings):
    s = dataclasses.replace(settings, quality_accept=1.01, max_attempts_per_page=2)  # never "good enough"
    r = ExtractionAgent(s).run(invoice_pdf)
    for p in r.page_results:
        assert len([a for a in p.attempts if "unavailable" not in a.reason]) <= 2
        assert p.decision_log, "decisions must be logged"


def test_decision_log_and_provenance_recorded(invoice_pdf, settings):
    r = ExtractionAgent(settings).run(invoice_pdf)
    p = r.page_results[0]
    assert p.metrics is not None and p.plan and p.attempts and p.method
    log = r.to_log_dict()
    assert log["pages"][0]["attempts"][0]["strategy"] == p.attempts[0].strategy
    assert "heuristic_score" in log["pages"][0]
    # the log must not contain cell values (privacy)
    assert "Kiran" not in str(log) and "INV-1001" not in str(log)


# ------------------------------------------------------------------ OCR

@requires_ocr
def test_scanned_pdf_ocr(small_scanned_pdf, small_rows, settings):
    r = ExtractionAgent(settings).run(small_scanned_pdf)
    assert r.page_results[0].kind == "scanned" and r.page_results[0].ocr_used
    t = r.tables[0]
    assert t.n_cols == 12 and t.n_rows == len(small_rows)
    assert _cell_accuracy(t, small_rows) >= 0.90  # OCR is imperfect; this is a floor, not a promise
    assert any(i.check == "ocr_unverified" for i in r.issues)  # OCR output never silently "verified"
    assert all(m.startswith("ocr") for m in t.row_methods)


@requires_ocr
def test_mixed_document_digital_plus_scanned(pdf_dir, small_digital_pdf, small_scanned_pdf, small_rows, settings):
    mixed = ff.combine_pdfs([small_digital_pdf, small_scanned_pdf], pdf_dir / "mixed.pdf")
    r = ExtractionAgent(settings).run(mixed)
    assert [p.kind for p in r.page_results] == ["digital", "scanned"]
    assert r.doc_kind == "mixed"
    assert len(r.tables) == 1, "OCR page must continue the digital table"
    t = r.tables[0]
    assert t.n_rows == 2 * len(small_rows)
    assert t.row_methods[0] == "pdfplumber_lines" and t.row_methods[-1].startswith("ocr")
    assert t.rows[: len(small_rows)] == small_rows
    assert _cell_accuracy(type("T", (), {"rows": t.rows[len(small_rows):]}), small_rows) >= 0.90


def test_missing_tesseract_is_reported_not_silent(small_scanned_pdf, small_digital_pdf, pdf_dir, settings):
    ocr_engine.reset_cache()
    s = dataclasses.replace(settings, tesseract_cmd="/definitely/not/here/tesseract")
    try:
        r = ExtractionAgent(s).run(small_scanned_pdf)
        assert r.ocr_available is False
        assert r.page_results[0].status == "failed"
        assert "Tesseract" in r.page_results[0].failure_reason
        assert r.status == "Failed" and r.tables == []
        assert any(i.check == "failed_page" and i.severity == "Error" for i in r.issues)
        assert any("OCR is unavailable" in w for w in r.warnings)

        # digital PDFs still work, as documented
        r2 = ExtractionAgent(s).run(small_digital_pdf)
        assert r2.status == "Complete"

        # mixed document -> Partial, never "Complete"
        mixed = ff.combine_pdfs([small_digital_pdf, small_scanned_pdf], pdf_dir / "mixed_noocr.pdf")
        r3 = ExtractionAgent(s).run(mixed)
        assert r3.status == "Partial" and r3.is_partial
    finally:
        ocr_engine.reset_cache()


def test_ocr_engine_reports_install_help_when_missing(settings):
    ocr_engine.reset_cache()
    s = dataclasses.replace(settings, tesseract_cmd="/nope/tesseract")
    try:
        st = ocr_engine.get_status(s, use_cache=False)
        assert not st.available and "github.com/UB-Mannheim" in st.message
        with pytest.raises(ocr_engine.OCRUnavailableError):
            ocr_engine.require_ocr(s)
    finally:
        ocr_engine.reset_cache()


def test_preprocess_variants_return_images():
    from PIL import Image
    img = Image.new("RGB", (400, 300), "white")
    for v in ("gray", "clean", "upscaled"):
        out = ocr_engine.preprocess(img, v)
        assert out.size[0] >= 400
    with pytest.raises(ValueError):
        ocr_engine.preprocess(img, "bogus")


def test_rule_removal_erases_borders_but_keeps_text_like_blobs():
    import numpy as np
    from PIL import Image, ImageDraw
    img = Image.new("L", (900, 600), 255)
    d = ImageDraw.Draw(img)
    d.line([(50, 100), (850, 100)], fill=0, width=3)      # horizontal rule
    d.line([(300, 50), (300, 550)], fill=0, width=3)      # vertical rule
    d.rectangle([500, 300, 530, 330], fill=0)             # text-sized blob (30px) must survive
    out = np.asarray(ocr_engine.preprocess(img.convert("RGB"), "clean"))
    assert out[100, 600] == 255 and out[300, 300] == 255   # rules gone
    assert out[315, 515] == 0                              # blob kept
