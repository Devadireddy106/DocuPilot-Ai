#!/usr/bin/env python
"""Run the pytest suite and save a machine-readable record of what passed / failed / was skipped.

Usage (project folder, venv active):   python scripts/run_tests.py
Outputs: reports/test_results.json, reports/test_results.txt   (exit code 0 only if nothing failed)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    xml_path = reports / "_junit.xml"
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={xml_path}", "-rs", "--durations=5"],
                          cwd=ROOT, capture_output=True, text=True)
    elapsed = round(time.time() - t0, 1)
    sys.stdout.write(proc.stdout[-4000:])
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr[-2000:])

    cases, counts = [], {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    if xml_path.exists():
        root = ET.parse(xml_path).getroot()
        for tc in root.iter("testcase"):
            name = f"{tc.get('classname')}::{tc.get('name')}"
            status, detail = "passed", ""
            for tag, st in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
                el = tc.find(tag)
                if el is not None:
                    status, detail = st, (el.get("message") or "")[:300]
                    break
            counts[status] += 1
            cases.append({"test": name, "status": status, "seconds": float(tc.get("time", 0)), "detail": detail})
        xml_path.unlink()

    try:
        import pymupdf, pdfplumber, pytesseract, openpyxl, gradio  # noqa: F401
        versions = {"python": sys.version.split()[0], "pymupdf": pymupdf.VersionBind, "pdfplumber": pdfplumber.__version__,
                    "openpyxl": openpyxl.__version__, "gradio": gradio.__version__}
    except Exception:
        versions = {"python": sys.version.split()[0]}
    from src import ocr_engine
    st = ocr_engine.get_status()
    versions["tesseract"] = st.version if st.available else "NOT AVAILABLE (OCR tests skipped)"

    result = {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed_seconds": elapsed, "exit_code": proc.returncode,
              "counts": counts, "total": len(cases), "versions": versions, "tests": cases}
    (reports / "test_results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    lines = [f"DocuPilot AI test run - {result['date']}", f"Environment: {versions}",
             f"Total {len(cases)} | passed {counts['passed']} | failed {counts['failed']} | errors {counts['error']} | skipped {counts['skipped']} | {elapsed}s", ""]
    for c in cases:
        if c["status"] != "passed":
            lines.append(f"{c['status'].upper():8s} {c['test']}  {c['detail']}")
    (reports / "test_results.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + lines[2])
    return 0 if counts["failed"] == 0 and counts["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
