"""Regression tests for format_cell_display.

Using `str(cell.value)` alone ignores number_format — a '0.00' format cell
prints its double-precision float value as-is (15 decimal digits), or a '0%'
percent format cell shows a number 100x wrong by printing the raw value
as-is. Reproduces mainly with values that came from real documents.
"""
from __future__ import annotations

import datetime

from articling.capture.number_format import format_cell_display


def test_none_is_empty_string() -> None:
    assert format_cell_display(None, "0.00") == ""


def test_non_numeric_value_passes_through() -> None:
    assert format_cell_display("-", "0%") == "-"
    assert format_cell_display("항목", "General") == "항목"


def test_decimal_format_rounds_instead_of_full_float_precision() -> None:
    # a real bug: this used to come out as "1.7846474982972722" verbatim.
    assert format_cell_display(1.7846474982972722, "0.00") == "1.78"
    assert format_cell_display(10.5, "0.00") == "10.50"
    assert format_cell_display(0, "0.00") == "0.00"


def test_percent_format_multiplies_by_100() -> None:
    # a real bug: this used to come out as "0.9965527638499173" verbatim (Excel shows "100%").
    assert format_cell_display(0.9965527638499173, "0%") == "100%"
    assert format_cell_display(1.0054235086234642, "0%") == "101%"


def test_conditional_three_section_color_format_picks_sign_section() -> None:
    fmt = "[Blue]\\▽0%;[Red]\\△0%"
    # positive -> first section ([Blue]▽), negative -> second section
    # ([Red]△) — the sign symbol itself expresses positive/negative, so it's
    # formatted as an absolute value with no separate '-' prepended.
    assert format_cell_display(0.8998822143698469, fmt) == "▽90%"
    assert format_cell_display(-9.714285714285712, fmt) == "△971%"


def test_thousands_separator() -> None:
    assert format_cell_display(17256, "#,##0") == "17,256"
    assert format_cell_display(1234567.891, "#,##0.00") == "1,234,567.89"


def test_general_format_trims_precision_and_integers() -> None:
    assert format_cell_display(3.0, "General") == "3"
    assert format_cell_display(1234567.891, "General") == "1234567.891"


def test_single_section_format_auto_adds_minus_for_negative() -> None:
    assert format_cell_display(-5.5, "0.00") == "-5.50"


def test_date_formats() -> None:
    d = datetime.date(2026, 9, 4)
    assert format_cell_display(d, "yyyy-mm-dd") == "2026-09-04"
    assert format_cell_display(d, "yyyy-mm") == "2026-09"


def test_unrecognized_format_falls_back_safely() -> None:
    # even an unrecognized/malformed format string must return something with no exception.
    result = format_cell_display(12.3, "???garbage???")
    assert isinstance(result, str)
