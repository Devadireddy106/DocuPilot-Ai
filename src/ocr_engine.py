"""Tesseract OCR wrapper.

IMPORTANT: `pytesseract` is only a thin Python wrapper. The real OCR engine is the separate
Tesseract executable, which must be installed on the machine (on Windows: the UB-Mannheim
installer). This module locates that executable, reports clearly when it is missing, and
provides image preprocessing + word-level OCR with Tesseract's own word confidence values
(0-100). Those values are engine-reported scores, not calibrated probabilities.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from config.settings import SETTINGS, Settings
from src.models import Word

try:  # optional
    import cv2  # type: ignore
except Exception:  # pragma: no cover - cv2 is optional
    cv2 = None

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None  # type: ignore

log = logging.getLogger(__name__)

INSTALL_HELP = (
    "Tesseract OCR is not installed or could not be found. Digital (text-based) PDFs still work, "
    "but scanned pages cannot be read. Windows: install Tesseract from "
    "https://github.com/UB-Mannheim/tesseract/wiki (default folder C:\\Program Files\\Tesseract-OCR), "
    "then restart the app, or set the TESSERACT_CMD environment variable to the full path of tesseract.exe."
)

_WINDOWS_DEFAULTS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


class OCRUnavailableError(RuntimeError):
    """Tesseract (or the requested language data) is missing."""


class OCRError(RuntimeError):
    """OCR ran but failed for this image (timeout, engine error)."""


@dataclass
class OCRStatus:
    available: bool
    version: str = ""
    command: str = ""
    message: str = ""


_RULE_GLYPHS = re.compile(r"[|¦\[\]{}_~\\/=]+")

_cache: dict = {}


def reset_cache() -> None:
    _cache.clear()


def _candidate_commands(settings: Settings) -> List[str]:
    explicit = settings.tesseract_cmd or os.environ.get("TESSERACT_CMD")
    if explicit:
        return [explicit]  # an explicit setting is never silently replaced
    cands: List[str] = []
    found = shutil.which("tesseract")
    if found:
        cands.append(found)
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(str(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
        cands.extend(_WINDOWS_DEFAULTS)
    return cands


def get_status(settings: Settings = SETTINGS, use_cache: bool = True) -> OCRStatus:
    """Locate Tesseract and report availability. Never raises."""
    key = (settings.tesseract_cmd, os.environ.get("TESSERACT_CMD"), settings.ocr_lang)
    if use_cache and key in _cache:
        return _cache[key]

    if pytesseract is None:
        st = OCRStatus(False, message="The Python package 'pytesseract' is not installed. " + INSTALL_HELP)
        _cache[key] = st
        return st

    status: Optional[OCRStatus] = None
    for cmd in _candidate_commands(settings):
        if not cmd or not (Path(cmd).exists() or shutil.which(cmd)):
            continue
        pytesseract.pytesseract.tesseract_cmd = cmd
        try:
            version = str(pytesseract.get_tesseract_version())
        except Exception as exc:  # executable present but not runnable
            log.info("Tesseract at %s not usable: %s", cmd, exc)
            continue
        try:
            langs = set(pytesseract.get_languages(config=""))
        except Exception:
            langs = set()
        wanted = set(settings.ocr_lang.split("+"))
        if langs and not wanted.issubset(langs):
            status = OCRStatus(
                False, version, cmd,
                f"Tesseract {version} was found but language data for '{settings.ocr_lang}' is missing "
                f"(installed: {', '.join(sorted(langs)) or 'none'}). Reinstall Tesseract with the language data.",
            )
            break
        status = OCRStatus(True, version, cmd, f"Tesseract {version} ready ({cmd}).")
        break
    if status is None:
        status = OCRStatus(False, message=INSTALL_HELP)
    _cache[key] = status
    return status


def require_ocr(settings: Settings = SETTINGS) -> OCRStatus:
    st = get_status(settings)
    if not st.available:
        raise OCRUnavailableError(st.message or INSTALL_HELP)
    pytesseract.pytesseract.tesseract_cmd = st.command
    return st


# --------------------------------------------------------------------------- preprocessing

def _to_gray_autocontrast(img: Image.Image) -> Image.Image:
    return ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)


def _estimate_skew_deg(gray: Image.Image, max_angle: float = 5.0, step: float = 0.5) -> float:
    """Projection-profile skew estimate on a down-scaled copy. Returns degrees to rotate by."""
    w, h = gray.size
    scale = 700.0 / max(w, h)
    small = gray.resize((max(1, int(w * scale)), max(1, int(h * scale)))) if scale < 1 else gray
    arr = np.asarray(small, dtype=np.uint8)
    thr = arr.mean() * 0.85
    binary = Image.fromarray(((arr < thr) * 255).astype(np.uint8))
    if np.count_nonzero(np.asarray(binary)) < 50:
        return 0.0
    best_angle, best_score = 0.0, -1.0
    for a in np.arange(-max_angle, max_angle + 1e-9, step):
        rot = binary.rotate(float(a), resample=Image.BILINEAR, fillcolor=0)
        score = float(np.var(np.asarray(rot, dtype=np.float32).sum(axis=1)))
        if score > best_score:
            best_score, best_angle = score, float(a)
    return 0.0 if abs(best_angle) < step else best_angle


def preprocess(img: Image.Image, variant: str = "gray") -> Image.Image:
    """Return a preprocessed copy.

    Variants
    --------
    gray     grayscale + autocontrast
    clean    grayscale, deskew, denoise, Otsu binarisation and REMOVAL OF TABLE RULING LINES
             (borders otherwise get read as '|' characters and hide the column gaps)
    upscaled grayscale at 1.5x
    """
    gray = _to_gray_autocontrast(img)
    if variant == "gray":
        return gray
    if variant == "upscaled":
        w, h = gray.size
        return gray.resize((int(w * 1.5), int(h * 1.5)), Image.LANCZOS)
    if variant == "clean":
        angle = _estimate_skew_deg(gray)
        if angle:
            gray = gray.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
        if cv2 is not None:
            arr = cv2.medianBlur(np.asarray(gray, dtype=np.uint8), 3)
            _t, binary = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            den = gray.filter(ImageFilter.MedianFilter(3))
            arr = np.asarray(den, dtype=np.uint8)
            binary = ((arr > _otsu_threshold(arr)) * 255).astype(np.uint8)
        ink = binary < 128
        ink &= ~_ruling_mask(ink)
        return Image.fromarray(np.where(ink, 0, 255).astype(np.uint8))
    raise ValueError(f"unknown preprocessing variant: {variant}")


def _runs_mask(ink: np.ndarray, min_len: int) -> np.ndarray:
    """True where `ink` has a horizontal run of at least `min_len` consecutive True values."""
    mask = np.zeros_like(ink, dtype=bool)
    sums = ink.sum(axis=1)
    for y in np.flatnonzero(sums >= min_len):
        padded = np.concatenate(([0], ink[y].astype(np.int8), [0]))
        d = np.diff(padded)
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        for a, b in zip(starts, ends):
            if b - a >= min_len:
                mask[y, a:b] = True
    return mask


def _ruling_mask(ink: np.ndarray) -> np.ndarray:
    """Mask of long horizontal/vertical strokes (table borders), slightly dilated."""
    h, w = ink.shape
    min_h = max(40, w // 40)
    min_v = max(40, h // 40)
    if cv2 is not None:
        u8 = ink.astype(np.uint8) * 255
        horiz = cv2.morphologyEx(u8, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (min_h, 1)))
        vert = cv2.morphologyEx(u8, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_v)))
        lines = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8))
        return lines > 0
    lines = _runs_mask(ink, min_h) | _runs_mask(ink.T, min_v).T
    out = lines.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            out |= np.roll(np.roll(lines, dy, axis=0), dx, axis=1)
    return out


def _otsu_threshold(arr: np.ndarray) -> int:
    hist = np.bincount(arr.ravel(), minlength=256).astype(np.float64)
    total = arr.size
    sum_all = float(np.dot(np.arange(256), hist))
    w_b = sum_b = 0.0
    best, thr = -1.0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, thr = between, t
    return thr


# --------------------------------------------------------------------------- recognition

def ocr_words(
    img: Image.Image,
    page_width_pt: float,
    psm: int = 6,
    settings: Settings = SETTINGS,
) -> List[Word]:
    """Run Tesseract and return words in PDF-point coordinates (top-left origin).

    Raises OCRUnavailableError if Tesseract is missing, OCRError for per-image failures.
    """
    require_ocr(settings)
    scale = page_width_pt / float(img.size[0]) if img.size[0] else 1.0
    config = f"--oem 3 --psm {int(psm)}"
    try:
        data = pytesseract.image_to_data(
            img,
            lang=settings.ocr_lang,
            config=config,
            output_type=pytesseract.Output.DICT,
            timeout=settings.ocr_timeout_s,
        )
    except pytesseract.TesseractNotFoundError as exc:  # pragma: no cover - raced removal
        raise OCRUnavailableError(INSTALL_HELP) from exc
    except RuntimeError as exc:  # timeout
        raise OCRError(f"OCR timed out or failed: {exc}") from exc
    except Exception as exc:
        raise OCRError(f"OCR engine error: {type(exc).__name__}: {exc}") from exc

    words: List[Word] = []
    n = len(data.get("text", []))
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt or _RULE_GLYPHS.fullmatch(txt):
            continue  # empty, or just leftovers of table borders (|, [, ], _ ...)
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:  # Tesseract uses -1 for non-word layout entries
            continue
        x, y, w, h = (float(data[k][i]) for k in ("left", "top", "width", "height"))
        words.append(Word(txt, x * scale, y * scale, (x + w) * scale, (y + h) * scale, conf))
    return words


def mean_confidence(words: List[Word]) -> Optional[float]:
    vals = [w.conf for w in words if w.conf is not None]
    return float(sum(vals) / len(vals)) if vals else None
