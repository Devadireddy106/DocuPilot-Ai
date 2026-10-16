"""Native (non-OCR) PDF access: page inspection, rendering, and table-finding strategies.

Strategies implemented here
---------------------------
* ``pdfplumber_lines``  - ruled tables (cell borders drawn as lines/rectangles)
* ``pdfplumber_text``   - un-ruled tables, column positions inferred from word alignment
* ``words_geometry``    - own gap-based layout analysis on PyMuPDF words (also used for OCR words)

All of them return :class:`RawTable` objects: exactly what was found on one page, before any
cross-page reconstruction.
"""
from __future__ import annotations

import logging
import re
from statistics import median
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from config.settings import SETTINGS, Settings
from src.models import PageMetrics, RawTable, Word

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)


def clean_text(value: Any) -> str:
    """Collapse whitespace; remove zero-width characters. Never alters visible characters."""
    if value is None:
        return ""
    s = str(value).replace("\xa0", " ").translate(_ZERO_WIDTH)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    return _WS.sub(" ", s).strip()


# --------------------------------------------------------------------------- inspection

def _garbled_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = 0
    for c in chars:
        o = ord(c)
        if c == "�" or o < 32 or 0xE000 <= o <= 0xF8FF:
            bad += 1
    return bad / len(chars)


def inspect_page(page: "pymupdf.Page", page_no: int, settings: Settings = SETTINGS) -> PageMetrics:
    rect = page.rect
    width, height = float(rect.width), float(rect.height)
    text = page.get_text("text") or ""
    n_chars = sum(1 for c in text if not c.isspace())

    n_images, coverage = 0, 0.0
    try:
        infos = page.get_image_info()
        area = max(width * height, 1.0)
        covered = 0.0
        for info in infos:
            bx = info.get("bbox")
            if not bx:
                continue
            r = pymupdf.Rect(bx) & rect
            if not r.is_empty:
                covered += r.width * r.height
            n_images += 1
        coverage = min(1.0, covered / area)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("image info failed on page %s: %s", page_no, exc)

    try:
        n_drawings = len(page.get_drawings())
    except Exception:  # pragma: no cover
        n_drawings = 0

    n_words = len(text.split())
    return PageMetrics(
        page=page_no,
        width=width,
        height=height,
        n_chars=n_chars,
        n_words=n_words,
        n_images=n_images,
        image_coverage=coverage,
        n_drawings=n_drawings,
        garbled_ratio=_garbled_ratio(text),
    )


def render_page(page: "pymupdf.Page", dpi: int, max_px: int) -> Image.Image:
    """Render a page to an RGB PIL image, capping the longest side at max_px (memory guard)."""
    rect = page.rect
    zoom = dpi / 72.0
    longest = max(rect.width, rect.height) * zoom
    if longest > max_px:
        zoom *= max_px / longest
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def native_words(page: "pymupdf.Page") -> List[Word]:
    out: List[Word] = []
    for w in page.get_text("words") or []:
        txt = clean_text(w[4])
        if txt:
            out.append(Word(txt, float(w[0]), float(w[1]), float(w[2]), float(w[3])))
    return out


# --------------------------------------------------------------------------- pdfplumber strategies

_LINES = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 5,
}
_TEXT = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "min_words_vertical": 2,
    "min_words_horizontal": 1,
    "text_x_tolerance": 3,
    "text_y_tolerance": 3,
}


def _plumber_tables(plumber_page: Any, page_no: int, settings_dict: dict, method: str) -> List[RawTable]:
    height = float(plumber_page.height)
    out: List[RawTable] = []
    try:
        found = plumber_page.find_tables(table_settings=settings_dict)
    except Exception as exc:
        raise RuntimeError(f"pdfplumber could not analyse the page: {exc}") from exc
    for t in found:
        try:
            rows_raw = t.extract()
        except Exception:
            continue
        rows = [[clean_text(c) for c in (r or [])] for r in rows_raw]
        row_y: List[Optional[Tuple[float, float]]] = []
        try:
            for r in t.rows:
                row_y.append((float(r.bbox[1]), float(r.bbox[3])))
        except Exception:
            row_y = []
        if len(row_y) != len(rows):
            row_y = [None] * len(rows)
        keep = [i for i, r in enumerate(rows) if any(c for c in r)]
        rows = [rows[i] for i in keep]
        row_y = [row_y[i] for i in keep]
        if len(rows) >= 1 and max((len(r) for r in rows), default=0) >= 2:
            out.append(
                RawTable(
                    page=page_no,
                    index=len(out),
                    rows=rows,
                    method=method,
                    bbox=tuple(float(v) for v in t.bbox),  # type: ignore[arg-type]
                    page_height=height,
                    row_y=row_y,
                )
            )
    try:
        plumber_page.flush_cache()
    except Exception:  # pragma: no cover
        pass
    return out


