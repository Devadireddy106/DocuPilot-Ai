# Architecture

```
            ┌────────────┐   path    ┌──────────────┐
 Browser ─▶ │  app.py    │ ────────▶ │  pipeline.py │ ──▶ workbook (.xlsx) + validation (.csv) + log (.json)
 (Gradio)   │ (UI, real  │ ◀──────── │  convert_pdf │
            │  progress) │  progress └──────┬───────┘
            └────────────┘                  │
                                   ┌────────▼─────────┐
                                   │ ExtractionAgent  │  (extraction_agent.py)
                                   └────────┬─────────┘
   preflight → per page: inspect → classify → plan → attempt(s) → score → decide
                                   │
        ┌───────────────┬──────────┴───────────┬───────────────────────┐
        ▼               ▼                      ▼                       ▼
 pdf_preflight   pdf_extractor           ocr_engine            table_reconstructor
 (validate file) (pdfplumber, words      (Tesseract, clean-up, (merge pages, headers,
                  geometry, render)       word confidences)     furniture, ragged rows)
                                   │
                  schema_normalizer (types, deterministic corrections) → validator → excel_exporter
```

## The agent loop (per page)
1. **Inspect** (`pdf_extractor.inspect_page`): character count, images and their page coverage, vector drawings, garbled-text ratio.
2. **Classify** (`document_classifier.classify_page`): `blank`, `scanned`, `unreliable_text`, `mixed`, `digital`. Rules are plain thresholds in `config/settings.py`.
3. **Plan** (`plan_for`, a pure function): ordered strategies per kind, e.g. digital → `pdfplumber_lines`, `words_geometry`, `pdfplumber_text`; scanned → `ocr_clean_psm6`, `ocr_gray_psm6`, `ocr_clean_psm4`, `ocr_upscaled_psm6`.
4. **Attempt** strategies one at a time, at most `max_attempts_per_page` (4); stop as soon as the heuristic score ≥ `quality_accept` (0.60); stop on a time budget (`max_processing_seconds`, 900 s per document).
5. **Score** (`score_tables`): a heuristic in 0‥1 from cell fill ratio, row-length consistency, number of rows and header-likeness, mildly reduced by low OCR confidence. **It is not a probability.** Two gates follow:
   * *word-integrity* (native strategies): the share of cell tokens that are whole words actually present on the page. A column grid cutting through words (typical when a table finder is run on prose) caps the score at 0.25 → page reported as "no table".
   * *OCR confidence floor*: page mean Tesseract word confidence < 35/100 → output discarded, page reported as **failed** (no garbage shown as data).
6. **Decide**: `ok` (≥ 0.60), `low_quality` (≥ 0.30, kept but flagged), `no_tables`, `blank`, or `failed` (with a reason). OCR needed but unavailable ⇒ `failed`, never skipped silently.
7. **Log**: every decision, attempt, score and reason goes into `PageResult.decision_log` and the JSON processing log (the log never contains cell values).

Failures are isolated per page: an exception on page *n* marks that page failed and the loop continues.

## Table finding
* `pdfplumber_lines` — ruled tables via drawn lines.
* `words_geometry` — own algorithm on word boxes (native or OCR): group words into lines by vertical centre → split into blocks on large vertical gaps → **peel single-text-run lines** (titles/captions/footers) from the edges → find column corridors with exact interval sweeps (a corridor is a vertical band ≥ `gap_factor × median word height` wide that ≤ 8 % of lines cross) → assign words to columns by centre.
* OCR clean-up (`ocr_engine.preprocess("clean")`): grayscale → deskew (projection profile) → median blur → Otsu → **removal of long horizontal/vertical strokes** (table borders otherwise become `|` characters and hide column gaps).

## Reconstruction (`table_reconstructor.py`)
Per raw table: strip page furniture (page numbers; single-cell edge rows repeated on ≥ 2 pages), normalise whitespace, drop empty columns, remember each row's original cell count.
Across pages (only **consecutive** pages, last table of page *p* ↔ first of *p+1*):
* same column count and header similarity ≥ 0.80 → merge, drop the repeated header;
* similarity 0.50‥0.80 → keep separate and flag "possible continuation";
* first row not header-like, same width, previous table ended low on its page, this one starts high, numeric columns still look numeric → headerless continuation, merge (noted);
* anything else → separate table (conservative), flagged if it might be a continuation.
No header text is ever invented: missing headers become `Column_N`; duplicate headers get a numeric suffix (noted).

## Types and corrections (`schema_normalizer.py`)
Strict number parsing (western `1,234.50` and Indian `12,34,567.89` grouping only; `1,23` is *not* parsed; leading-zero strings, long digit strings and id-like headers stay text). Dates are converted only when day/month order is unambiguous. The only automatic value changes are two OCR rules (stray border character stuck to a number; O/l/I look-alikes inside a number) applied solely to OCR cells in numeric columns whose result parses as a number — each recorded with original, corrected, rule, reason and shown in the Validation_Report as "Auto-corrected".

## Validation (`validator.py`)
Separate from extraction; never edits values. Findings carry Severity (Error/Warning/Info), Category (Confirmed/Potential/Unverified), page, table, row, column, description, suggested action, resolution status. Per-check cap of 100 findings per table prevents flooding.

## Export (`excel_exporter.py`)
Sheets: `Extracted_Data` (+ `Extracted_Data_2…`), `Validation_Report`, `Processing_Summary`. Source page, extraction method and a Review_Flag column are appended after the source columns (original column order preserved). The workbook is built in a temp dir, **re-opened and verified** (zip integrity, required sheets, row counts, no formulas) before being published under a unique name (never overwriting). Strings starting with `= + - @` are stored as text cells with Excel's quote-prefix flag; illegal XML characters are removed; 32 000-character cap per cell.

## Security
Treat PDFs as untrusted: signature, size, page-count, corruption and encryption checks; private temp workspace with a fixed internal file name; output names sanitised; no command is ever built from document content; Tesseract is invoked through pytesseract with fixed arguments and a per-call timeout; page renders are size-capped; no network calls; no secrets in the repository.
