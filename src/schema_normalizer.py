"""Cell/column normalisation, type inference and safe typed conversion.

Principles
----------
* Be conservative: when a value cannot be interpreted unambiguously it stays *text*.
* Identifiers (leading zeros, long digit strings, phone numbers) are never turned into numbers.
* Dates are only converted when their day/month order is unambiguous.
* Every automatic change to a value is returned as a :class:`Correction` record.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from src.models import Correction, Table

_NUM_PLAIN = re.compile(r"^\d+(\.\d+)?$")
# Thousands grouping, strict: western 1,234,567.89 or Indian 12,34,567.89. Anything else ("1,23") is
# ambiguous (could be a European decimal) and is NOT parsed.
_NUM_WESTERN = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_NUM_INDIAN = re.compile(r"^\d{1,2}(,\d{2})+,\d{3}(\.\d+)?$")
_CURRENCY = re.compile(r"^(?:rs\.?|inr|usd|eur|gbp|[$€£₹¥])\s*", re.I)
_CURRENCY_TRAIL = re.compile(r"\s*(?:rs\.?|inr|usd|eur|gbp|[$€£₹¥])$", re.I)
_ID_HEADER = re.compile(
    r"\b(id|code|sku|phone|mobile|contact|account|acct|pin|zip|postal|ref|reference|aadhaar|pan|gstin|isbn|barcode|hsn|serial|invoice\s*(no|number|#)|policy)\b",
    re.I,
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_NUM = re.compile(r"^(\d{1,4})([-/.])(\d{1,2})\2(\d{1,4})$")
_DATE_TXT1 = re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3,9})\.?,?[ -](\d{2,4})$")
_DATE_TXT2 = re.compile(r"^([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})$")


# ----------------------------------------------------------------------------- numbers

def parse_number(s: str) -> Optional[float]:
    """Parse an unambiguous numeric string. Returns None when unsure (never guesses)."""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1].strip()
    if t.endswith("-") and not t.startswith("-"):
        neg, t = True, t[:-1].strip()
    if t.startswith("-") or t.startswith("−"):
        neg, t = True, t[1:].strip()
    elif t.startswith("+"):
        t = t[1:].strip()
    t = _CURRENCY.sub("", t)
    t = _CURRENCY_TRAIL.sub("", t).strip()
    pct = False
    if t.endswith("%"):
        pct, t = True, t[:-1].strip()
    if _NUM_PLAIN.match(t):
        if len(t) > 1 and t[0] == "0" and not t.startswith("0."):
            return None  # leading zero => identifier-like, not a quantity
        val = float(t)
    elif _NUM_WESTERN.match(t) or _NUM_INDIAN.match(t):
        val = float(t.replace(",", ""))
    else:
        return None
    if pct:
        val /= 100.0
    return -val if neg else val


def is_percent(s: str) -> bool:
    return str(s).strip().endswith("%")


def _decimals(s: str) -> int:
    m = re.search(r"\.(\d+)", str(s))
    return len(m.group(1)) if m else 0


# ----------------------------------------------------------------------------- dates

@dataclass
class DateParts:
    kind: str          # "ymd" | "abc" (two numeric parts + 4-digit year, order unknown) | "text" | "short" (2-digit year)
    a: int = 0         # first numeric part (for "abc") / year for ymd
    b: int = 0
    c: int = 0         # year for "abc"; day for "ymd"
    value: Optional[dt.date] = None   # set when fully unambiguous & valid
    valid: bool = True


def parse_date_parts(s: str) -> Optional[DateParts]:
    t = str(s).strip()
    if not t:
        return None
    m = _DATE_NUM.match(t)
    if m:
        p1, p2, p3 = m.group(1), int(m.group(3)), m.group(4)
        if len(p1) == 4:  # yyyy-mm-dd
            y, mo, d = int(p1), p2, int(p3)
            try:
                return DateParts("ymd", y, mo, d, dt.date(y, mo, d), True)
            except ValueError:
                return DateParts("ymd", y, mo, d, None, False)
        if len(p3) == 4:  # dd/mm/yyyy or mm/dd/yyyy
            return DateParts("abc", int(p1), p2, int(p3))
        if len(p1) <= 2 and len(p3) == 2:
            return DateParts("short", int(p1), p2, int(p3))
        return None
    m = _DATE_TXT1.match(t) or _DATE_TXT2.match(t)
    if m:
        g = m.groups()
        if _DATE_TXT1.match(t):
            d, mon, y = int(g[0]), g[1], int(g[2])
        else:
            mon, d, y = g[0], int(g[1]), int(g[2])
        mi = _MONTHS.get(mon[:3].lower())
        if mi is None:
            return None
        if y < 100:
            return DateParts("short", d, mi, y)
        try:
            return DateParts("text", y, mi, d, dt.date(y, mi, d), True)
        except ValueError:
            return DateParts("text", y, mi, d, None, False)
    return None


# ----------------------------------------------------------------------------- headers

def normalize_headers(headers: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Fill blank headers with neutral names; de-duplicate repeated names. Returns (headers, notes)."""
    out: List[str] = []
    notes: List[str] = []
    seen: Counter = Counter()
    for i, h in enumerate(headers, start=1):
        name = (h or "").strip()
        if not name:
            name = f"Column_{i}"
            notes.append(f"Column {i} had no header in the source; neutral name '{name}' used.")
        key = name.lower()
        seen[key] += 1
        if seen[key] > 1:
            new = f"{name}_{seen[key]}"
            notes.append(f"Duplicate header '{name}' renamed to '{new}' so columns stay distinguishable.")
            name = new
        out.append(name)
    return out, notes