def extract_pdfplumber_lines(plumber_page: Any, page_no: int) -> List[RawTable]:
    return _plumber_tables(plumber_page, page_no, _LINES, "pdfplumber_lines")


def extract_pdfplumber_text(plumber_page: Any, page_no: int) -> List[RawTable]:
    return _plumber_tables(plumber_page, page_no, _TEXT, "pdfplumber_text")


# --------------------------------------------------------------------------- own geometry strategy

def _group_lines(words: Sequence[Word], row_tol: float) -> List[List[Word]]:
    """Cluster words into text lines by vertical centre."""
    if not words:
        return []
    heights = [max(w.y1 - w.y0, 1e-3) for w in words]
    med_h = median(heights)
    ordered = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    lines: List[List[Word]] = []
    centres: List[float] = []
    for w in ordered:
        yc = (w.y0 + w.y1) / 2
        if lines and abs(yc - centres[-1]) <= row_tol * med_h:
            lines[-1].append(w)
            centres[-1] = sum((x.y0 + x.y1) / 2 for x in lines[-1]) / len(lines[-1])
        else:
            lines.append([w])
            centres.append(yc)
    for ln in lines:
        ln.sort(key=lambda w: w.x0)
    return lines


def _split_blocks(lines: List[List[Word]], med_h: float) -> List[List[List[Word]]]:
    """Split the line list wherever the vertical gap is much larger than normal line spacing."""
    if not lines:
        return []

    def top(ln): return min(w.y0 for w in ln)
    def bot(ln): return max(w.y1 for w in ln)

    gaps = [top(lines[i + 1]) - bot(lines[i]) for i in range(len(lines) - 1)]
    pos = [g for g in gaps if g > 0]
    typical = median(pos) if pos else med_h * 0.3
    limit = max(typical * 2.5, med_h * 1.6)
    blocks: List[List[List[Word]]] = [[lines[0]]]
    for i, g in enumerate(gaps):
        if g > limit:
            blocks.append([lines[i + 1]])
        else:
            blocks[-1].append(lines[i + 1])
    return blocks


