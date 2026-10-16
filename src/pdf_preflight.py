"""Preflight checks run before any extraction work.

Treats the upload as untrusted: validates signature, size, structure, encryption and page count.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Union

from config.settings import SETTINGS, Settings
from src.file_manager import sha256_file

try:
    import pymupdf
except ImportError:  # pragma: no cover - very old PyMuPDF
    import fitz as pymupdf  # type: ignore

log = logging.getLogger(__name__)


class PreflightError(Exception):
    """Raised when a file must be rejected. `user_message` is safe and friendly to show."""

    def __init__(self, code: str, user_message: str):
        super().__init__(f"{code}: {user_message}")
        self.code = code
        self.user_message = user_message


@dataclass
class PreflightResult:
    path: Path
    size_bytes: int
    sha256: str
    page_count: int
    warnings: List[str] = field(default_factory=list)


def run_preflight(path: Union[str, Path], settings: Settings = SETTINGS) -> PreflightResult:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise PreflightError("not_found", "The uploaded file could not be found. Please upload it again.")

    size = p.stat().st_size
    if size == 0:
        raise PreflightError("empty_file", "The file is empty (0 bytes).")
    limit = settings.max_file_mb * 1024 * 1024
    if size > limit:
        raise PreflightError(
            "too_large",
            f"The file is {size / 1024 / 1024:.1f} MB. The limit is {settings.max_file_mb} MB. "
            "Try compressing the PDF or splitting it.",
        )

    with open(p, "rb") as fh:
        head = fh.read(1024)
    if b"%PDF-" not in head:
        raise PreflightError(
            "not_a_pdf",
            "This does not look like a PDF file (missing the PDF signature). Please upload a .pdf document.",
        )

    warnings: List[str] = []
    if p.suffix.lower() != ".pdf":
        warnings.append("The file does not have a .pdf extension, but its content looks like a PDF.")

    try:
        doc = pymupdf.open(str(p))
    except Exception as exc:
        log.info("Could not open PDF: %s", exc)
        raise PreflightError(
            "corrupt",
            "The PDF appears to be damaged and could not be opened. Try re-exporting or re-downloading it.",
        ) from exc

    try:
        if doc.needs_pass:
            raise PreflightError(
                "encrypted",
                "This PDF is password-protected. Remove the password (or print it to a new PDF) and upload it again.",
            )
        if doc.is_encrypted:
            warnings.append("The PDF has encryption/permission flags but opens without a password.")
        try:
            count = doc.page_count
        except Exception as exc:
            raise PreflightError("corrupt", "The PDF page list could not be read; the file may be damaged.") from exc
        if count <= 0:
            raise PreflightError(
                "no_pages",
                "No readable pages were found. The PDF may be empty or damaged. Try re-exporting or re-downloading it.",
            )
        if count > settings.max_pages:
            raise PreflightError(
                "too_many_pages",
                f"The PDF has {count} pages. The maximum supported is {settings.max_pages} pages. "
                "Please split the document and convert the parts separately.",
            )
        if getattr(doc, "is_repaired", False):
            warnings.append("The PDF structure was damaged and had to be repaired; some content may be missing.")
        # Probe the first page so grossly broken files fail early with a clear message.
        try:
            doc.load_page(0)
        except Exception as exc:
            raise PreflightError("corrupt", "The first page of the PDF could not be read; the file may be damaged.") from exc
    finally:
        doc.close()

    return PreflightResult(path=p, size_bytes=size, sha256=sha256_file(p), page_count=count, warnings=warnings)
