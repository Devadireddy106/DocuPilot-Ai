"""End-to-end pipeline used by the web app and the scripts: PDF in -> workbook + reports out."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Union

import pandas as pd

from config.settings import SETTINGS, Settings
from src import file_manager, processing_report
from src.excel_exporter import export_workbook
from src.extraction_agent import ExtractionAgent
from src.models import ExtractionResult
from src.pdf_preflight import PreflightError

log = logging.getLogger(__name__)
ProgressFn = Callable[[float, str], None]


@dataclass
class PipelineOutput:
    ok: bool
    message: str = ""                       # friendly error when ok is False
    result: Optional[ExtractionResult] = None
    workbook_path: Optional[Path] = None
    validation_csv_path: Optional[Path] = None
    log_path: Optional[Path] = None
    previews: dict = field(default_factory=dict)    # sheet name -> DataFrame (first rows)


def preview_frames(result: ExtractionResult, max_rows: int = 500) -> dict:
    frames = {}
    for t in result.tables:
        df = pd.DataFrame(t.rows[:max_rows], columns=t.headers)
        df.insert(len(df.columns), "Source_Page", t.row_pages[:max_rows])
        frames[t.sheet_name] = df
    return frames


def issues_frame(result: ExtractionResult) -> pd.DataFrame:
    cols = ["ID", "Severity", "Category", "Page", "Sheet", "Row", "Column", "Description", "Suggested action", "Status"]
    rows = [
        [i.issue_id, i.severity, i.category, i.page or "", i.sheet or "", i.excel_row or "", i.column or "",
         i.description, i.action, i.status]
        for i in result.issues
    ]
    return pd.DataFrame(rows, columns=cols)


def convert_pdf(
    pdf_path: Union[str, Path],
    original_name: Optional[str] = None,
    progress: Optional[ProgressFn] = None,
    settings: Settings = SETTINGS,
    out_dir: Optional[Path] = None,
) -> PipelineOutput:
    """Run the full conversion. Never raises for bad input: returns ok=False with a friendly message."""
    out_dir = Path(out_dir or settings.output_dir)
    shown = file_manager.display_name(original_name or Path(pdf_path).name)

    def emit(f: float, m: str) -> None:
        if progress:
            try:
                progress(f, m)
            except Exception:
                log.debug("progress callback failed", exc_info=True)

    try:
        file_manager.purge_old_outputs(out_dir, settings.output_retention_hours)
    except Exception:  # best effort
        log.debug("purge failed", exc_info=True)

    try:
        with file_manager.workspace() as work:
            staged = file_manager.stage_upload(pdf_path, work)  # private copy under a fixed safe name
            agent = ExtractionAgent(settings, emit)
            result = agent.run(staged, shown)
    except PreflightError as exc:
        log.info("Rejected upload: %s", exc.code)
        return PipelineOutput(False, exc.user_message)
    except Exception as exc:
        log.exception("Unexpected failure during extraction")
        return PipelineOutput(False, f"Something went wrong while reading this PDF ({type(exc).__name__}). "
                                     "Please try again or try a different file.")

    emit(0.95, "Writing the Excel workbook")
    try:
        xlsx = export_workbook(result, out_dir, settings)
    except Exception as exc:
        log.exception("Workbook export failed")
        return PipelineOutput(False, f"The data was extracted but the Excel file could not be created: {exc}", result=result)

    base = xlsx.stem
    csv_path = processing_report.write_validation_csv(result, out_dir, base + "_validation")
    log_path = processing_report.write_processing_log(result, out_dir, base + "_log")
    emit(1.0, "Done")
    return PipelineOutput(True, "", result, xlsx, csv_path, log_path, preview_frames(result))
