"""Cross-page table reconstruction.

Takes the per-page raw tables chosen by the extraction agent and produces final tables:
* removes page furniture (page numbers, repeated running headers/footers)
* detects headers; removes repeated header rows
* merges tables that continue across *consecutive* pages - conservatively
* keeps tables separate (and flags them) when the relationship is uncertain
* never invents header text that implies facts (neutral ``Column_N`` only)
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from config.settings import SETTINGS, Settings
from src.models import PageResult, RawTable, Table
from src.pdf_extractor import clean_text
from src.schema_normalizer import (
    infer_column_types, normalize_headers, parse_date_parts, parse_number,
)

log = logging.getLogger(__name__)

_PAGE_NO = re.compile(r"^(page\s*)?\d+(\s*(of|/)\s*\d+)?$", re.I)
_INVALID_SHEET = re.compile(r"[\[\]\:\*\?\/\\]")


# ------------------------------------------------------------------ small helpers

def _nonempty(row: Sequence[str]) -> int:
    return sum(1 for c in row if c)


def _norm_cell(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def row_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """Mean fuzzy similarity over columns where at least one side is non-empty (0..1)."""
    n = max(len(a), len(b))
    scores: List[float] = []
    for i in range(n):
        x = _norm_cell(a[i]) if i < len(a) else ""
        y = _norm_cell(b[i]) if i < len(b) else ""
        if not x and not y:
            continue
        scores.append(SequenceMatcher(None, x, y).ratio())
    return sum(scores) / len(scores) if scores else 0.0


def is_header_like(row: Sequence[str], ncols: Optional[int] = None) -> bool:
    """A header row: mostly filled, every filled cell contains letters and none is a plain number/date."""
    n = ncols or len(row)
    filled = [c for c in row if c]
    if len(filled) < max(2, (n + 1) // 2):
        return False
    for c in filled:
        if parse_number(c) is not None or parse_date_parts(c) is not None:
            return False
        if not re.search(r"[A-Za-zÀ-ɏऀ-෿]", c):
            return False
    return True


def _pad(row: Sequence[str], n: int) -> List[str]:
    r = list(row)[:n]
    return r + [""] * (n - len(r))


def make_sheet_name(base: str, used: set) -> str:
    name = _INVALID_SHEET.sub("_", base).strip("' ")[:31] or "Sheet"
    cand, i = name, 2
    while cand.lower() in {u.lower() for u in used}:
        suffix = f"_{i}"
        cand = name[: 31 - len(suffix)] + suffix
        i += 1
    used.add(cand)
    return cand


# ------------------------------------------------------------------ page furniture

def _furniture_keys(raw_by_page: Dict[int, List[RawTable]]) -> set:
    """Texts that occur as single-cell edge rows on >=2 different pages (running headers/footers)."""
    seen: Dict[str, set] = defaultdict(set)
    for page, tables in raw_by_page.items():
        for t in tables:
            for pos in (0, len(t.rows) - 1):
                if 0 <= pos < len(t.rows) and _nonempty(t.rows[pos]) == 1:
                    txt = next(c for c in t.rows[pos] if c)
                    seen[re.sub(r"\d+", "#", txt.lower())].add(page)
    return {k for k, pages in seen.items() if len(pages) >= 2}


def _strip_furniture(t: RawTable, keys: set, notes: List[str]) -> RawTable:
    rows, conf, ys = list(t.rows), list(t.row_conf), list(t.row_y)
    removed = 0
    for pos in ("first", "last"):
        while len(rows) > 1:
            i = 0 if pos == "first" else len(rows) - 1
            if _nonempty(rows[i]) != 1:
                break
            txt = next(c for c in rows[i] if c)
            if _PAGE_NO.match(txt.strip()) or re.sub(r"\d+", "#", txt.lower()) in keys:
                rows.pop(i); conf.pop(i); ys.pop(i)
                removed += 1
            else:
                break
    if removed:
        notes.append(f"Page {t.page}: removed {removed} page header/footer/page-number line(s).")
    return RawTable(t.page, t.index, rows, t.method, t.bbox, t.page_height, conf, ys, list(t.notes))


# ------------------------------------------------------------------ main entry

def _drop_empty_columns(t: RawTable) -> RawTable:
    width = max((len(r) for r in t.rows), default=0)
    rows = [_pad(r, width) for r in t.rows]
    keep = [c for c in range(width) if any(r[c] for r in rows)]
    if len(keep) == width or not keep:
        return RawTable(t.page, t.index, rows, t.method, t.bbox, t.page_height, t.row_conf, t.row_y, list(t.notes)) if keep else t
    new_rows = [[r[c] for c in keep] for r in rows]
    # remember ragged lengths are lost here only for columns that are entirely empty
    return RawTable(t.page, t.index, new_rows, t.method, t.bbox, t.page_height, t.row_conf, t.row_y,
                    list(t.notes) + [f"Page {t.page}: dropped {width - len(keep)} completely empty column(s)."])


def _modal_length(lengths: Sequence[int]) -> int:
    if not lengths:
        return 0
    counts = Counter(lengths)
    top = max(counts.values())
    return max(n for n, c in counts.items() if c == top)  # ties -> the longer row (loses no data)


def _prepare(t: RawTable) -> Tuple[RawTable, List[int]]:
    """Clean cells and return the table plus each row's ORIGINAL cell count (for ragged-row flags)."""
    rows = [[clean_text(c) for c in r] for r in t.rows]
    keep = [i for i, r in enumerate(rows) if any(r)]
    orig_len = [len(t.rows[i]) for i in keep]
    rows = [rows[i] for i in keep]
    conf = [t.row_conf[i] for i in keep]
    ys = [t.row_y[i] for i in keep]
    return RawTable(t.page, t.index, rows, t.method, t.bbox, t.page_height, conf, ys, list(t.notes)), orig_len


