"""Plain data containers shared by all pipeline stages.

Keeping them in one place makes the agent's state inspectable and easy to serialise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

BBox = Tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points (top-left origin)


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: Optional[float] = None  # OCR engine word confidence 0-100 (not a calibrated probability)


@dataclass
class PageMetrics:
    page: int
    width: float = 0.0
    height: float = 0.0
    n_chars: int = 0
    n_words: int = 0
    n_images: int = 0
    image_coverage: float = 0.0
    n_drawings: int = 0
    garbled_ratio: float = 0.0


@dataclass
class Attempt:
    """One extraction strategy tried on one page."""
    strategy: str
    success: bool
    reason: str
    n_tables: int = 0
    n_rows: int = 0
    score: float = 0.0          # heuristic score 0..1 (NOT a probability)
    duration_s: float = 0.0
    mean_ocr_conf: Optional[float] = None


@dataclass
class RawTable:
    """A table exactly as one strategy found it on one page (before reconstruction)."""
    page: int
    index: int
    rows: List[List[str]]
    method: str
    bbox: Optional[BBox] = None
    page_height: Optional[float] = None
    row_conf: List[Optional[float]] = field(default_factory=list)
    row_y: List[Optional[Tuple[float, float]]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        n = len(self.rows)
        if len(self.row_conf) != n:
            self.row_conf = (list(self.row_conf) + [None] * n)[:n]
        if len(self.row_y) != n:
            self.row_y = (list(self.row_y) + [None] * n)[:n]


@dataclass
class PageResult:
    page: int
    kind: str = "unknown"                 # digital | mixed | scanned | blank | unreliable_text
    status: str = "pending"               # ok | low_quality | no_tables | blank | failed
    method: str = ""                      # strategy whose output was kept
    score: float = 0.0
    metrics: Optional[PageMetrics] = None
    plan: List[str] = field(default_factory=list)
    attempts: List[Attempt] = field(default_factory=list)
    decision_log: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    failure_reason: str = ""
    tables: List[RawTable] = field(default_factory=list)
    ocr_used: bool = False
    ocr_attempted: bool = False
    ocr_mean_conf: Optional[float] = None
    n_words_seen: int = 0

    def log(self, msg: str) -> None:
        self.decision_log.append(msg)

    @property
    def succeeded(self) -> bool:
        return self.status in ("ok", "no_tables", "blank")


@dataclass
class Table:
    """A reconstructed table (possibly spanning several pages)."""
    table_id: str
    headers: List[str]
    rows: List[List[str]]
    row_pages: List[int]
    row_methods: List[str]
    row_conf: List[Optional[float]]
    row_y: List[Optional[Tuple[float, float]]]
    header_inferred: bool = False
    header_page: Optional[int] = None
    pages: List[int] = field(default_factory=list)
    sheet_name: str = ""
    notes: List[str] = field(default_factory=list)
    row_notes: Dict[int, List[str]] = field(default_factory=dict)   # 0-based data row -> notes
    possible_continuation_of: Optional[str] = None
    bbox: Optional[BBox] = None
    page_height: Optional[float] = None
    last_row_bottom_ratio: Optional[float] = None

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.headers)


@dataclass
class Issue:
    """One validation finding.

    severity: Error | Warning | Info
    category: Confirmed (a hard invariant is violated) | Potential (suspicious, needs a human look)
              | Unverified (nothing is known to be wrong but the value was not independently checked)
    """
    issue_id: str
    severity: str
    category: str
    check: str
    description: str
    action: str
    page: Optional[int] = None
    table_id: Optional[str] = None
    sheet: Optional[str] = None
    row: Optional[int] = None         # 1-based data row within the table
    excel_row: Optional[int] = None   # row number in the worksheet (header is row 1)
    column: Optional[str] = None
    status: str = "Open"


@dataclass
class Correction:
    """A deterministic automatic change. Nothing is changed without one of these records."""
    table_id: str
    row: Optional[int]
    column: Optional[str]
    original: str
    corrected: str
    rule: str
    reason: str
    page: Optional[int] = None


@dataclass
class ExtractionResult:
    filename: str
    sha256: str
    total_pages: int
    page_results: List[PageResult] = field(default_factory=list)
    tables: List[Table] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    corrections: List[Correction] = field(default_factory=list)
    duration_s: float = 0.0
    status: str = "Failed"              # Complete | Partial | No tables found | Failed
    status_detail: str = ""
    ocr_available: bool = False
    ocr_version: str = ""
    doc_kind: str = "unknown"
    warnings: List[str] = field(default_factory=list)
    aborted_for_time: bool = False
    tool_versions: Dict[str, str] = field(default_factory=dict)

    @property
    def n_records(self) -> int:
        return sum(t.n_rows for t in self.tables)

    @property
    def is_partial(self) -> bool:
        return self.status == "Partial"

    def to_log_dict(self) -> Dict[str, Any]:
        """JSON-serialisable processing log (no cell values - keeps private content out of logs)."""
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "total_pages": self.total_pages,
            "status": self.status,
            "status_detail": self.status_detail,
            "doc_kind": self.doc_kind,
            "duration_s": round(self.duration_s, 3),
            "ocr_available": self.ocr_available,
            "ocr_version": self.ocr_version,
            "warnings": self.warnings,
            "tool_versions": self.tool_versions,
            "pages": [
                {
                    "page": p.page,
                    "kind": p.kind,
                    "status": p.status,
                    "method": p.method,
                    "heuristic_score": round(p.score, 3),
                    "failure_reason": p.failure_reason,
                    "ocr_used": p.ocr_used,
                    "ocr_mean_word_confidence": None if p.ocr_mean_conf is None else round(p.ocr_mean_conf, 1),
                    "plan": p.plan,
                    "attempts": [
                        {
                            "strategy": a.strategy,
                            "success": a.success,
                            "reason": a.reason,
                            "tables": a.n_tables,
                            "rows": a.n_rows,
                            "heuristic_score": round(a.score, 3),
                            "duration_s": round(a.duration_s, 3),
                        }
                        for a in p.attempts
                    ],
                    "decision_log": p.decision_log,
                    "warnings": p.warnings,
                }
                for p in self.page_results
            ],
            "tables": [
                {
                    "table_id": t.table_id,
                    "sheet": t.sheet_name,
                    "pages": t.pages,
                    "rows": t.n_rows,
                    "columns": t.n_cols,
                    "headers": t.headers,
                    "header_inferred": t.header_inferred,
                    "notes": t.notes,
                }
                for t in self.tables
            ],
            "issue_counts": _count_by(self.issues),
            "corrections": [
                {"table": c.table_id, "row": c.row, "column": c.column, "rule": c.rule, "reason": c.reason, "page": c.page}
                for c in self.corrections
            ],
        }


def _count_by(issues: List[Issue]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in issues:
        out[i.severity] = out.get(i.severity, 0) + 1
    out["total"] = len(issues)
    return out
