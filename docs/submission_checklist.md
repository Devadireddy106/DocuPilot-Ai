# Challenge submission checklist

Deadline given: **16 October 2026, 7:00 PM IST.** Status as of the build: ✅ done and evidenced · ⚠️ done with a caveat · ⬜ **you must do this** (could not be done in the build environment).

## Product requirements
| # | Requirement | Status | Evidence / caveat |
|---|---|---|---|
| 1 | Accept valid PDFs; reject bad ones with clear messages | ✅ | `tests/test_pdf_preflight.py`, `tests/test_error_recovery.py` |
| 2 | Text + tables from digital PDFs | ✅ | benchmark `digital_*` types |
| 3 | OCR for scanned pages | ✅ ⚠️ | benchmark `scanned`, `mixed`; synthetic scans only |
| 4 | Mixed digital/scanned documents (per-page decision) | ✅ | `mixed_digital_scanned` case, `test_mixed_document_digital_plus_scanned` |
| 5 | 10+ columns | ✅ | 12- and 16-column cases |
| 6 | Tables spanning pages | ✅ | repeated-header and header-less continuation cases, 50-page stress |
| 7 | Preserve headers, order, records, source page | ✅ | `Source_Page` column; exactness metrics |
| 8 | Validation and error detection | ✅ ⚠️ | `tests/test_validation.py`; precision/recall on a small dev set |
| 9 | Bounded retries with alternative strategies | ✅ | `max_attempts_per_page`, `test_attempts_are_bounded`, `test_strategy_error_triggers_fallback_strategy` |
| 10 | Flag unresolved uncertainty | ✅ | Unverified/Potential categories, `Complete (review needed)`, failed pages |
| 11 | Formatted Excel export (3 sheet types, frozen header, filters, formats) | ✅ | `tests/test_excel_export.py`, `samples/generated_outputs/` |
| 12 | Responsive web UI | ✅ ⚠️ | headless Chromium at 390 px and desktop; real devices not tested |
| 13 | Tests, docs, demo package | ✅ ⚠️ | demo **script** only — no video |
| – | Free, local, CPU-only, no paid API | ✅ | `requirements.txt` |
| – | ≤ 50 pages enforced | ✅ | `test_more_than_50_pages_rejected`; 50-page runs in `reports/` |

## Things only you can do
* ⬜ Run the Windows steps in `README.md` once on your laptop (`python scripts/run_tests.py`, `python scripts/run_benchmark.py`, `python app.py`) and note any difference. Python 3.11/3.12 + Tesseract for Windows are untested here.
* ⬜ Try 2–3 **real** (non-confidential) PDFs and look at the output — all measurements so far are on synthetic files.
* ⬜ Open the app on your phone (same Wi-Fi: set `DOCUPILOT_HOST=1` and use `http://<laptop-ip>:7860`, or deploy) and check upload/download on your actual browser.
* ⬜ Record the demo video with `docs/demo_script.md`.
* ⬜ (Optional) Deploy to Hugging Face Spaces following `docs/deployment.md` and test it; keep it private if you will upload anything sensitive.
* ⬜ Add a `LICENSE` of your choice before publishing the repository; replace the placeholder owner/contact details if the form asks for them.
* ⬜ Submit through the challenge's own channel before the deadline; confirm what file formats / links they require.

## Do not claim
Real-world accuracy · perfect OCR · Windows, phone or hosting testing that you have not done · INCUXAI endorsement · that a hosted copy deletes uploads immediately.