def reconstruct_tables(page_results: Sequence[PageResult], settings: Settings = SETTINGS) -> List[Table]:
    raw_by_page: Dict[int, List[RawTable]] = {pr.page: sorted(pr.tables, key=lambda t: (t.bbox[1] if t.bbox else t.index)) for pr in page_results if pr.tables}
    if not raw_by_page:
        return []
    furniture = _furniture_keys(raw_by_page)

    # chunk = one raw table, cleaned, with metadata
    chunks: List[dict] = []
    for page in sorted(raw_by_page):
        for rt in raw_by_page[page]:
            notes: List[str] = []
            rt = _strip_furniture(rt, furniture, notes)
            rt, orig_len = _prepare(rt)
            if not rt.rows:
                continue
            ref_len = _modal_length(orig_len)
            rt = _drop_empty_columns(rt)
            chunks.append({"raw": rt, "orig_len": orig_len, "ref_len": ref_len,
                           "notes": notes + list(rt.notes), "page": page})

    tables: List[Table] = []
    meta: List[dict] = []  # parallel to tables: last raw chunk info for merge decisions
    last_table_on_page: Dict[int, int] = {}

    for ci, ch in enumerate(chunks):
        rt: RawTable = ch["raw"]
        page = rt.page
        width = max(len(r) for r in rt.rows)
        rows = [_pad(r, width) for r in rt.rows]
        ref_len = ch["ref_len"]
        first_is_header = is_header_like(rows[0], width)

        is_first_on_page = all(prev["raw"].page != page for prev in chunks[:ci])
        prev_idx = last_table_on_page.get(page - 1)
        decision = None  # None | ("merge", has_header_repeat) | ("possible", reason)

        if is_first_on_page and prev_idx is not None:
            prev = tables[prev_idx]
            pm = meta[prev_idx]
            same_width = prev.n_cols == width
            if first_is_header and same_width:
                sim = row_similarity(rows[0], prev.headers)
                if sim >= settings.header_similarity_merge:
                    decision = ("merge", True)
                elif sim >= settings.header_similarity_possible:
                    decision = ("possible", f"header on page {page} is similar ({sim:.0%}) but not identical to the header of {prev.table_id}")
            elif not first_is_header and same_width:
                pos_ok = _position_ok(pm, rt, settings)
                type_ok = _types_compatible(prev, rows)
                if pos_ok and type_ok:
                    decision = ("merge", False)
                else:
                    why = []
                    if not pos_ok:
                        why.append("page-position evidence missing or does not fit a continuation")
                    if not type_ok:
                        why.append("column value types differ from the previous table")
                    decision = ("possible", "headerless table on page %d has the same column count as %s but %s" % (page, prev.table_id, "; ".join(why)))
            elif not same_width and abs(prev.n_cols - width) <= 1 and not first_is_header:
                decision = ("possible", f"headerless table on page {page} has {width} columns while {prev.table_id} has {prev.n_cols}")

        if decision and decision[0] == "merge":
            tbl = tables[prev_idx]
            body = rows[1:] if decision[1] else rows
            body_conf = rt.row_conf[1:] if decision[1] else rt.row_conf
            body_y = rt.row_y[1:] if decision[1] else rt.row_y
            body_orig = ch["orig_len"][1:] if decision[1] else ch["orig_len"]
            if decision[1]:
                tbl.notes.append(f"Page {page}: repeated header row removed; table continues from page {tbl.pages[-1]}.")
            else:
                tbl.notes.append(f"Page {page}: headerless continuation merged into {tbl.table_id} (page position and column types fit).")
            base = len(tbl.rows)
            for k, r in enumerate(body):
                tbl.rows.append(r)
                tbl.row_pages.append(page)
                tbl.row_methods.append(rt.method)
                tbl.row_conf.append(body_conf[k])
                tbl.row_y.append(body_y[k])
                if body_orig[k] != ref_len:
                    tbl.row_notes.setdefault(base + k, []).append(
                        f"row had {body_orig[k]} cell(s) in the source, expected {ref_len}")
            tbl.pages.append(page)
            tbl.notes.extend(ch["notes"])
            meta[prev_idx] = {"raw": rt, "has_header": True}
            last_table_on_page[page] = prev_idx
            continue

        # new table
        t_id = f"T{len(tables) + 1}"
        if first_is_header:
            headers, body = rows[0], rows[1:]
            body_conf, body_y, body_orig = rt.row_conf[1:], rt.row_y[1:], ch["orig_len"][1:]
            header_inferred, header_page = False, page
        else:
            headers, body = [""] * width, rows
            body_conf, body_y, body_orig = rt.row_conf, rt.row_y, ch["orig_len"]
            header_inferred, header_page = True, None
        hdrs, hnotes = normalize_headers(headers)
        notes = list(ch["notes"])
        if header_inferred:
            notes.append(f"No header row found in the source on page {page}; neutral column names (Column_N) were used.")
        else:
            notes.extend(hnotes)
        tbl = Table(
            table_id=t_id, headers=hdrs, rows=[list(r) for r in body],
            row_pages=[page] * len(body), row_methods=[rt.method] * len(body),
            row_conf=list(body_conf), row_y=list(body_y),
            header_inferred=header_inferred, header_page=header_page, pages=[page], notes=notes,
            bbox=rt.bbox, page_height=rt.page_height,
        )
        for k, n in enumerate(body_orig):
            if n != ref_len:
                tbl.row_notes.setdefault(k, []).append(f"row had {n} cell(s) in the source, expected {ref_len}")
        if decision and decision[0] == "possible":
            prev_tbl = tables[prev_idx]
            tbl.possible_continuation_of = prev_tbl.table_id
            tbl.notes.append(f"Possible continuation of {prev_tbl.table_id} NOT merged: {decision[1]}.")
        tables.append(tbl)
        meta.append({"raw": rt, "has_header": not header_inferred})
        last_table_on_page[page] = len(tables) - 1

    # post-processing per table
    used: set = set()
    for tbl in tables:
        _remove_repeated_headers(tbl, settings)
        tbl.sheet_name = ""  # assigned by the exporter (it owns worksheet naming)
        if tbl.rows:
            last_y = tbl.row_y[-1]
            if last_y and tbl.page_height:
                tbl.last_row_bottom_ratio = last_y[1] / tbl.page_height
    return tables


