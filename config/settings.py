"""Central configuration for DocuPilot AI.

All limits and heuristic thresholds live here so they are visible, testable and
overridable. Environment variables can override a few deployment-sensitive values.

NOTE: the "quality" values below are *heuristic scores* in the range 0..1. They are
NOT calibrated probabilities and must never be presented as accuracy percentages.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parent.parent

# The product requirement is "up to 50 pages". Configuration may lower this, never raise it.
HARD_MAX_PAGES = 50


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


@dataclass
class Settings:
    app_name: str = "DocuPilot AI"
    subtitle: str = "Intelligent PDF-to-Excel Agent"
    tagline: str = "From documents to trusted data."

    # --- input limits -------------------------------------------------------
    max_pages: int = HARD_MAX_PAGES
    max_file_mb: int = 50

    # --- page inspection / classification ----------------------------------
    min_native_chars: int = 25          # fewer chars => page is not "digital"
    max_garbled_ratio: float = 0.20     # share of replacement/control chars => unreliable text layer
    image_coverage_mixed: float = 0.50  # page area covered by images => "mixed" if it has text

    # --- OCR ---------------------------------------------------------------
    tesseract_cmd: Optional[str] = None  # explicit path; otherwise PATH / Windows defaults
    ocr_lang: str = "eng"
    ocr_dpi: int = 300
    ocr_max_render_px: int = 6500        # cap on the longest rendered side (memory guard)
    ocr_timeout_s: int = 90              # per Tesseract call
    ocr_psm_primary: int = 6
    ocr_psm_alt: int = 4
    low_ocr_conf: float = 60.0           # Tesseract word confidence (0-100) below which rows are flagged
    ocr_unusable_conf: float = 35.0      # page mean OCR confidence below this => output discarded, page reported as failed

    # --- agent --------------------------------------------------------------
    quality_accept: float = 0.60         # heuristic score at/above which an attempt is accepted
    quality_min_usable: float = 0.30     # below this the tables are discarded as unreliable
    max_attempts_per_page: int = 4
    max_processing_seconds: int = 900    # total wall-clock budget for one document

    # --- table geometry heuristics -----------------------------------------
    gap_factor: float = 0.6              # column gap threshold = gap_factor * median word height
    row_tolerance: float = 0.6           # same text line if centres within row_tolerance * median height
    continuation_bottom_ratio: float = 0.55  # prev table must end below this fraction of page height
    continuation_top_ratio: float = 0.45     # next table must start above this fraction of page height
    header_similarity_merge: float = 0.80    # cell-wise similarity to treat a header as "repeated"
    header_similarity_possible: float = 0.50 # between this and merge threshold => keep separate + flag

    # --- validation ---------------------------------------------------------
    max_issues_per_check: int = 100
    outlier_iqr_factor: float = 3.0
    total_tolerance_abs: float = 0.02

    # --- output / privacy ---------------------------------------------------
    output_dir: Path = ROOT_DIR / "outputs"
    reports_dir: Path = ROOT_DIR / "reports"
    output_retention_hours: int = 24
    max_cell_chars: int = 32000          # Excel hard limit is 32,767

    def __post_init__(self) -> None:
        self.max_pages = max(1, min(int(self.max_pages), HARD_MAX_PAGES))
        self.output_dir = Path(self.output_dir)
        self.reports_dir = Path(self.reports_dir)


def load_settings() -> Settings:
    """Build settings, applying optional environment overrides."""
    s = Settings()
    s.max_pages = _env_int("DOCUPILOT_MAX_PAGES", s.max_pages, 1, HARD_MAX_PAGES)
    s.max_file_mb = _env_int("DOCUPILOT_MAX_FILE_MB", s.max_file_mb, 1, 500)
    s.ocr_dpi = _env_int("DOCUPILOT_OCR_DPI", s.ocr_dpi, 100, 600)
    s.max_processing_seconds = _env_int("DOCUPILOT_MAX_SECONDS", s.max_processing_seconds, 10, 7200)
    s.output_retention_hours = _env_int("DOCUPILOT_RETENTION_HOURS", s.output_retention_hours, 1, 720)
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd and cmd.strip():
        s.tesseract_cmd = cmd.strip()
    out = os.environ.get("DOCUPILOT_OUTPUT_DIR")
    if out and out.strip():
        s.output_dir = Path(out.strip())
    return s


SETTINGS = load_settings()
