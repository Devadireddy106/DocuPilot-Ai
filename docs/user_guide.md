# User guide

## 1. Convert a PDF
1. Open the app (`python app.py` → <http://127.0.0.1:7860>, or your hosted link). The banner under the button says whether OCR is ready.
2. Choose a PDF — tap the upload box on a phone/tablet, or drag-and-drop on a computer. Limits: **50 pages, 50 MB**, no password.
3. Tap **Convert to Excel**. The bar and the stage list (*Checking the file → Reading pages → Rebuilding tables → Checking data quality → Writing Excel → Done*) are driven by the backend's real progress; the message shows the current page ("Reading page 7 of 30").
4. Read the summary card. The first line is the **status**:
   * ✅ **Complete** — every page was processed.
   * ⚠️ **Complete (review needed)** — every page was processed but some have low heuristic quality; review them.
   * ⚠️ **Partial** — some pages failed; the workbook holds only the other pages. It says so in the workbook too.
   * ℹ️ **No tables found** — the document was read but contains no table (the workbook explains this).
   * ❌ **Failed** — nothing usable could be produced; the message explains why.
5. Check the **preview** (scroll sideways for wide tables; use the *Table* selector when several tables were found) and the **validation list** (errors first).
6. Download the **Excel workbook**, the **validation report (.csv)** and the **processing log (.json, no cell values)**.

## 2. The workbook
| Sheet | Contents |
|---|---|
| `Extracted_Data` (+ `Extracted_Data_2`, …) | One sheet per independent table. Your columns in source order, then **Source_Page**, **Extraction_Method** and **Review_Flag**. Header row frozen, filters on. Rows with warnings are amber, with errors red, and list the issue IDs. |
| `Validation_Report` | Issue ID, Severity, Category, Page, Sheet, Row Reference, Column Name, Description, Suggested Action, Resolution Status. |
| `Processing_Summary` | File name and hash, pages, failed pages, OCR pages, tables, records, issue counts, durations, methods used, completion status; a red banner if the result is partial/failed; a per-page table (type, status, method, attempts, notes). |

Number cells are real numbers (with number formats), ISO dates are real dates, identifiers keep their leading zeros, and
anything ambiguous stays text. Text that begins with `=`, `+`, `-` or `@` is stored as text and can never run as a formula.

## 3. Reading the validation findings
* **Severity** — *Error* (act on it), *Warning* (look at it), *Info* (for your awareness).
* **Category** — *Confirmed* (a rule is definitely violated, e.g. an impossible date, a failed page), *Potential* (suspicious — could be genuine), *Unverified* (nothing is known to be wrong, but it has not been independently verified — all OCR output is in this group).
* **Resolution status** — `Open` for everything you need to decide; `Auto-corrected` for the few deterministic OCR repairs (original and corrected text are quoted in the description).
* A report with no findings **does not prove the data is right**; the automated checks are limited.

## 4. When something looks wrong
| You see | Meaning / what to do |
|---|---|
| "needs OCR but Tesseract is not available" | Install Tesseract (README); digital PDFs still convert. |
| "OCR confidence is too low … no values were kept" | The scan is too poor to read; rescan at ≥ 200–300 dpi, straight, high contrast. |
| `possible_wrapped_text` warnings | A cell that wrapped onto several lines was read as separate rows (un-ruled tables). Append the text to the cell above and delete the row. |
| "Possible continuation … NOT merged" | The tool was not sure two tables on consecutive pages are one table; merge them yourself if they are. |
| `ambiguous_dates` | Dates like 05/06/2026 could be 5 June or 6 May; they were left as text. |
| `total_mismatch` | A "Total" row from the PDF differs from the sum of the rows above; check for missed rows or misread digits. |
| `Column_1, Column_2…` headers | The PDF had no header row (or it was not found). The names are neutral placeholders — rename them. |
| Password / corrupt / > 50 pages | Remove the password, re-export the PDF, or split it. |

## 5. Privacy
Everything runs on the machine hosting the app. Do not upload confidential documents to a shared hosted copy. Details in README and `docs/deployment.md`.
