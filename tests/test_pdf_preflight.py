import dataclasses

import pytest

import fixture_factory as ff
from config.settings import HARD_MAX_PAGES, SETTINGS
from src.pdf_preflight import PreflightError, run_preflight


def test_valid_pdf_passes(invoice_pdf):
    r = run_preflight(invoice_pdf)
    assert r.page_count >= 2
    assert len(r.sha256) == 64
    assert r.size_bytes > 0


def test_corrupt_pdf_rejected(pdf_dir):
    p = ff.make_corrupt_pdf(pdf_dir / "corrupt.pdf")
    with pytest.raises(PreflightError) as e:
        run_preflight(p)
    assert e.value.code in {"corrupt", "no_pages"}
    assert "PDF" in e.value.user_message


def test_non_pdf_signature_rejected(pdf_dir):
    p = ff.make_not_pdf(pdf_dir / "fake.pdf")
    with pytest.raises(PreflightError) as e:
        run_preflight(p)
    assert e.value.code == "not_a_pdf"


def test_empty_file_rejected(pdf_dir):
    p = pdf_dir / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(PreflightError) as e:
        run_preflight(p)
    assert e.value.code == "empty_file"


def test_missing_file_rejected(pdf_dir):
    with pytest.raises(PreflightError) as e:
        run_preflight(pdf_dir / "does_not_exist.pdf")
    assert e.value.code == "not_found"


def test_encrypted_pdf_rejected(pdf_dir, small_digital_pdf):
    p = ff.make_encrypted_pdf(small_digital_pdf, pdf_dir / "enc.pdf")
    with pytest.raises(PreflightError) as e:
        run_preflight(p)
    assert e.value.code == "encrypted"
    assert "password" in e.value.user_message.lower()


def test_more_than_50_pages_rejected(pdf_dir):
    p = ff.make_many_pages_pdf(pdf_dir / "p51.pdf", 51)
    with pytest.raises(PreflightError) as e:
        run_preflight(p)
    assert e.value.code == "too_many_pages"
    assert "50" in e.value.user_message


def test_exactly_50_pages_accepted(pdf_dir):
    p = ff.make_many_pages_pdf(pdf_dir / "p50.pdf", 50)
    assert run_preflight(p).page_count == 50


def test_page_limit_can_be_lowered_but_never_raised():
    assert dataclasses.replace(SETTINGS, max_pages=10).max_pages == 10
    assert dataclasses.replace(SETTINGS, max_pages=500).max_pages == HARD_MAX_PAGES


def test_file_size_limit(pdf_dir, small_digital_pdf):
    s = dataclasses.replace(SETTINGS, max_file_mb=1)
    big = pdf_dir / "big.pdf"
    big.write_bytes(small_digital_pdf.read_bytes() + b"\n%" + b"x" * (2 * 1024 * 1024))
    with pytest.raises(PreflightError) as e:
        run_preflight(big, s)
    assert e.value.code == "too_large"
