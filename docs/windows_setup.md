# Windows setup guide (VS Code, CPU-only)

> **Status:** prepared from standard Windows tooling. The project was built and tested on **Linux**; the
> Windows commands below have **not been executed on a Windows machine** by the author. Treat them as the
> expected path and report any difference.

## Prerequisites
* Windows 10/11, ~1 GB free disk.
* Python 3.11 or 3.12 from <https://www.python.org/downloads/> (tick "Add python.exe to PATH"). Developed/tested on Python 3.13 (Linux); 3.11/3.12 are expected to work but are untested.
* VS Code with the *Python* extension (optional but recommended).

## Step by step (PowerShell)
```powershell
# 1  Extract the ZIP
Expand-Archive -Path "$HOME\Downloads\DocuPilot_AI.zip" -DestinationPath "$HOME\Documents" -Force
# 2  Open the project folder
cd "$HOME\Documents\docupilot-ai"
code .
# 3  Virtual environment
py -3.12 -m venv .venv          # or: py -3.11 -m venv .venv   /   python -m venv .venv
# 4  Activate
.\.venv\Scripts\Activate.ps1
# 5  Dependencies
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
# 6  Tesseract (separate program!)
winget install --id UB-Mannheim.TesseractOCR -e
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
# 7  Tests
python scripts/run_tests.py
# 8  Benchmark
python scripts/run_benchmark.py
# 9  Launch
python app.py        # open http://127.0.0.1:7860
```

## Why two things for OCR?
`pytesseract` (installed by pip) is only a thin Python wrapper. The actual OCR engine is the **Tesseract executable**,
installed separately. DocuPilot looks for it, in order: the `TESSERACT_CMD` environment variable, `PATH`,
`%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe`, `C:\Program Files\Tesseract-OCR\tesseract.exe`,
`C:\Program Files (x86)\Tesseract-OCR\tesseract.exe`. Without it, **digital PDFs still convert**, and scanned pages are
reported as failed with install instructions (never silently skipped).

## Debugging in VS Code
`.vscode/launch.json` provides three launch configurations (run app, run tests, run benchmark). Select the interpreter
`.venv\Scripts\python.exe` (Ctrl+Shift+P → *Python: Select Interpreter*). `.vscode/settings.json` enables pytest discovery.

## Common problems
| Symptom | Fix |
|---|---|
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again |
| `py` is not recognised | Install Python from python.org (includes the `py` launcher) or use `python -m venv .venv` |
| App banner: "OCR is not available" | Install Tesseract (step 6); restart the app; or `$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"` |
| `TesseractNotFoundError` in logs | Same as above — the executable is missing or not on PATH |
| `pip install` fails building a package | Use Python 3.11/3.12 (64-bit); upgrade pip; PyMuPDF/OpenCV/NumPy ship wheels for these versions |
| Port 7860 already in use | `$env:GRADIO_SERVER_PORT = 7861; python app.py` |
| Browser shows a dark page | The app forces the light theme; add `?__theme=light` to the URL if needed |
| Very slow on a big scanned PDF | OCR costs several seconds per page on CPU; lower the DPI: `$env:DOCUPILOT_OCR_DPI = 220` (less accurate) |
| "The PDF has N pages. The maximum supported is 50" | Split the PDF first; the limit is intentional |
| Antivirus quarantines `tesseract.exe` | Re-install from the official UB-Mannheim page and whitelist the folder |

## Environment variables
`DOCUPILOT_MAX_PAGES` (≤ 50), `DOCUPILOT_MAX_FILE_MB`, `DOCUPILOT_OCR_DPI`, `DOCUPILOT_MAX_SECONDS`,
`DOCUPILOT_RETENTION_HOURS`, `DOCUPILOT_OUTPUT_DIR`, `TESSERACT_CMD`, `GRADIO_SERVER_PORT`, `DOCUPILOT_LOG`.