# ----------------------------------------------------------------------------- column typing

@dataclass
class ColumnType:
    index: int
    name: str
    kind: str = "text"            # numeric | date | text | empty
    n_filled: int = 0
    numeric_ratio: float = 0.0
    date_ratio: float = 0.0
    id_like: bool = False
    date_order: str = "unknown"   # dmy | mdy | iso | conflict | ambiguous | unknown
    decimals: int = 0
    is_percent: bool = False
    has_leading_zero: bool = False


def infer_column_types(table: Table, threshold: float = 0.8) -> List[ColumnType]:
    types: List[ColumnType] = []
    for ci, name in enumerate(table.headers):
        vals = [r[ci] if ci < len(r) else "" for r in table.rows]
        filled = [v for v in vals if v.strip()]
        ct = ColumnType(index=ci, name=name, n_filled=len(filled))
        if not filled:
            ct.kind = "empty"
            types.append(ct)
            continue
        ct.id_like = bool(_ID_HEADER.search(name or ""))
        nums = [parse_number(v) for v in filled]
        ct.numeric_ratio = sum(1 for n in nums if n is not None) / len(filled)
        ct.has_leading_zero = any(re.fullmatch(r"0\d+", v.strip()) for v in filled)
        long_digits = any(re.fullmatch(r"\d{12,}", v.strip()) for v in filled)
        dates = [parse_date_parts(v) for v in filled]
        ct.date_ratio = sum(1 for d in dates if d is not None) / len(filled)

        if ct.date_ratio >= threshold and ct.date_ratio >= ct.numeric_ratio:
            ct.kind = "date"
            ct.date_order = _date_order([d for d in dates if d is not None])
        elif (
            ct.numeric_ratio >= threshold
            and not ct.id_like
            and not ct.has_leading_zero
            and not long_digits
        ):
            ct.kind = "numeric"
            good = [v for v, n in zip(filled, nums) if n is not None]
            ct.decimals = max((_decimals(v) for v in good), default=0)
            ct.is_percent = all(is_percent(v) for v in good)
        else:
            ct.kind = "text"
        types.append(ct)
    return types


def _date_order(parts: List[DateParts]) -> str:
    kinds = {p.kind for p in parts}
    if kinds == {"ymd"}:
        return "iso"
    if kinds <= {"ymd", "text"}:
        return "iso"
    first_gt12 = any(p.kind == "abc" and p.a > 12 for p in parts)
    second_gt12 = any(p.kind == "abc" and p.b > 12 for p in parts)
    if "short" in kinds and not (first_gt12 or second_gt12):
        return "ambiguous"
    if first_gt12 and second_gt12:
        return "conflict"
    if first_gt12:
        return "dmy"
    if second_gt12:
        return "mdy"
    if "abc" in kinds or "short" in kinds:
        return "ambiguous"
    return "iso"


