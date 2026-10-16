# Deployment (free hosting) — preparation only

> Nothing has been deployed. No external account was used. The app works fully locally; hosting is optional and
> its compatibility is **untested** (see "What was and was not tested").

## Hugging Face Spaces (free CPU tier) — expected steps
1. Create a new Space, SDK **Gradio**, hardware *CPU basic*.
2. Upload the repository contents. The root `README.md` already carries the Spaces YAML header (`sdk: gradio`, `app_file: app.py`).
3. **Tesseract** is a system package: copy `deploy/packages.txt` to the **repository root** as `packages.txt` (Spaces installs the listed apt packages). Without it the app still runs, shows "OCR not available", and converts digital PDFs only.
4. `requirements.txt` is used for the Python packages. If the Space's Gradio version differs from `sdk_version` in the README header, adjust that line (the code supports Gradio 5 and 6).
5. Optional environment variables (Space *Settings → Variables*): `DOCUPILOT_MAX_PAGES`, `DOCUPILOT_MAX_SECONDS`, `DOCUPILOT_RETENTION_HOURS`.
`app.py` binds to `0.0.0.0` automatically when `SPACE_ID` is set and exposes `demo`.

## Resource limits to expect
* Free CPU Spaces are shared, small machines. OCR takes several seconds per page (measured here: see `reports/benchmark_summary.md`, 2 vCPU); a 50-page **scanned** document may approach the default 900 s budget on slower hardware — pages beyond the budget are marked failed and the workbook is marked PARTIAL.
* Spaces may sleep when idle and restart (generated files in the output folder are lost on restart).

## Privacy on a hosted deployment
* Uploaded PDFs are **processed by the hosting server**. Gradio stores uploads in its temp/cache folder; the platform may log requests. **Do not promise users that files are deleted immediately** — this app removes only its *own* private working copy after each conversion and its own generated outputs older than `DOCUPILOT_RETENTION_HOURS` (default 24 h, on the next conversion). Gradio's upload cache and platform logs are outside the app's control, and this deletion behaviour was verified only on a local Linux run.
* Use a hosted copy only for non-confidential documents, or keep the Space private.

## What was and was not tested
| Item | Status |
|---|---|
| Local run (Linux, Python 3.13, Gradio 6.30) incl. headless-browser upload→convert→download at phone and desktop widths | tested |
| `app.py` entry point / `demo` object / port + host handling | tested locally only |
| Actual deployment on Hugging Face Spaces (apt packages, Gradio version pin, resource limits) | **not tested** |
| Other hosts (Render, Railway, etc.) | not tested |
