"""Unit tests for `_merge_touching_tables` — reproduces, with coordinates
alone, cases where a table's original formatting is broken up and it gets
incorrectly split into several rectangles."""
from __future__ import annotations

from articling.extractors.xlsx import _merge_touching_tables


def test_merges_footer_row_directly_below_header() -> None:
    # header+body (4,4,17,12), directly below it (zero gap) a Total row
    # (18,2,18,10) — the column ranges overlap
    ranges = {
        (4, 4, 17, 12): "Body",
        (18, 2, 18, 10): "TotalRow",
    }
    merged = _merge_touching_tables(ranges)
    assert merged == {(4, 2, 18, 12): "Body"}


def test_merges_three_way_chain_including_containment() -> None:
    # reproduces a real case: body (4,4,17,12) + a Total row (18,2,18,10,
    # touching vertically) + a category column (5,3,11,3, touching
    # horizontally). Merging Body+TotalRow first makes the resulting
    # bounding box geometrically contain the category column whole —
    # containment has to be merged too.
    ranges = {
        (4, 4, 17, 12): "Body",
        (18, 2, 18, 10): "TotalRow",
        (5, 3, 11, 3): "CategoryCol",
    }
    merged = _merge_touching_tables(ranges)
    # What matters is that it ends up merged into one table (one bounding
    # box) — which of the three names survives (which depends on merge
    # order) is an implementation detail we don't care about.
    assert list(merged.keys()) == [(4, 2, 18, 12)]


def test_does_not_merge_tables_with_a_real_gap() -> None:
    # two tables with an actual gap (1+ row apart) are left untouched
    ranges = {
        (1, 1, 5, 5): "TableA",
        (7, 1, 10, 5): "TableB",  # there's an empty row 6 between rows 5 and 7 (a gap of 1)
    }
    merged = _merge_touching_tables(ranges)
    assert merged == ranges


def test_does_not_merge_completely_disjoint_tables() -> None:
    # two tables that neither touch nor overlap (completely separate) are left untouched
    ranges = {
        (1, 1, 3, 3): "TableA",
        (10, 10, 12, 12): "TableB",
    }
    assert _merge_touching_tables(ranges) == ranges
