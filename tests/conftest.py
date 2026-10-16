"""Shared pytest fixtures. All PDFs are generated programmatically (synthetic)."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import fixture_factory as ff  # noqa: E402
from config.settings import SETTINGS  # noqa: E402
from src import ocr_engine  # noqa: E402
from src.models import PageResult, RawTable, Table  # noqa: E402

OCR_AVAILABLE = ocr_engine.get_status(SETTINGS, use_cache=False).available
requires_ocr = pytest.mark.skipif(not OCR_AVAILABLE, reason="Tesseract executable not installed")


@pytest.fixture()
def settings(tmp_path):
    """A private Settings copy writing outputs to tmp_path."""
    out = tmp_path / "outputs"
    out.mkdir()
    return dataclasses.replace(SETTINGS, output_dir=out, reports_dir=tmp_path / "reports")


@pytest.fixture(scope="session")
def pdf_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("pdfs")


@pytest.fixture(scope="session")
def invoice_rows():
    return ff.invoice_dataset(40)


@pytest.fixture(scope="session")
def invoice_pdf(pdf_dir, invoice_rows):
    """Digital, ruled, 12 columns, header repeated on each page."""
    return ff.make_table_pdf(pdf_dir / "invoice_ruled.pdf", ff.INVOICE_HEADERS, invoice_rows, rows_per_page=20, grid=True)


@pytest.fixture(scope="session")
def invoice_unruled_pdf(pdf_dir, invoice_rows):
    return ff.make_table_pdf(pdf_dir / "invoice_unruled.pdf", ff.INVOICE_HEADERS, invoice_rows, rows_per_page=20, grid=False)


@pytest.fixture(scope="session")
def invoice_noheader_pdf(pdf_dir, invoice_rows):
    """Header only on page 1 (continuation pages are headerless)."""
    return ff.make_table_pdf(pdf_dir / "invoice_noheader.pdf", ff.INVOICE_HEADERS, invoice_rows, rows_per_page=20,
                             grid=True, repeat_header=False)


@pytest.fixture(scope="session")
def prose_pdf(pdf_dir):
    return ff.make_prose_pdf(pdf_dir / "prose.pdf")


@pytest.fixture(scope="session")
def small_rows():
    return ff.invoice_dataset(10, seed=3)


@pytest.fixture(scope="session")
def small_digital_pdf(pdf_dir, small_rows):
    return ff.make_table_pdf(pdf_dir / "small_digital.pdf", ff.INVOICE_HEADERS, small_rows, rows_per_page=30, grid=True)


@pytest.fixture(scope="session")
def small_scanned_pdf(pdf_dir, small_digital_pdf):
    return ff.make_scanned_pdf(small_digital_pdf, pdf_dir / "small_scanned.pdf", dpi=220)


def make_raw(page, rows, y0=50.0, y1=700.0, page_height=800.0, method="pdfplumber_lines", conf=None):
    ys = [(y0 + i * 10.0, y0 + i * 10.0 + 9.0) for i in range(len(rows))]
    return RawTable(page, 0, [list(r) for r in rows], method, bbox=(10.0, y0, 500.0, y1), page_height=page_height,
                    row_conf=conf or [], row_y=ys)


def make_page(page, rows=None, **kw):
    pr = PageResult(page=page, status="ok")
    if rows is not None:
        pr.tables = [make_raw(page, rows, **kw)]
    return pr


def make_table(headers, rows, pages=None, methods=None, conf=None, tid="T1"):
    n = len(rows)
    return Table(
        table_id=tid, headers=list(headers), rows=[list(r) for r in rows],
        row_pages=list(pages or [1] * n), row_methods=list(methods or ["pdfplumber_lines"] * n),
        row_conf=list(conf or [None] * n), row_y=[None] * n, pages=sorted(set(pages or [1])),
        sheet_name="Extracted_Data",
    )
