"""DocuPilot AI - Intelligent PDF-to-Excel Agent.  From documents to trusted data.

Run locally:   python app.py        (then open http://127.0.0.1:7860)
Hugging Face Spaces / other Gradio hosts use this file as the entry point (`demo` is exposed).

All PDF and OCR processing happens on the machine running this app, never on the phone/browser.
Independent project - not endorsed by or affiliated with INCUXAI.
"""
from __future__ import annotations

import html
import inspect
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import gradio as gr
import pandas as pd

from config.settings import SETTINGS
from src import ocr_engine, processing_report
from src.pipeline import PipelineOutput, convert_pdf, issues_frame

logging.basicConfig(level=os.environ.get("DOCUPILOT_LOG", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("docupilot.app")

STAGES = ["Checking the file", "Reading pages", "Rebuilding tables", "Checking data quality", "Writing Excel", "Done"]

CSS = """
:root { --navy:#0b1f3a; --blue:#1d4ed8; --blue-dark:#1e40af; --emerald:#059669; --bg:#f4f6fa; --card:#ffffff; --line:#d7dde8; --muted:#475569; }
body, .gradio-container { background: var(--bg) !important; color: var(--navy) !important; }
.gradio-container { max-width: 1100px !important; margin: 0 auto !important; padding: 0 16px !important; }
.dp-header { padding: 22px 4px 6px; }
.dp-brand { font-size: clamp(1.6rem, 5vw, 2.3rem); font-weight: 800; color: var(--navy); letter-spacing: -0.02em; margin: 0; }
.dp-brand span { color: var(--blue); }
.dp-sub { font-size: clamp(1rem, 3.2vw, 1.2rem); color: var(--navy); margin: 2px 0 0; font-weight: 600; }
.dp-tag { color: var(--emerald); font-weight: 700; margin: 2px 0 0; }
.dp-card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px; box-shadow: 0 1px 2px rgba(11,31,58,.05); }
.dp-note { font-size: .9rem; color: var(--muted); }
button.primary, .dp-convert button { background: var(--blue) !important; color: #fff !important; border: 0 !important; min-height: 52px !important; font-size: 1.05rem !important; font-weight: 700 !important; border-radius: 12px !important; }
button.primary:hover, .dp-convert button:hover { background: var(--blue-dark) !important; }
button, .gr-button { min-height: 44px; }
.dp-stage-wrap { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px; }
.dp-bar { height: 12px; background: #e2e8f0; border-radius: 99px; overflow: hidden; margin: 10px 0 6px; }
.dp-bar > div { height: 100%; background: var(--blue); border-radius: 99px; transition: width .25s ease; }
.dp-bar.done > div { background: var(--emerald); }
.dp-bar.fail > div { background: #b91c1c; }
.dp-steps { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 0; margin: 8px 0 0; list-style: none; font-size: .92rem; }
.dp-steps li { color: #64748b; } .dp-steps li.on { color: var(--blue); font-weight: 700; } .dp-steps li.ok { color: var(--emerald); font-weight: 700; }
.dp-msg { font-size: .95rem; color: var(--navy); }
.dp-err { color: #991b1b; font-weight: 600; }
.dp-table .table-wrap, .dp-table { overflow-x: auto !important; -webkit-overflow-scrolling: touch; }
footer { opacity: .7; }
.gradio-container, .gradio-container .main, .gradio-container main, .gradio-container .wrap, .gradio-container .column, .gradio-container .block, .gradio-container .form, .dp-card, .dp-table { min-width: 0 !important; max-width: 100% !important; }
.dp-table { overflow-x: auto !important; display: block; }
html, body { overflow-x: hidden; max-width: 100vw; }
@media (max-width: 640px) { .gradio-container { padding: 0 10px !important; } .dp-card { padding: 10px; } }
"""

FORCE_LIGHT_JS = """
() => {
  try {
    const url = new URL(window.location);
    if (url.searchParams.get('__theme') !== 'light') { url.searchParams.set('__theme', 'light'); window.location.replace(url); }
  } catch (e) {}
}
"""

HEADER_HTML = f"""
<div class="dp-header">
  <h1 class="dp-brand">DocuPilot <span>AI</span></h1>
  <p class="dp-sub">{html.escape(SETTINGS.subtitle)}</p>
  <p class="dp-tag">{html.escape(SETTINGS.tagline)}</p>
</div>
"""

PRIVACY_MD = f"""
**Privacy notice.** Your PDF is processed by the computer that runs this app. If you are using a *hosted* copy,
that is the hosting server: your file is uploaded to it, a private working copy is created for conversion and removed
afterwards, and the generated Excel/CSV/JSON files are kept in the server's output folder for up to
{SETTINGS.output_retention_hours} hours before being cleaned up on a later run. The hosting platform may keep its own
upload cache or logs that this app cannot control, so **do not upload confidential documents to a shared/hosted copy**.
Nothing is sent to any third-party API: OCR and extraction run locally with open-source tools.
"""

HOWTO_MD = """
1. Upload a PDF (up to 50 pages). Digital and scanned pages both work.
2. Tap **Convert to Excel** and watch the real processing stages.
3. Check the preview and the validation list. Yellow rows in the workbook need a human look.
4. Download the workbook (and the validation report).

**Good to know**
- Scanned pages are read with OCR. OCR values are *never* marked as verified: spot-check numbers.
- Cells are never silently changed. The few automatic fixes (e.g. a stray border character stuck to a number) are listed in the Validation_Report.
- Quality numbers are heuristics, not probabilities.
"""

LIMITS_MD = """
- Maximum 50 pages and 50 MB per PDF; password-protected PDFs are rejected.
- Cell text that wraps over several lines may become several rows (flagged when detectable).
- Very narrow column gaps (< ~0.6 line-heights) cannot be told apart.
- OCR accuracy depends on scan quality; tiny fonts, skew, and low contrast reduce it.
- Tested only on synthetic documents so far (see docs/known_limitations.md).
"""


def stage_html(frac: float, msg: str, state: str = "run") -> str:
    if state == "fail":
        idx = -1
    elif frac >= 1.0:
        idx = len(STAGES) - 1
    elif frac < 0.05:
        idx = 0
    elif frac < 0.84:
        idx = 1
    elif frac < 0.90:
        idx = 2
    elif frac < 0.95:
        idx = 3
    else:
        idx = 4
    items = []
    for i, name in enumerate(STAGES):
        cls = "ok" if (idx > i or state == "done") else "on" if i == idx else ""
        mark = "✓ " if cls == "ok" else "● " if cls == "on" else "○ "
        items.append(f'<li class="{cls}">{mark}{html.escape(name)}</li>')
    bar_cls = {"done": "done", "fail": "fail"}.get(state, "")
    pct = 100 if state == "done" else max(2, int(frac * 100))
    msg_cls = "dp-msg dp-err" if state == "fail" else "dp-msg"
    return (
        f'<div class="dp-stage-wrap"><div class="{msg_cls}">{html.escape(msg)}</div>'
        f'<div class="dp-bar {bar_cls}"><div style="width:{pct}%"></div></div>'
        f'<ul class="dp-steps">{"".join(items)}</ul></div>'
    )


def _noop(n: int):
    return [gr.update() for _ in range(n)]


N_OUT = 10  # status, summary, table_dd, preview, issues, xlsx, csv, log, state, results panel


def convert(file_path: Optional[str], progress=gr.Progress()):
    if not file_path:
        yield [stage_html(0, "Please choose a PDF file first.", "fail")] + _noop(N_OUT - 1)
        return

    original = Path(file_path).name
    q: "queue.Queue[tuple]" = queue.Queue()
    holder: Dict[str, Any] = {}

    def worker() -> None:
        try:
            holder["out"] = convert_pdf(file_path, original, progress=lambda f, m: q.put((f, m)))
        except Exception as exc:  # pragma: no cover - convert_pdf already guards
            log.exception("worker crashed")
            holder["out"] = PipelineOutput(False, f"Unexpected error ({type(exc).__name__}). Please try again.")
        finally:
            q.put(None)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    yield [stage_html(0.0, "Starting…")] + _noop(N_OUT - 2) + [gr.update(visible=False)]

    last = (0.0, "Starting…")
    while True:
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if item is None:
            break
        last = item
        progress(item[0], desc=item[1])
        yield [stage_html(*item)] + _noop(N_OUT - 1)
    th.join(timeout=5)

    out: PipelineOutput = holder.get("out") or PipelineOutput(False, "No result was produced.")
    if not out.ok:
        yield [stage_html(last[0], out.message, "fail"), gr.update(value=f"### ❌ Could not convert\n{out.message}"),
               gr.update(visible=False), gr.update(value=None), gr.update(value=None),
               gr.update(value=None), gr.update(value=None), gr.update(value=None), {}, gr.update(visible=False)]
        return

    res = out.result
    names = list(out.previews.keys())
    first = names[0] if names else None
    preview = out.previews[first] if first else pd.DataFrame({"Message": ["No tables were extracted. See the validation list."]})
    done_msg = {
        "Complete": "Conversion finished.",
        "Partial": "Finished with PARTIAL results - see the warnings below.",
        "Complete (review needed)": "Finished - some pages need review (see warnings below).",
    }.get(res.status, f"Finished: {res.status}.")
    yield [
        stage_html(1.0, done_msg, "done"),
        processing_report.plain_summary(res),
        gr.update(choices=names, value=first, visible=len(names) > 1),
        preview,
        issues_frame(res),
        str(out.workbook_path), str(out.validation_csv_path), str(out.log_path),
        out.previews, gr.update(visible=True),
    ]


def switch_table(name: str, previews: Dict[str, pd.DataFrame]):
    if previews and name in previews:
        return previews[name]
    return gr.update()


def ocr_banner() -> str:
    st = ocr_engine.get_status(SETTINGS)
    if st.available:
        return f"OCR ready (Tesseract {st.version}). Scanned PDFs are supported."
    return ("⚠️ OCR is not available on this server, so scanned pages cannot be read (digital PDFs still work). "
            + st.message)


def build_ui() -> gr.Blocks:
    kwargs: Dict[str, Any] = {"title": "DocuPilot AI - PDF to Excel"}
    sig = inspect.signature(gr.Blocks.__init__).parameters
    if "css" in sig:          # Gradio 5.x
        kwargs["css"] = CSS
    if "js" in sig:
        kwargs["js"] = FORCE_LIGHT_JS
    with gr.Blocks(**kwargs) as demo:
        gr.HTML(HEADER_HTML)
        with gr.Group(elem_classes="dp-card"):
            file_in = gr.File(label="Upload a PDF (digital or scanned, up to 50 pages)", file_types=[".pdf"],
                              type="filepath", file_count="single")
            convert_btn = gr.Button("Convert to Excel", variant="primary", elem_classes="dp-convert")
            gr.Markdown(ocr_banner(), elem_classes="dp-note")
        status = gr.HTML(value="", label="Progress")
        summary = gr.Markdown(value="")
        state = gr.State({})

        with gr.Column(visible=False) as results:
            gr.Markdown("### Preview of extracted records")
            table_dd = gr.Dropdown(label="Table", choices=[], visible=False, interactive=True)
            preview = gr.Dataframe(label="Extracted records (scroll sideways for wide tables)", interactive=False,
                                   wrap=False, max_height=420, elem_classes="dp-table")
            gr.Markdown("### Validation warnings")
            issues = gr.Dataframe(label="Findings (Errors first). Nothing is hidden or silently changed.",
                                  interactive=False, wrap=True, max_height=380, elem_classes="dp-table")
            gr.Markdown("### Downloads")
            with gr.Row():
                xlsx_out = gr.File(label="Excel workbook (.xlsx)", interactive=False)
                csv_out = gr.File(label="Validation report (.csv)", interactive=False)
                log_out = gr.File(label="Processing log (.json, no cell values)", interactive=False)

        with gr.Accordion("How it works", open=False):
            gr.Markdown(HOWTO_MD)
        with gr.Accordion("Privacy notice", open=False):
            gr.Markdown(PRIVACY_MD)
        with gr.Accordion("Known limitations", open=False):
            gr.Markdown(LIMITS_MD)
        gr.Markdown("<span class='dp-note'>DocuPilot AI is an independent project built for the INCUXAI AI Agent "
                    "Development Challenge 2026. It is not endorsed by or affiliated with INCUXAI.</span>")

        outputs = [status, summary, table_dd, preview, issues, xlsx_out, csv_out, log_out, state, results]
        convert_btn.click(convert, inputs=[file_in], outputs=outputs, show_progress="hidden")
        table_dd.change(switch_table, inputs=[table_dd, state], outputs=[preview])
    demo.queue(max_size=8, default_concurrency_limit=2)
    return demo


demo = build_ui()


def main() -> None:
    launch_kwargs: Dict[str, Any] = {
        "server_name": "0.0.0.0" if (os.environ.get("SPACE_ID") or os.environ.get("DOCUPILOT_HOST")) else "127.0.0.1",
        "max_file_size": f"{SETTINGS.max_file_mb}mb",
        "show_error": False,
    }
    sig = inspect.signature(gr.Blocks.launch).parameters
    if "css" in sig:          # Gradio 6.x moved these to launch()
        launch_kwargs["css"] = CSS
    if "js" in sig:
        launch_kwargs["js"] = FORCE_LIGHT_JS
    if "allowed_paths" in sig:
        SETTINGS.output_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs["allowed_paths"] = [str(SETTINGS.output_dir)]
    demo.launch(**launch_kwargs)


if __name__ == "__main__":
    main()
