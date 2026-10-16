#!/usr/bin/env python
"""Run the DocuPilot AI benchmark on synthetic documents and save REAL results.

Usage (from the project folder, venv active):
    python scripts/run_benchmark.py                 # all cases
    python scripts/run_benchmark.py --skip-ocr      # skip OCR cases (no Tesseract)
    python scripts/run_benchmark.py --stress        # add 50-page digital (and 50-page scanned) runs
    python scripts/run_benchmark.py --save-samples  # also keep PDFs + generated workbooks in samples/

Outputs: reports/benchmark_results.json, reports/benchmark_cases.csv, reports/benchmark_summary.md
Every number is computed from the pipeline's actual output; nothing is hard-coded.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dataclasses  # noqa: E402

import benchmark_lib as bl  # noqa: E402
import fixture_factory as ff  # noqa: E402
from config.settings import SETTINGS  # noqa: E402
from src import ocr_engine  # noqa: E402
from src.pipeline import convert_pdf  # noqa: E402
from src.validator import validate  # noqa: E402


def run_case(case, work: Path, out_dir: Path, settings, keep_dir: Path | None):
    pdf, expected = case.build(work)
    t0 = time.perf_counter()
    out = convert_pdf(pdf, pdf.name, settings=settings, out_dir=out_dir)
    elapsed = time.perf_counter() - t0
    rec = {"name": case.name, "doc_type": case.doc_type, "description": case.description, "known_weak": case.known_weak,
           "needs_ocr": case.needs_ocr, "pdf": pdf.name, "seconds": round(elapsed, 2), "pipeline_ok": out.ok}
    if not out.ok:
        rec.update(error=out.message)
        return rec, []
    r = out.result
    scores, tinfo = bl.evaluate_extraction(r, expected)
    agg = bl.aggregate(scores)
    ok_pages = sum(1 for p in r.page_results if p.status != "failed")
    rec.update(
        status=r.status, pages=r.total_pages, page_success_rate=round(ok_pages / r.total_pages, 4),
        pages_ocr=sum(1 for p in r.page_results if p.ocr_used),
        ocr_mean_conf=(round(sum(p.ocr_mean_conf for p in r.page_results if p.ocr_mean_conf is not None) / max(1, sum(1 for p in r.page_results if p.ocr_mean_conf is not None)), 1) if any(p.ocr_mean_conf is not None for p in r.page_results) else None),
        failed_pages=sum(1 for p in r.page_results if p.status == "failed"), tables=len(r.tables), records=r.n_records,
        expected_records=sum(len(x[1]) for x in expected), issues=len(r.issues),
        issues_error=sum(1 for i in r.issues if i.severity == "Error"), issues_warning=sum(1 for i in r.issues if i.severity == "Warning"),
        corrections=len(r.corrections), workbook=out.workbook_path.name, **tinfo, **agg,
    )
    if keep_dir:
        keep_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, keep_dir / pdf.name)
        gen = keep_dir / "generated_outputs"
        gen.mkdir(exist_ok=True)
        shutil.copy2(out.workbook_path, gen / f"{pdf.stem}.xlsx")
        shutil.copy2(out.validation_csv_path, gen / f"{pdf.stem}_validation.csv")
    return rec, scores


def stress(kind: str, work: Path, out_dir: Path, settings):
    """Exactly-50-page runs. Digital: 18 rows/page = 900 rows. Scanned: the same pages rasterised at 200 dpi.

    Expected rows are derived from what was PRINTED (pages x rows_per_page), never from what was extracted.
    """
    import pymupdf
    rows_per_page = 18
    rows = ff.invoice_dataset(50 * rows_per_page, seed=99)
    src = ff.make_table_pdf(work / "stress_src.pdf", ff.INVOICE_HEADERS, rows, rows_per_page=rows_per_page, grid=True)
    with pymupdf.open(str(src)) as d:
        n = d.page_count
        if n != 50:   # make it exactly 50 pages; expected rows follow from the pages kept
            d.select(list(range(min(n, 50))))
            d.save(str(work / "stress50.pdf"))
            n = min(n, 50)
            src = work / "stress50.pdf"
    printed = rows[: n * rows_per_page]
    pdf = src
    if kind == "scanned":
        pdf = ff.make_scanned_pdf(src, work / "stress50_scanned.pdf", dpi=200)
    t0 = time.perf_counter()
    out = convert_pdf(pdf, pdf.name, settings=settings, out_dir=out_dir)
    el = time.perf_counter() - t0
    rec = {"kind": kind, "pages": n, "expected_records": len(printed), "seconds": round(el, 1), "pipeline_ok": out.ok}
    if out.ok:
        r = out.result
        sc = bl.score_table(ff.INVOICE_HEADERS, printed, r.tables[0].headers if r.tables else [], r.tables[0].rows if r.tables else [])
        rec.update(status=r.status, tables=len(r.tables), records=r.n_records,
                   failed_pages=sum(1 for p in r.page_results if p.status == "failed"),
                   pages_ocr=sum(1 for p in r.page_results if p.ocr_used), seconds_per_page=round(el / n, 2),
                   memory="not measured", **bl.aggregate([sc]))
    else:
        rec["error"] = out.message
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports"))
    ap.add_argument("--skip-ocr", action="store_true")
    ap.add_argument("--stress", action="store_true")
    ap.add_argument("--save-samples", action="store_true")
    ap.add_argument("--only", nargs="*", help="run only these case names")
    ap.add_argument("--render-only", action="store_true", help="regenerate benchmark_summary.md from the saved JSON")
    ap.add_argument("--stress-only", action="store_true", help="re-run only the 50-page stress runs and update the existing results file")
    args = ap.parse_args()

    out_reports = Path(args.out); out_reports.mkdir(parents=True, exist_ok=True)
    st = ocr_engine.get_status(SETTINGS)
    env = {"python": sys.version.split()[0], "platform": platform.platform(), "cpu_count": os.cpu_count(),
           "tesseract": st.version if st.available else "NOT AVAILABLE", "date": time.strftime("%Y-%m-%d %H:%M:%S")}
    print("Environment:", env)

    if args.render_only:
        res_path = out_reports / "benchmark_results.json"
        (out_reports / "benchmark_summary.md").write_text(render_md(json.loads(res_path.read_text(encoding="utf-8"))), encoding="utf-8")
        print("Rendered", out_reports / "benchmark_summary.md")
        return 0

    if args.stress_only:
        res_path = out_reports / "benchmark_results.json"
        results = json.loads(res_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="docupilot_bench_") as td:
            td = Path(td); settings = dataclasses.replace(SETTINGS, output_dir=td / "out"); (td / "out").mkdir()
            runs = []
            for kind in ("digital", "scanned"):
                if kind == "scanned" and not st.available:
                    continue
                w = td / f"stress_{kind}"; w.mkdir(); print("stress", kind, "...", flush=True)
                runs.append(stress(kind, w, td / "out", settings)); print(runs[-1], flush=True)
        results["stress"] = runs
        results["environment_stress"] = env
        res_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        (out_reports / "benchmark_summary.md").write_text(render_md(results), encoding="utf-8")
        print("Updated stress results in", res_path)
        return 0

    records, all_scores = [], defaultdict(list)
    with tempfile.TemporaryDirectory(prefix="docupilot_bench_") as td:
        td = Path(td)
        settings = dataclasses.replace(SETTINGS, output_dir=td / "out")
        (td / "out").mkdir()
        keep = ROOT / "samples" if args.save_samples else None
        for case in bl.build_cases():
            if args.only and case.name not in args.only:
                continue
            if case.needs_ocr and (args.skip_ocr or not st.available):
                records.append({"name": case.name, "doc_type": case.doc_type, "skipped": "OCR not available/skipped"})
                print(f"SKIP {case.name}")
                continue
            work = td / case.name; work.mkdir()
            rec, scores = run_case(case, work, td / "out", settings, keep)
            records.append(rec)
            for s in scores:
                all_scores[case.doc_type].append(s)
                all_scores["ALL"].append(s)
            print(f"{case.name:26s} {rec.get('status', 'ERR'):9s} cell={rec.get('exact_cell_accuracy')} rows={rec.get('row_completeness')} "
                  f"align={rec.get('column_alignment')} t={rec['seconds']}s")

        # labelled validation precision/recall
        val = bl.score_validation(lambda t: validate([t], []))
        print("Validation:", {k: v for k, v in val.items() if k != "detail"})

        stress_runs = []
        if args.stress:
            for kind in ("digital", "scanned"):
                if kind == "scanned" and not st.available:
                    continue
                w = td / f"stress_{kind}"; w.mkdir()
                print("stress", kind, "...", flush=True)
                stress_runs.append(stress(kind, w, td / "out", settings))
                print(stress_runs[-1])

    by_type = {k: bl.aggregate(v) | {"tables": len(v)} for k, v in all_scores.items()}
    prev = {}
    prev_path = out_reports / "benchmark_results.json"
    if prev_path.exists() and not stress_runs:      # keep earlier 50-page stress results (extraction code unchanged since)
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    stress_runs = stress_runs or prev.get("stress", [])
    results = {"environment": env, "environment_stress": prev.get("environment_stress", env if stress_runs else None),
               "cases": records, "by_document_type": by_type, "validation_detection": val, "stress": stress_runs,
               "notes": ["All documents are synthetic; results do not establish real-world performance.",
                         "Cell accuracy counts exact (whitespace-normalised) string equality at the same column index.",
                         "Cases flagged known_weak document limitations and are included, not hidden."]}
    (out_reports / "benchmark_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    cols = ["name", "doc_type", "status", "pages", "page_success_rate", "tables", "expected_tables", "records", "expected_records",
            "exact_cell_accuracy", "numeric_cell_accuracy", "row_completeness", "exact_row_rate", "column_alignment", "header_exact_rate",
            "spurious_rows", "duplicate_header_rows_leaked", "issues", "issues_error", "issues_warning", "corrections", "ocr_mean_conf", "failed_pages", "seconds", "known_weak", "skipped", "error"]
    with open(out_reports / "benchmark_cases.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for r in records:
            w.writerow(r)
    (out_reports / "benchmark_summary.md").write_text(render_md(results), encoding="utf-8")
    print("Saved:", out_reports / "benchmark_results.json")
    return 0


def pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render_md(res) -> str:
    L = ["# DocuPilot AI - benchmark results (generated by scripts/run_benchmark.py)", "",
         f"Environment: {res['environment']}", "",
         "**All documents are synthetic. These numbers do not establish performance on real-world documents.**", "",
         "## Per document", "", "| Case | Type | Status | Cell acc. | Numeric acc. | Row compl. | Col. align | Spurious rows | Time (s) |", "|---|---|---|---|---|---|---|---|---|"]
    for r in res["cases"]:
        if r.get("skipped"):
            L.append(f"| {r['name']} | {r['doc_type']} | skipped | | | | | | |"); continue
        if not r.get("pipeline_ok"):
            L.append(f"| {r['name']} | {r['doc_type']} | ERROR: {r.get('error')} | | | | | | {r['seconds']} |"); continue
        L.append(f"| {r['name']}{' (known weak)' if r['known_weak'] else ''} | {r['doc_type']} | {r['status']} | {pct(r['exact_cell_accuracy'])} | "
                 f"{pct(r['numeric_cell_accuracy'])} | {pct(r['row_completeness'])} | {pct(r['column_alignment'])} | {r['spurious_rows']} | {r['seconds']} |")
    L += ["", "## By document type (micro-average over tables)", "", "| Type | Tables | Cell acc. | Numeric acc. | Row compl. | Col. align | Header exact | Dup. headers leaked |", "|---|---|---|---|---|---|---|---|"]
    order = sorted(res["by_document_type"].items(), key=lambda kv: (kv[0] == "ALL", kv[0]))
    for k, v in order:
        L.append(f"| {k} | {v['tables']} | {pct(v['exact_cell_accuracy'])} | {pct(v['numeric_cell_accuracy'])} | {pct(v['row_completeness'])} | "
                 f"{pct(v['column_alignment'])} | {pct(v['header_exact_rate'])} | {v['duplicate_header_rows_leaked']} |")
    v = res["validation_detection"]
    L += ["", "## Validation error detection (labelled injected anomalies)", "",
          f"Cases: {v['cases']} | TP {v['true_positives']} | FP {v['false_positives']} | FN {v['false_negatives']} | precision {pct(v['precision'])} | recall {pct(v['recall'])}"]
    if res["stress"]:
        L += ["", "## 50-page stress runs (one synthetic 12-column invoice layout, 18 rows/page)", "",
              "| Kind | Pages | Expected rows | Extracted rows | Failed pages | Status | Total time (s) | s / page | Cell acc. | Numeric acc. | Row compl. | Exact rows | Spurious rows |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for s in res["stress"]:
            if not s.get("pipeline_ok"):
                L.append(f"| {s['kind']} | {s['pages']} | | | | ERROR: {s.get('error')} | {s['seconds']} | | | | | | |"); continue
            L.append(f"| {s['kind']} | {s['pages']} | {s.get('expected_records', '?')} | {s['records']} | {s['failed_pages']} | {s['status']} | {s['seconds']} | "
                     f"{s['seconds_per_page']} | {pct(s['exact_cell_accuracy'])} | {pct(s['numeric_cell_accuracy'])} | {pct(s['row_completeness'])} | "
                     f"{pct(s['exact_row_rate'])} | {s['spurious_rows']} |")
        L += ["", "Memory use was not measured."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