def resolve_date(parts: DateParts, order: str) -> Tuple[Optional[dt.date], bool]:
    """Return (date or None, is_valid). `is_valid` False means a confirmed impossible date."""
    if parts.value is not None:
        return parts.value, True
    if parts.kind in ("ymd", "text"):
        return None, parts.valid
    if parts.kind == "abc" and order in ("dmy", "mdy"):
        d, m = (parts.a, parts.b) if order == "dmy" else (parts.b, parts.a)
        try:
            return dt.date(parts.c, m, d), True
        except ValueError:
            return None, False
    return None, True  # ambiguous/short => unknown, not provably invalid


# ----------------------------------------------------------------------------- typed conversion for export

def to_typed_value(cell: str, ct: ColumnType, order_override: Optional[str] = None):
    """Return (value, number_format_or_None). Unparseable / ambiguous cells remain text."""
    s = cell.strip() if cell else ""
    if not s:
        return None, None
    if ct.kind == "numeric":
        n = parse_number(s)
        if n is None:
            return cell, None
        if ct.is_percent or is_percent(s):
            fmt = "0." + "0" * max(0, ct.decimals - 0) + "%" if ct.decimals else "0%"
            return n, fmt
        if ct.decimals:
            return n, "#,##0." + "0" * ct.decimals
        return (int(n) if float(n).is_integer() else n), "#,##0"
    if ct.kind == "date":
        parts = parse_date_parts(s)
        if parts is None:
            return cell, None
        d, _valid = resolve_date(parts, order_override or ct.date_order)
        if d is not None:
            return d, "yyyy-mm-dd" if ct.date_order == "iso" else "dd-mmm-yyyy"
        return cell, None
    return cell, None


# ----------------------------------------------------------------------------- deterministic corrections

_OCR_DIGIT_MAP = {"O": "0", "o": "0", "l": "1", "I": "1", "|": "1"}
_OCR_CANDIDATE = re.compile(r"^[\d,.\-()%OolI|\s]+$")
_BORDER_CHARS = "|¦=_~[]{}()\\/!'\" \t"


def apply_ocr_digit_fixes(table: Table, types: List[ColumnType]) -> List[Correction]:
    """Fix two well-understood OCR artefacts in OCR-derived cells of NUMERIC columns.

    1. ``ocr_border_artifact``: table borders read as stray characters stuck to a number
       (e.g. ``=100.24``, ``37)``, ``|52``): the stray edge characters are removed.
    2. ``ocr_digit_confusion``: letters that look like digits (O/o->0, l/I/|->1) inside an otherwise
       numeric string.

    Applied only when ALL hold: the cell came from OCR; its column is numeric (>=80% parseable);
    the cell does not already parse as a number; and the repaired text DOES parse as a number.
    Genuine values such as ``(123)`` already parse and are never touched. Every change is recorded
    with the original text, corrected text, rule and reason.
    """
    corrections: List[Correction] = []
    for ci, ct in enumerate(types):
        if ct.kind != "numeric":
            continue
        for ri, row in enumerate(table.rows):
            if ci >= len(row) or not table.row_methods[ri].startswith("ocr"):
                continue
            cell = row[ci]
            if not cell or parse_number(cell) is not None:
                continue
            fixed, rules = cell, []
            stripped = fixed.strip(_BORDER_CHARS)
            if stripped != fixed and any(ch.isdigit() for ch in stripped):
                if parse_number(stripped) is not None or _OCR_CANDIDATE.match(stripped):
                    fixed, rules = stripped, ["ocr_border_artifact"]
            if parse_number(fixed) is None and _OCR_CANDIDATE.match(fixed) and any(ch.isdigit() for ch in fixed):
                swapped = "".join(_OCR_DIGIT_MAP.get(ch, ch) for ch in fixed)
                if swapped != fixed:
                    fixed, rules = swapped, rules + ["ocr_digit_confusion"]
            if rules and fixed != cell and parse_number(fixed) is not None:
                row[ci] = fixed
                reasons = {
                    "ocr_border_artifact": "a stray border character was stuck to the number",
                    "ocr_digit_confusion": "letters that look like digits (O/o->0, l/I/|->1) were inside the number",
                }
                corrections.append(
                    Correction(
                        table_id=table.table_id, row=ri + 1, column=table.headers[ci],
                        original=cell, corrected=fixed, rule="+".join(rules),
                        reason="OCR text in a numeric column: " + "; ".join(reasons[r] for r in rules)
                               + ". The repaired text parses as a number.",
                        page=table.row_pages[ri],
                    )
                )
    return corrections
