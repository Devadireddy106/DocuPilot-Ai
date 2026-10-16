import copy

import pytest

from conftest import make_table
from src.models import PageResult
from src.schema_normalizer import (
    apply_ocr_digit_fixes, infer_column_types, normalize_headers, parse_date_parts, parse_number, resolve_date,
)
from src.validator import validate

H = ["Item", "Qty", "Amount"]


def issues_for(table, pages=None, **kw):
    return validate([table], pages or [], **kw)


def checks(issues):
    return {i.check for i in issues}


# ------------------------------------------------------------------ numeric parsing

@pytest.mark.parametrize("text,expected", [
    ("1234", 1234.0), ("1,234.50", 1234.5), ("12,34,567.89", 1234567.89), ("1,234,567", 1234567.0), ("(500)", -500.0), ("-12.5", -12.5),
    ("12.5%", 0.125), ("₹1,200", 1200.0), ("Rs. 99", 99.0), ("100-", -100.0), ("0.5", 0.5),
])
def test_parse_number_ok(text, expected):
    assert parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "007", "1,23", "1.234,56", "12/05/2026", "1e5x", "--5", "12 34", "NaN"])
def test_parse_number_refuses_to_guess(text):
    assert parse_number(text) is None


# ------------------------------------------------------------------ dates

def test_date_parsing_unambiguous_and_ambiguous():
    assert parse_date_parts("2026-03-14").value.isoformat() == "2026-03-14"
    assert parse_date_parts("14 Mar 2026").value.isoformat() == "2026-03-14"
    assert parse_date_parts("Mar 14, 2026").value.isoformat() == "2026-03-14"
    amb = parse_date_parts("05/06/2026")
    assert amb.value is None and resolve_date(amb, "ambiguous") == (None, True)
    assert resolve_date(amb, "dmy")[0].isoformat() == "2026-06-05"
    assert resolve_date(amb, "mdy")[0].isoformat() == "2026-05-06"


def test_invalid_date_is_confirmed_error():
    t = make_table(["Date", "Amt"], [["2026-02-30", "1"], ["2026-03-01", "2"], ["2026-03-02", "3"]])
    iss = issues_for(t)
    bad = [i for i in iss if i.check == "invalid_date"]
    assert bad and bad[0].severity == "Error" and bad[0].category == "Confirmed" and bad[0].excel_row == 2


def test_ambiguous_dates_flagged_potential_and_left_as_text():
    t = make_table(["Date", "Amt"], [["05/06/2026", "1"], ["03/04/2026", "2"], ["01/02/2026", "3"]])
    assert any(i.check == "ambiguous_dates" and i.category == "Potential" for i in issues_for(t))
    assert infer_column_types(t)[0].date_order == "ambiguous"


def test_day_first_inferred_when_a_value_exceeds_12():
    t = make_table(["Date", "Amt"], [["25/06/2026", "1"], ["03/04/2026", "2"], ["01/02/2026", "3"]])
    assert infer_column_types(t)[0].date_order == "dmy"
    assert "ambiguous_dates" not in checks(issues_for(t))


def test_conflicting_date_orders_flagged():
    t = make_table(["Date", "Amt"], [["25/06/2026", "1"], ["06/25/2026", "2"], ["01/02/2026", "3"]])
    assert "date_order_conflict" in checks(issues_for(t))


# ------------------------------------------------------------------ table checks

def test_duplicate_records_flagged_as_potential():
    rows = [["A", "1", "10"], ["B", "2", "20"], ["A", "1", "10"]]
    iss = issues_for(make_table(H, rows))
    d = [i for i in iss if i.check == "duplicate_record"]
    assert len(d) == 1 and d[0].row == 3 and d[0].category == "Potential"


def test_missing_value_flagged_when_column_mostly_filled():
    rows = [["A", str(i), str(i * 10)] for i in range(1, 9)]
    rows[3][1] = ""
    iss = issues_for(make_table(H, rows))
    m = [i for i in iss if i.check == "missing_value"]
    assert len(m) == 1 and m[0].column == "Qty" and m[0].row == 4