def _column_boundaries(block: List[List[Word]], min_gap: float) -> List[float]:
    """x positions separating columns: wide vertical corridors that (almost) no line crosses.

    Uses exact interval arithmetic (no pixel grid) so narrow gutters are not eaten by rounding.
    """
    # 1. union of word intervals per line (so a line counts at most once at any x)
    per_line: List[List[Tuple[float, float]]] = []
    for ln in block:
        ivs = sorted((w.x0, w.x1) for w in ln)
        merged: List[List[float]] = []
        for a, b in ivs:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        per_line.append([(a, b) for a, b in merged])

    # 2. sweep: how many lines cover each elementary segment
    events: List[Tuple[float, int]] = []
    for ivs in per_line:
        for a, b in ivs:
            events.append((a, 1))
            events.append((b, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    n = len(block)
    tol = 0 if n < 10 else max(1, int(0.08 * n))

    bounds: List[float] = []
    cover = 0
    free_start: Optional[float] = None
    seen_occupied = False
    for x, delta in events:
        before = cover
        cover += delta
        was_free = before <= tol
        is_free = cover <= tol
        if was_free and not is_free:  # a free corridor ends here
            if free_start is not None and seen_occupied and (x - free_start) >= min_gap:
                bounds.append((free_start + x) / 2.0)
            free_start = None
        elif not was_free and is_free:  # a free corridor starts here
            free_start = x
            seen_occupied = True
        elif before == 0 and delta > 0:
            seen_occupied = True
    return bounds


def _join_cell_words(ws: List[Word], med_h: float) -> str:
    """Join the words of one cell with single spaces.

    Exception (OCR only): Tesseract often splits an identifier after its hyphen ('INV-' '1002').
    When an OCR word ends with '-', the next OCR word starts with a letter/digit and the two almost
    touch (gap < 0.3 x median word height, well below a real word space) they are joined directly.
    """
    out = ""
    prev: Optional[Word] = None
    for w in ws:
        if prev is None:
            out = w.text
        else:
            tight_hyphen = (
                w.conf is not None and prev.conf is not None
                and prev.text.endswith("-") and w.text[:1].isalnum()
                and (w.x0 - prev.x1) < 0.3 * med_h
            )
            out += ("" if tight_hyphen else " ") + w.text
        prev = w
    return clean_text(out)


def _assign_columns(block: List[List[Word]], bounds: List[float], med_h: float) -> Tuple[List[List[str]], List[List[Optional[float]]]]:
    ncols = len(bounds) + 1
    rows: List[List[str]] = []
    confs: List[List[Optional[float]]] = []
    for ln in block:
        cells: List[List[Word]] = [[] for _ in range(ncols)]
        for w in ln:
            xc = (w.x0 + w.x1) / 2
            col = sum(1 for b in bounds if xc > b)
            cells[col].append(w)
        rows.append([_join_cell_words(c, med_h) for c in cells])
        confs.append([None if not c else _mean([x.conf for x in c if x.conf is not None]) for c in cells])
    return rows, confs


def _mean(vals: List[float]) -> Optional[float]:
    return float(sum(vals) / len(vals)) if vals else None


def _segment_count(line: List[Word], min_gap: float) -> int:
    """Number of text runs in a line, where runs are separated by gaps >= min_gap.

    A real table row has several runs (one per filled cell). A title/caption/footer is ONE run,
    even when it happens to straddle a column corridor.
    """
    segs = 1
    for a, b in zip(line, line[1:]):
        if b.x0 - a.x1 >= min_gap:
            segs += 1
    return segs


def words_to_tables(
    words: Sequence[Word],
    page_no: int,
    page_height: float,
    method: str,
    settings: Settings = SETTINGS,
) -> List[RawTable]:
    """Reconstruct tables purely from word geometry (works for native and OCR words)."""
    words = [w for w in words if w.text and (w.x1 > w.x0)]
    if len(words) < 4:
        return []
    med_h = median(max(w.y1 - w.y0, 1e-3) for w in words)
    min_gap = settings.gap_factor * med_h
    lines = _group_lines(words, settings.row_tolerance)
    tables: List[RawTable] = []
    for block in _split_blocks(lines, med_h):
        if len(block) < 2:
            continue

        # Peel single-run lines (titles, captions, footers) hugging the edges of a multi-column body.
        # Done BEFORE corridor detection so a centred title cannot destroy a column gap.
        segs = [_segment_count(ln, min_gap) for ln in block]
        notes: List[str] = []
        lo, hi = 0, len(block)
        while hi - lo > 2 and segs[lo] == 1 and segs[lo + 1] >= 2 and median(segs[lo:hi]) >= 3:
            notes.append("Text above table: " + " ".join(w.text for w in block[lo]))
            lo += 1
        while hi - lo > 2 and segs[hi - 1] == 1 and segs[hi - 2] >= 2 and median(segs[lo:hi]) >= 3:
            notes.append("Text below table: " + " ".join(w.text for w in block[hi - 1]))
            hi -= 1
        block = block[lo:hi]
        if len(block) < 2:
            continue

        bounds = _column_boundaries(block, min_gap)
        if not bounds:
            continue
        rows, confs = _assign_columns(block, bounds, med_h)

        # Drop columns that are completely empty (can occur when a corridor split nothing off).
        keep_cols = [c for c in range(len(rows[0])) if any(r[c] for r in rows)]
        if len(keep_cols) < 2:
            continue
        rows = [[r[c] for c in keep_cols] for r in rows]
        confs = [[cf[c] for c in keep_cols] for cf in confs]
        y_ranges = [(min(w.y0 for w in ln), max(w.y1 for w in ln)) for ln in block]

        row_conf = [_mean([c for c in cf if c is not None]) for cf in confs]
        bbox = (
            min(w.x0 for ln in block for w in ln),
            y_ranges[0][0],
            max(w.x1 for ln in block for w in ln),
            y_ranges[-1][1],
        )
        tables.append(
            RawTable(
                page=page_no,
                index=len(tables),
                rows=rows,
                method=method,
                bbox=bbox,
                page_height=page_height,
                row_conf=row_conf,
                row_y=list(y_ranges),
                notes=notes,
            )
        )
    return tables


def extract_words_geometry(page: "pymupdf.Page", page_no: int, settings: Settings = SETTINGS) -> Tuple[List[RawTable], int]:
    words = native_words(page)
    return words_to_tables(words, page_no, float(page.rect.height), "words_geometry", settings), len(words)
