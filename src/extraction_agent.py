"""The extraction agent: an explainable, per-page decision engine.

For every page the agent
  1. inspects it (text layer, images, vector art, garbling),
  2. classifies it (digital / mixed / scanned / unreliable_text / blank),
  3. builds an ordered plan of strategies,
  4. runs strategies one at a time, scoring each result with a *heuristic* quality score,
  5. stops as soon as a result is good enough, or after a bounded number of attempts,
  6. records every decision, attempt and failure reason in the page's state.

The quality score is a heuristic in 0..1. It is NOT a calibrated probability and is never shown
as an accuracy percentage.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from config.settings import SETTINGS, Settings
from src import ocr_engine, pdf_extractor
from src.document_classifier import classify_document, classify_page
from src.models import Attempt, ExtractionResult, PageResult, RawTable
from src.pdf_preflight import PreflightError, run_preflight
from src.schema_normalizer import apply_ocr_digit_fixes, infer_column_types
from src.table_reconstructor import is_header_like, make_sheet_name, reconstruct_tables
from src.validator import validate

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None  # type: ignore

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

NATIVE_STRATEGIES = ("pdfplumber_lines", "words_geometry", "pdfplumber_text")
OCR_STRATEGIES = {
    "ocr_gray_psm6": ("gray", "ocr_psm_primary"),
    "ocr_clean_psm6": ("clean", "ocr_psm_primary"),
    "ocr_clean_psm4": ("clean", "ocr_psm_alt"),
    "ocr_upscaled_psm6": ("upscaled", "ocr_psm_primary"),
}


# ----------------------------------------------------------------------------- scoring

def score_tables(tables: Sequence[RawTable], mean_conf: Optional[float] = None) -> float:
    """Heuristic quality score in [0, 1] for the tables found on one page.

    Rewards well-filled, regular tables with several rows and a header-like first row; applies a
    mild penalty for low OCR word confidence. Prose mis-detected as a table scores low because
    most of its cells are empty.
    """
    if not tables:
        return 0.0
    weights, scores = [], []
    for t in tables:
        n = len(t.rows)
        if n == 0:
            continue
        lengths = [len(r) for r in t.rows]
        ncols = max(lengths)
        if ncols < 2:
            continue
        fill = sum(1 for r in t.rows for c in r if c) / float(n * ncols)
        modal = max(set(lengths), key=lengths.count)
        consistency = lengths.count(modal) / n
        fill_c = min(1.0, max(0.0, (fill - 0.25) / 0.45))
        rows_c = min(1.0, n / 6.0)
        hdr = 1.0 if is_header_like(t.rows[0], ncols) else 0.6
        scores.append(fill_c * (0.40 + 0.20 * consistency + 0.25 * rows_c + 0.15 * hdr))
        weights.append(n)
    if not scores:
        return 0.0
    score = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
    if mean_conf is not None:
        score *= 0.75 + 0.25 * min(1.0, mean_conf / 85.0)
    return max(0.0, min(1.0, score))


def word_integrity(tables: Sequence[RawTable], words: Sequence) -> float:
    """Share of cell tokens that are whole words actually present on the page (0..1).

    A wrong column split (typically pdfplumber's text strategy on prose) cuts words in half
    ('agenda ite' | 'ms'). Genuine tables score ~1.0. Used only for native strategies, where the
    page's own word list is the ground truth.
    """
    vocab = {w.text for w in words}
    total = hit = 0
    for t in tables:
        for row in t.rows:
            for cell in row:
                for tok in cell.split():
                    total += 1
                    hit += tok in vocab
    return hit / total if total else 1.0


# ----------------------------------------------------------------------------- planning

def plan_for(kind: str, image_coverage: float = 0.0, ocr_ok: bool = True) -> List[str]:
    """Ordered strategy plan for a page kind (pure function - easy to test)."""
    if kind == "blank":
        return []
    if kind == "digital":
        plan = list(NATIVE_STRATEGIES)
        if image_coverage >= 0.15:
            plan.append("ocr_clean_psm6")
        return plan
    if kind == "mixed":
        return ["pdfplumber_lines", "words_geometry", "ocr_clean_psm6", "ocr_gray_psm6"]
    if kind == "scanned":
        return ["ocr_clean_psm6", "ocr_gray_psm6", "ocr_clean_psm4", "ocr_upscaled_psm6"]
    if kind == "unreliable_text":
        return ["ocr_clean_psm6", "ocr_gray_psm6", "words_geometry"]
    return list(NATIVE_STRATEGIES)


# ----------------------------------------------------------------------------- agent

@dataclass
class _Ctx:
    started: float
    deadline: float
    ocr_ok: bool
    ocr_msg: str
    plumber: object = None


class ExtractionAgent:
    def __init__(self, settings: Settings = SETTINGS, progress: Optional[ProgressFn] = None):
        self.s = settings
        self._progress = progress

    # -- helpers -----------------------------------------------------------
    def _emit(self, frac: float, msg: str) -> None:
        if self._progress:
            try:
                self._progress(max(0.0, min(1.0, frac)), msg)
            except Exception:  # a broken UI callback must never break extraction
                log.debug("progress callback failed", exc_info=True)

    # -- public ------------------------------------------------------------
    def run(self, path: Union[str, Path], display_name: Optional[str] = None) -> ExtractionResult:
        t0 = time.monotonic()
        self._emit(0.02, "Checking the PDF file")
        pre = run_preflight(path, self.s)  # raises PreflightError with a friendly message

        ocr_status = ocr_engine.get_status(self.s)
        result = ExtractionResult(
            filename=display_name or Path(path).name,
            sha256=pre.sha256,
            total_pages=pre.page_count,
            ocr_available=ocr_status.available,
            ocr_version=ocr_status.version,
            warnings=list(pre.warnings),
            tool_versions=_tool_versions(ocr_status.version),
        )
        if not ocr_status.available:
            result.warnings.append("OCR is unavailable: " + ocr_status.message)

        ctx = _Ctx(started=t0, deadline=t0 + self.s.max_processing_seconds,
                   ocr_ok=ocr_status.available, ocr_msg=ocr_status.message)

        doc = pymupdf.open(str(pre.path))
        plumber_doc = None
        try:
            if pdfplumber is not None:
                try:
                    plumber_doc = pdfplumber.open(str(pre.path))
                except Exception as exc:
                    result.warnings.append(f"pdfplumber could not open the file ({type(exc).__name__}); table-line detection skipped.")
            ctx.plumber = plumber_doc

            n = pre.page_count
            for pno in range(n):
                frac = 0.05 + 0.75 * (pno / n)
                if time.monotonic() > ctx.deadline:
                    result.aborted_for_time = True
                    for rest in range(pno, n):
                        pr = PageResult(page=rest + 1, status="failed", failure_reason="time budget exhausted before this page was processed")
                        pr.log("Skipped: processing time budget exhausted.")
                        result.page_results.append(pr)
                    result.warnings.append("The time budget was exhausted; remaining pages were not processed.")
                    break
                self._emit(frac, f"Reading page {pno + 1} of {n}")
                result.page_results.append(self._process_page(doc, pno, ctx, n))
        finally:
            doc.close()
            if plumber_doc is not None:
                try:
                    plumber_doc.close()
                except Exception:  # pragma: no cover
                    pass

        result.doc_kind = classify_document(p.kind for p in result.page_results if p.kind != "unknown")

        self._emit(0.84, "Rebuilding tables across pages")
        tables = reconstruct_tables(result.page_results, self.s)
        used: set = set()
        for i, t in enumerate(tables, start=1):
            t.sheet_name = make_sheet_name("Extracted_Data" if i == 1 else f"Extracted_Data_{i}", used)

        self._emit(0.90, "Checking data quality")
        for t in tables:
            types = infer_column_types(t)
            result.corrections.extend(apply_ocr_digit_fixes(t, types))
        result.tables = tables
        result.issues = validate(
            tables, result.page_results, result.corrections, self.s,
            ocr_available=ocr_status.available, aborted_for_time=result.aborted_for_time,
            total_pages=n,
        )
        _finalise_status(result)
        result.duration_s = time.monotonic() - t0
        return result

    # -- per page ----------------------------------------------------------
    def _process_page(self, doc, pno: int, ctx: _Ctx, total: int) -> PageResult:
        page_no = pno + 1
        pr = PageResult(page=page_no)
        try:
            page = doc.load_page(pno)
            pr.metrics = pdf_extractor.inspect_page(page, page_no, self.s)
            m = pr.metrics
            pr.kind = classify_page(m, self.s)
            pr.log(
                f"Inspected: {m.n_chars} text chars, {m.n_images} image(s) covering {m.image_coverage:.0%}, "
                f"{m.n_drawings} vector drawing(s), garbled ratio {m.garbled_ratio:.2f} -> classified '{pr.kind}'."
            )
            if pr.kind == "blank":
                pr.status = "blank"
                pr.log("Page has no text, images or meaningful drawings; nothing to extract.")
                return pr

            pr.plan = plan_for(pr.kind, m.image_coverage, ctx.ocr_ok)
            pr.log("Plan: " + " -> ".join(pr.plan))
            self._execute_plan(page, pr, ctx)
        except Exception as exc:  # per-page failure isolation
            log.exception("Page %s failed", page_no)
            pr.status = "failed"
            pr.failure_reason = f"unexpected error ({type(exc).__name__}: {str(exc)[:160]})"
            pr.log("Aborted this page after an unexpected error; other pages continue.")
        return pr

    def _execute_plan(self, page, pr: PageResult, ctx: _Ctx) -> None:
        s = self.s
        counted = 0
        best: Optional[Tuple[float, List[RawTable], str, Optional[float]]] = None
        ocr_errors: List[str] = []
        ocr_succeeded = False
        image_cache: Dict[str, object] = {}
        plumber_page = None
        if ctx.plumber is not None and any(x.startswith("pdfplumber") for x in pr.plan):
            try:
                plumber_page = ctx.plumber.pages[pr.page - 1]
            except Exception as exc:
                pr.warnings.append(f"pdfplumber could not load this page ({type(exc).__name__}).")

        for strat in pr.plan:
            if counted >= s.max_attempts_per_page:
                pr.log(f"Attempt limit ({s.max_attempts_per_page}) reached; stopping.")
                break
            if time.monotonic() > ctx.deadline:
                pr.log("Time budget exhausted; stopping retries.")
                break
            is_ocr = strat.startswith("ocr")
            if is_ocr and not ctx.ocr_ok:
                pr.attempts.append(Attempt(strat, False, "OCR unavailable (Tesseract not found)"))
                pr.log(f"Skipped {strat}: OCR unavailable.")
                continue
            if strat.startswith("pdfplumber") and plumber_page is None:
                pr.attempts.append(Attempt(strat, False, "pdfplumber page unavailable"))
                continue

            t_start = time.monotonic()
            counted += 1
            if is_ocr:
                pr.ocr_attempted = True
            try:
                tables, n_words, conf = self._run_strategy(strat, page, plumber_page, pr, image_cache)
                if is_ocr:
                    ocr_succeeded = True
                    pr.n_words_seen = max(pr.n_words_seen, n_words)
                score = score_tables(tables, conf)
                reason = (
                    f"found {len(tables)} table(s), {sum(len(t.rows) for t in tables)} row(s)"
                    if tables else ("no words recognised" if is_ocr and n_words == 0 else "no table structure found")
                )
                if tables and strat.startswith("pdfplumber"):
                    if "words" not in image_cache:
                        image_cache["words"] = pdf_extractor.native_words(page)
                    integ = word_integrity(tables, image_cache["words"])  # type: ignore[arg-type]
                    if integ < 0.95:
                        score = min(score, 0.25) if integ < 0.80 else score * integ
                        reason += (f"; only {integ:.0%} of cell words are whole words on the page "
                                   "(column boundaries cut through words)")
                att = Attempt(strat, bool(tables), reason, len(tables), sum(len(t.rows) for t in tables), score,
                              time.monotonic() - t_start, conf)
                pr.attempts.append(att)
                pr.log(f"{strat}: {reason}; heuristic score {score:.2f}.")
                if best is None or score > best[0]:
                    best = (score, tables, strat, conf)
                if score >= s.quality_accept:
                    pr.log(f"Score {score:.2f} >= {s.quality_accept:.2f}: accepted, no further attempts needed.")
                    break
                if is_ocr and n_words == 0 and strat == "ocr_clean_psm4":
                    break
            except ocr_engine.OCRUnavailableError as exc:
                ctx.ocr_ok = False
                pr.attempts.append(Attempt(strat, False, "OCR unavailable", duration_s=time.monotonic() - t_start))
                ocr_errors.append(str(exc))
            except Exception as exc:
                msg = f"{type(exc).__name__}: {str(exc)[:140]}"
                pr.attempts.append(Attempt(strat, False, f"error - {msg}", duration_s=time.monotonic() - t_start))
                pr.log(f"{strat} failed ({msg}).")
                if is_ocr:
                    ocr_errors.append(msg)

        image_cache.clear()
        self._decide(pr, best, ctx, ocr_succeeded, ocr_errors)

    def _run_strategy(self, strat: str, page, plumber_page, pr: PageResult, image_cache: dict):
        """Return (tables, n_words_seen, mean_ocr_conf)."""
        if strat == "pdfplumber_lines":
            return pdf_extractor.extract_pdfplumber_lines(plumber_page, pr.page), 0, None
        if strat == "pdfplumber_text":
            return pdf_extractor.extract_pdfplumber_text(plumber_page, pr.page), 0, None
        if strat == "words_geometry":
            tables, n = pdf_extractor.extract_words_geometry(page, pr.page, self.s)
            return tables, n, None
        if strat in OCR_STRATEGIES:
            variant, psm_attr = OCR_STRATEGIES[strat]
            if "page" not in image_cache:
                image_cache["page"] = pdf_extractor.render_page(page, self.s.ocr_dpi, self.s.ocr_max_render_px)
            img = ocr_engine.preprocess(image_cache["page"], variant)  # type: ignore[arg-type]
            words = ocr_engine.ocr_words(img, float(page.rect.width), getattr(self.s, psm_attr), self.s)
            tables = pdf_extractor.words_to_tables(words, pr.page, float(page.rect.height), strat, self.s)
            return tables, len(words), ocr_engine.mean_confidence(words)
        raise ValueError(f"unknown strategy {strat}")

    def _decide(self, pr: PageResult, best, ctx: _Ctx, ocr_succeeded: bool, ocr_errors: List[str]) -> None:
        s = self.s
        score = best[0] if best else 0.0
        ocr_needed = pr.kind in ("scanned", "unreliable_text") or (pr.kind == "mixed" and score < s.quality_accept)

        if best and score >= s.quality_accept:
            pr.status = "ok"
        elif best and score >= s.quality_min_usable:
            pr.status = "low_quality"
            pr.warnings.append(
                f"best extraction scored {score:.2f} (< {s.quality_accept:.2f}); rows may be incomplete or misaligned"
            )
        else:
            pr.status = "no_tables"
            best = None if best is None or score < s.quality_min_usable else best
            if ocr_needed and not ctx.ocr_ok and not ocr_succeeded:
                pr.status = "failed"
                pr.failure_reason = "page needs OCR but Tesseract is not available (see README for installation)"
            elif ocr_needed and ocr_errors and not ocr_succeeded:
                pr.status = "failed"
                pr.failure_reason = "OCR failed: " + ocr_errors[-1][:160]
            elif pr.ocr_attempted and pr.n_words_seen == 0:
                pr.warnings.append("OCR recognised no text on this page; it may be blank, very faint or an unsupported language")

        if best is not None and pr.status in ("ok", "low_quality") and best[2].startswith("ocr") \
                and best[3] is not None and best[3] < s.ocr_unusable_conf:
            pr.status = "failed"
            pr.failure_reason = (f"OCR confidence is too low (mean {best[3]:.0f}/100): the scan is too poor to read reliably, "
                                 "so no values were kept. Try a higher-resolution or cleaner scan.")
            pr.warnings.append("low-confidence OCR output was discarded rather than shown as data")
            best = None

        if best is not None and pr.status in ("ok", "low_quality"):
            pr.score, pr.tables, pr.method, conf = best
            pr.ocr_used = pr.method.startswith("ocr")
            pr.ocr_mean_conf = conf if pr.ocr_used else None
        else:
            pr.score = 0.0
        if pr.status == "low_quality" and ocr_needed and not ctx.ocr_ok:
            pr.warnings.append("OCR would be needed to read this page reliably but is unavailable")
        pr.log(f"Decision: status={pr.status}" + (f", kept '{pr.method}'" if pr.method else "") + (f", reason: {pr.failure_reason}" if pr.failure_reason else "") + ".")


# ----------------------------------------------------------------------------- finishing

def _finalise_status(r: ExtractionResult) -> None:
    pages = r.page_results
    failed = [p for p in pages if p.status == "failed"]
    lowq = [p for p in pages if p.status == "low_quality"]
    if not r.tables:
        if failed and len(failed) == len(pages):
            r.status, r.status_detail = "Failed", "No page could be processed."
        elif failed:
            r.status = "Failed"
            r.status_detail = f"No tables were extracted and {len(failed)} page(s) failed."
        else:
            r.status, r.status_detail = "No tables found", "The document was read, but no tables were found."
        return
    if failed or r.aborted_for_time:
        nums = ", ".join(str(p.page) for p in failed[:12]) + ("..." if len(failed) > 12 else "")
        r.status = "Partial"
        r.status_detail = f"{len(failed)} page(s) could not be processed (pages {nums}); the workbook contains partial results."
    else:
        if lowq:
            r.status = "Complete (review needed)"
            nums = ", ".join(str(p.page) for p in lowq[:12]) + ("..." if len(lowq) > 12 else "")
            r.status_detail = (f"All pages were processed, but {len(lowq)} page(s) have low heuristic quality "
                               f"(pages {nums}) and must be reviewed.")
        else:
            r.status = "Complete"
            r.status_detail = "All pages were processed."


def _tool_versions(tesseract_version: str) -> Dict[str, str]:
    v = {"python": sys.version.split()[0]}
    try:
        v["pymupdf"] = pymupdf.VersionBind
    except Exception:  # pragma: no cover
        pass
    if pdfplumber is not None:
        v["pdfplumber"] = getattr(pdfplumber, "__version__", "?")
    v["tesseract"] = tesseract_version or "not available"
    return v