def test_numeric_parse_failure_flagged():
    rows = [["A", str(i), f"{i}.00"] for i in range(1, 9)]
    rows[2][2] = "1O.00x"
    iss = issues_for(make_table(H, rows))
    assert any(i.check == "numeric_parse" and i.row == 3 for i in iss)


def test_suspicious_outlier_flagged():
    rows = [["A", str(i), str(100 + i)] for i in range(1, 12)]
    rows[5][2] = "99999"
    iss = issues_for(make_table(H, rows))
    assert any(i.check == "suspicious_value" and i.row == 6 for i in iss)


def test_column_misalignment_text_in_numeric_column():
    rows = [["A", str(i), str(i * 2)] for i in range(1, 9)]
    rows[4][1] = "Pending"
    iss = issues_for(make_table(H, rows))
    assert any(i.check == "column_misalignment" and i.row == 5 for i in iss)


def test_total_mismatch_flagged_but_correct_total_is_not():
    good = [["A", "1", "10.00"], ["B", "2", "20.00"], ["Total", "", "30.00"]]
    bad = [["A", "1", "10.00"], ["B", "2", "20.00"], ["Total", "", "35.00"]]
    assert "total_mismatch" not in checks(issues_for(make_table(H, good)))
    assert "total_mismatch" in checks(issues_for(make_table(H, bad)))


def test_subtotals_and_grand_total_accepted():
    rows = [["A", "1", "10.00"], ["Subtotal", "", "10.00"], ["B", "2", "20.00"], ["Subtotal", "", "20.00"], ["Grand Total", "", "30.00"]]
    assert "total_mismatch" not in checks(issues_for(make_table(H, rows)))


def test_empty_table_and_empty_column():
    assert "empty_table" in checks(issues_for(make_table(H, [])))
    rows = [["A", "", "1"], ["B", "", "2"]]
    assert "empty_column" in checks(issues_for(make_table(H, rows)))


def test_repeated_header_row_detected_if_it_survives():
    rows = [["A", "1", "10"], H, ["B", "2", "20"]]
    assert "repeated_header" in checks(issues_for(make_table(H, rows)))


def test_ragged_row_notes_become_issues():
    t = make_table(H, [["A", "1", "10"], ["B", "2", ""]])
    t.row_notes = {1: ["row had 2 cell(s) in the source, expected 3"]}
    assert any(i.check == "row_length" and i.row == 2 for i in issues_for(t))


def test_low_ocr_confidence_only_for_ocr_rows():
    t = make_table(H, [["A", "1", "10"], ["B", "2", "20"]], methods=["ocr_clean_psm6", "pdfplumber_lines"], conf=[40.0, 40.0])
    low = [i for i in issues_for(t) if i.check == "low_ocr_confidence"]
    assert [i.row for i in low] == [1]


# ------------------------------------------------------------------ page-level

def test_failed_page_is_a_confirmed_error():
    pr = PageResult(page=3, status="failed", failure_reason="boom")
    iss = validate([], [pr])
    assert iss[0].severity == "Error" and iss[0].category == "Confirmed" and iss[0].page == 3 and "boom" in iss[0].description


def test_ocr_pages_are_listed_as_unverified():
    pr = PageResult(page=1, status="ok", ocr_used=True, ocr_mean_conf=71.0)
    iss = validate([], [pr])
    assert any(i.check == "ocr_unverified" and i.category == "Unverified" for i in iss)


def test_time_budget_abort_is_reported():
    assert any(i.check == "time_budget" for i in validate([], [], aborted_for_time=True))


def test_issue_ids_unique_and_sorted_by_severity():
    rows = [["A", "1", "10"], ["A", "1", "10"], ["B", "x", "20"]]
    iss = validate([make_table(H, rows)], [PageResult(page=1, status="failed", failure_reason="x")])
    ids = [i.issue_id for i in iss]
    assert len(ids) == len(set(ids)) and ids[0] == "V001"
    sev = [i.severity for i in iss]
    assert sev == sorted(sev, key=lambda s: {"Error": 0, "Warning": 1, "Info": 2}[s])


def test_issue_cap_prevents_flooding():
    rows = [["A", "1", "10"]] * 400
    iss = issues_for(make_table(H, rows))
    assert len([i for i in iss if i.check == "duplicate_record"]) <= 100
    assert any(i.check == "issue_cap" for i in iss)


