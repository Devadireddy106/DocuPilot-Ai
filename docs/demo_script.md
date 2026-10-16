# Demo script (about 4 minutes)

> **A video has NOT been recorded** — this environment cannot record screen/audio. Use this script to record one
> (Windows: *Win+G* Game Bar, or OBS Studio, free). Everything below uses files in `samples/`; run
> `python scripts/run_benchmark.py --save-samples` first if you want fresh outputs.

**Setup (before recording):** activate the venv, `python app.py`, open <http://127.0.0.1:7860> in a browser window (or phone-sized window).
Have `samples/invoice_ruled.pdf`, `samples/bank_unruled.pdf`, `samples/invoice_scanned.pdf`, `samples/mixed_digital_scanned.pdf`,
`samples/bank_scanned_degraded.pdf` ready.

| Time | Show | Say |
|---|---|---|
| 0:00 | Landing page, OCR-ready banner | "DocuPilot AI turns PDFs — digital or scanned — into Excel you can trust. Free, local, CPU-only. This is an independent project for the INCUXAI challenge." |
| 0:20 | Upload `invoice_ruled.pdf` → **Convert** | "12 columns across two pages. Watch the stages: they are the backend's real progress, not an animation." |
| 0:50 | Preview, scroll sideways; open workbook: frozen header, filters, Source_Page | "Repeated headers removed, rows kept in order, every row knows its source page." |
| 1:20 | Upload `bank_unruled.pdf` | "No ruling lines, a title above the table, blank debit/credit cells. The agent found the columns from word geometry." |
| 1:50 | Upload `invoice_scanned.pdf` | "Scanned: no text layer. The agent classifies the page, removes the table borders, runs Tesseract, and flags OCR output as **unverified**." Show Validation_Report `ocr_unverified`, and an `Auto-corrected` row if present. |
| 2:30 | Upload `mixed_digital_scanned.pdf` | "Page 1 is digital, page 2 is scanned — decided per page — and the table is rebuilt across both." |
| 3:00 | Upload `bank_scanned_degraded.pdf` | "A scan that is too poor. It does **not** guess: the page is reported as failed, status is not 'Complete', and no garbage appears as data." |
| 3:25 | Show `reports/benchmark_summary.md` and `reports/test_results.txt` | "Real numbers from the repo's own scripts — synthetic documents, so they show behaviour, not real-world accuracy. Failures are listed, not hidden." |
| 3:50 | Close | "Honest by design: uncertainty is flagged, partial results are marked partial, nothing is silently changed. From documents to trusted data." |

**Do not claim:** real-world accuracy, perfect OCR, Windows/phone testing that you have not done, or INCUXAI endorsement.
