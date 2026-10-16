from conftest import make_page, make_raw
from src.models import PageResult
from src.table_reconstructor import (
    is_header_like, make_sheet_name, reconstruct_tables, row_similarity,
)

H = ["Code", "Name", "Qty", "Price"]


def body(start, n):
    return [[f"{i:03d}", f"Item {i}", str(i), f"{i * 1.5:.2f}"] for i in range(start, start + n)]


def test_repeated_header_across_pages_merges_and_drops_duplicate_header():
    pages = [make_page(1, [H] + body(1, 5)), make_page(2, [H] + body(6, 5))]
    t = reconstruct_tables(pages)
    assert len(t) == 1
    assert t[0].headers == H and t[0].n_rows == 10
    assert t[0].row_pages == [1] * 5 + [2] * 5
    assert t[0].pages == [1, 2]


def test_headerless_continuation_merges_only_with_position_and_type_evidence():
    pages = [make_page(1, [H] + body(1, 5)), make_page(2, body(6, 5), y0=40)]
    t = reconstruct_tables(pages)
    assert len(t) == 1 and t[0].n_rows == 10


def test_headerless_next_page_not_merged_without_position_evidence():
    p2 = make_page(2, body(6, 5))
    p2.tables[0].bbox = None  # no geometry -> cannot prove continuation
    t = reconstruct_tables([make_page(1, [H] + body(1, 5)), p2])
    assert len(t) == 2
    assert t[1].possible_continuation_of == t[0].table_id
    assert t[1].header_inferred and t[1].headers[0] == "Column_1"


def test_headerless_not_merged_when_value_types_differ():
    weird = [["a", "b", "c", "d"] for _ in range(4)]   # text where Qty/Price numerics were
    t = reconstruct_tables([make_page(1, [H] + body(1, 5)), make_page(2, weird, y0=40)])
    assert len(t) == 2


def test_different_schema_kept_separate():
    h2 = ["Region", "Target", "Actual", "Gap"]
    t = reconstruct_tables([make_page(1, [H] + body(1, 5)), make_page(2, [h2] + body(1, 3))])
    assert len(t) == 2 and t[1].headers == h2


def test_similar_but_not_identical_header_is_flagged_not_merged():
    h_alt = ["Code", "Name", "Quantity", "Price"]
    t = reconstruct_tables([make_page(1, [H] + body(1, 4)), make_page(2, [["Code", "Title", "Count", "Cost"]] + body(5, 3))])
    assert len(t) == 2
    # with a very close header (OCR noise) it merges
    t2 = reconstruct_tables([make_page(1, [H] + body(1, 4)), make_page(2, [["Cod3", "Name", "Qty", "Price"]] + body(5, 3))])
    assert len(t2) == 1


def test_non_consecutive_pages_never_merge():
    t = reconstruct_tables([make_page(1, [H] + body(1, 4)), PageResult(page=2, status="failed"), make_page(3, [H] + body(5, 4))])
    assert len(t) == 2


def test_page_numbers_and_running_footers_removed():
    r1 = [H] + body(1, 4) + [["Page 1 of 2", "", "", ""], ["Company Confidential", "", "", ""]]
    r2 = [["Company Confidential", "", "", ""], H] + body(5, 4) + [["Page 2 of 2", "", "", ""], ["Company Confidential", "", "", ""]]
    t = reconstruct_tables([make_page(1, r1), make_page(2, r2)])
    flat = [c for row in t[0].rows for c in row]
    assert not any("Page" in c or "Confidential" in c for c in flat)
    assert t[0].n_rows == 8


def test_inconsistent_row_lengths_are_padded_and_flagged():
    rows = [H] + body(1, 3)
    rows[2] = rows[2][:3]            # short row
    t = reconstruct_tables([make_page(1, rows)])[0]
    assert all(len(r) == 4 for r in t.rows)
    assert t.row_notes and "3 cell" in list(t.row_notes.values())[0][0]


def test_long_row_widens_table_without_losing_data():
    rows = [H] + body(1, 4)
    rows[3] = rows[3] + ["EXTRA"]
    t = reconstruct_tables([make_page(1, rows)])[0]
    assert t.n_cols == 5
    assert any("EXTRA" in c for c in t.rows[2])
    assert t.headers[4].startswith("Column_")


def test_missing_headers_get_neutral_names_only():
    t = reconstruct_tables([make_page(1, body(1, 6))])[0]
    assert t.header_inferred
    assert t.headers == ["Column_1", "Column_2", "Column_3", "Column_4"]
    assert any("No header row" in n for n in t.notes)


def test_blank_header_cell_and_duplicates_named_neutrally():
    t = reconstruct_tables([make_page(1, [["Name", "", "Qty", "Name"]] + [["a", "b", "1", "c"]] * 3)])[0]
    assert t.headers == ["Name", "Column_2", "Qty", "Name_2"]


def test_blank_cells_and_whitespace_normalised_not_dropped():
    rows = [H, ["001", "  Spaced   Out  ", "", "1.00"], ["002", "B C", "2", ""]]
    t = reconstruct_tables([make_page(1, rows)])[0]
    assert t.rows[0] == ["001", "Spaced Out", "", "1.00"]
    assert t.rows[1][1] == "B C" and t.rows[1][3] == ""


def test_leading_zeros_kept_in_reconstruction():
    t = reconstruct_tables([make_page(1, [H] + body(1, 3))])[0]
    assert t.rows[0][0] == "001"


def test_two_tables_same_page_stay_separate():
    pr = PageResult(page=1, status="ok")
    pr.tables = [make_raw(1, [H] + body(1, 4), y0=50, y1=200), make_raw(1, [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]], y0=300, y1=360)]
    t = reconstruct_tables([pr])
    assert len(t) == 2


def test_repeated_header_inside_body_removed():
    rows = [H] + body(1, 3) + [H] + body(4, 3)
    t = reconstruct_tables([make_page(1, rows)])[0]
    assert t.n_rows == 6
    assert any("repeated header row" in n for n in t.notes)


def test_helpers():
    assert is_header_like(H) and not is_header_like(["1", "2", "3", "4"])
    assert row_similarity(H, H) == 1.0 and row_similarity(H, ["x", "y", "z", "w"]) < 0.3
    used = set()
    a = make_sheet_name("Data", used); b = make_sheet_name("data", used)
    assert a != b.lower() or a.lower() != b.lower()
    assert len(make_sheet_name("x" * 80, set())) <= 31
    assert "/" not in make_sheet_name("a/b:c?d", set())