def test_validator_never_modifies_values():
    rows = [["A", "1", "1O.0"], ["A", "1", "1O.0"], ["Total", "", "5"]]
    t = make_table(H, rows)
    before = copy.deepcopy(t.rows)
    validate([t], [])
    assert t.rows == before


# ------------------------------------------------------------------ deterministic corrections

def _ocr_table(vals):
    rows = [["A", str(i + 1), v] for i, v in enumerate(vals)]
    return make_table(H, rows, methods=["ocr_clean_psm6"] * len(rows))


def test_ocr_corrections_are_recorded_with_original_rule_and_reason():
    t = _ocr_table(["10.00", "20.00", "=30.00", "4O.00"] + [f"{i}0.00" for i in range(5, 21)])
    types = infer_column_types(t)
    corr = apply_ocr_digit_fixes(t, types)
    got = {c.original: c for c in corr}
    assert got["=30.00"].corrected == "30.00" and got["=30.00"].rule == "ocr_border_artifact"
    assert got["4O.00"].corrected == "40.00" and got["4O.00"].rule == "ocr_digit_confusion"
    for c in corr:
        assert c.reason and c.rule and c.page == 1 and c.column == "Amount"
    assert t.rows[2][2] == "30.00" and t.rows[3][2] == "40.00"


def test_corrections_not_applied_to_digital_cells_or_text_columns_or_genuine_values():
    digital = make_table(H, [["A", str(i), "=3" + str(i)] for i in range(1, 10)])
    assert apply_ocr_digit_fixes(digital, infer_column_types(digital)) == []
    t = _ocr_table(["(500)"] + [f"{i}0.00" for i in range(2, 12)])
    assert apply_ocr_digit_fixes(t, infer_column_types(t)) == [] and t.rows[0][2] == "(500)"
    textual = make_table(H, [["l0l", "x", "y"]] * 5, methods=["ocr_clean_psm6"] * 5)
    assert apply_ocr_digit_fixes(textual, infer_column_types(textual)) == []


def test_ambiguous_ocr_values_are_left_for_review_not_guessed():
    t = _ocr_table(["10.00", "20.00", "3l.5O"] + [f"{i}0.00" for i in range(4, 20)] + ["9a.00"])
    corr = apply_ocr_digit_fixes(t, infer_column_types(t))
    assert t.rows[-1][2] == "9a.00"                # letter 'a' is not a known confusion -> untouched
    assert all(c.original != "9a.00" for c in corr)
    assert t.rows[2][2] == "31.50"                 # l->1 and O->0 are the documented, unambiguous ones


def test_header_normalisation_notes():
    h, notes = normalize_headers(["A", "", "A", "a"])
    assert h == ["A", "Column_2", "A_2", "a_3"] and len(notes) == 3


def test_id_like_and_long_digit_columns_stay_text():
    t = make_table(["Phone", "Account No", "Qty"], [["9876543210123", "0012345", "1"], ["9876543210124", "0012346", "2"]])
    kinds = [c.kind for c in infer_column_types(t)]
    assert kinds[0] == "text" and kinds[1] == "text" and kinds[2] == "numeric"


def test_removed_content_is_reported_as_info_for_auditability():
    t = make_table(H, [["A", "1", "10"], ["B", "2", "20"]])
    t.notes = ["Page 1: removed 2 page header/footer/page-number line(s).",
               "Page 2: repeated header row removed; table continues from page 1.",
               "Page 3: repeated header row removed; table continues from page 2.",
               "Removed 1 repeated header row(s) found inside the table body.",
               "Page 2: dropped 1 completely empty column(s)."]
    iss = {i.check: i for i in issues_for(t)}
    assert "2 single-cell line(s)" in iss["removed_page_furniture"].description
    assert "3 repeated header row(s)" in iss["removed_repeated_headers"].description
    assert "1 column(s)" in iss["dropped_empty_columns"].description
    assert all(iss[k].severity == "Info" for k in ("removed_page_furniture", "removed_repeated_headers", "dropped_empty_columns"))