def _position_ok(prev_meta: dict, rt: RawTable, s: Settings) -> bool:
    prev_raw: RawTable = prev_meta["raw"]
    if not (prev_raw.bbox and prev_raw.page_height and rt.bbox and rt.page_height):
        return False
    bottom = prev_raw.bbox[3] / prev_raw.page_height
    top = rt.bbox[1] / rt.page_height
    return bottom >= s.continuation_bottom_ratio and top <= s.continuation_top_ratio


def _types_compatible(prev: Table, new_rows: List[List[str]]) -> bool:
    types = infer_column_types(prev)
    sample = new_rows[:5]
    for ct in types:
        if ct.kind != "numeric":
            continue
        vals = [r[ct.index] for r in sample if ct.index < len(r) and r[ct.index]]
        if vals and sum(1 for v in vals if parse_number(v) is not None) / len(vals) < 0.5:
            return False
    return True


def _remove_repeated_headers(tbl: Table, s: Settings) -> None:
    if tbl.header_inferred:
        return
    keep_idx = []
    removed = 0
    for i, r in enumerate(tbl.rows):
        if is_header_like(r, tbl.n_cols) and row_similarity(r, tbl.headers) >= s.header_similarity_merge:
            removed += 1
            continue
        keep_idx.append(i)
    if removed:
        remap = {old: new for new, old in enumerate(keep_idx)}
        tbl.rows = [tbl.rows[i] for i in keep_idx]
        tbl.row_pages = [tbl.row_pages[i] for i in keep_idx]
        tbl.row_methods = [tbl.row_methods[i] for i in keep_idx]
        tbl.row_conf = [tbl.row_conf[i] for i in keep_idx]
        tbl.row_y = [tbl.row_y[i] for i in keep_idx]
        tbl.row_notes = {remap[k]: v for k, v in tbl.row_notes.items() if k in remap}
        tbl.notes.append(f"Removed {removed} repeated header row(s) found inside the table body.")
