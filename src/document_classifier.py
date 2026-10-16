"""Page- and document-level classification from cheap, inspectable page metrics."""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from config.settings import SETTINGS, Settings
from src.models import PageMetrics


def classify_page(m: PageMetrics, settings: Settings = SETTINGS) -> str:
    """Return one of: digital | mixed | scanned | unreliable_text | blank.

    - blank:            no text, no images, almost no vector content
    - scanned:          (almost) no native text but images/vector art present => needs OCR
    - unreliable_text:  native text exists but looks garbled (broken font encoding) => prefer OCR
    - mixed:            native text exists and large images cover the page
    - digital:          useful native text layer
    """
    if m.n_chars < settings.min_native_chars:
        if m.n_images > 0 and m.image_coverage >= 0.05:
            return "scanned"
        if m.n_images > 0 or m.n_drawings > 20:
            return "scanned"
        return "blank"
    if m.garbled_ratio > settings.max_garbled_ratio:
        return "unreliable_text"
    if m.image_coverage >= settings.image_coverage_mixed:
        return "mixed"
    return "digital"


def classify_document(kinds: Iterable[str]) -> str:
    counts = Counter(k for k in kinds if k != "blank")
    if not counts:
        return "blank"
    needs_ocr = counts.get("scanned", 0) + counts.get("unreliable_text", 0)
    digital = counts.get("digital", 0)
    mixed = counts.get("mixed", 0)
    total = sum(counts.values())
    if needs_ocr == total:
        return "scanned"
    if digital == total:
        return "digital"
    return "mixed" if (needs_ocr or mixed) else "digital"
