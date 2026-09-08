"""Turns an openpyxl cell value into a string as close as possible to what
Excel actually displays.

Printing `cell.value` alone with `str()` ignores `cell.number_format`
entirely. For example:

- A `'0.00'` format cell whose stored value is a double-precision float
  prints with up to 15 decimal digits as-is (e.g. `1.7846474982972722`).
- A percent format (`'0%'`) cell prints the raw value as-is, showing a
  number **100x wrong** (`0.9965527638499173` → Excel shows "100%").
- A conditional 3-section color format (`'[Blue]\\▽0%;[Red]\\△0%'`, a
  common convention for marking positive/negative with color + symbol) also
  just prints the raw negative number.

Both `Table.grid` text and the visual-capture PNG (`xlsx_capture.py`) go
through this function.

**This is not a full Excel formatting engine** — it only handles common
patterns (plain decimal places, percent, thousands separators, a
conditional 3-section format with bracketed color tags + escaped literals,
basic dates). Any format it doesn't recognize falls back safely to
`str(value)` (never raises — that's not a problem worth blocking the whole
capture/extraction over).
"""
from __future__ import annotations

import datetime
import re

_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_QUOTED_RE = re.compile(r'"([^"]*)"')
_ESCAPE_RE = re.compile(r"\\(.)")
_DIGIT_MASK_RE = re.compile(r"[0#](?:[0#,]*)(\.[0#]+)?")

_DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"yyyy[-/.]mm[-/.]dd", re.I), "%Y-%m-%d"),
    (re.compile(r"yy[-/.]mm[-/.]dd", re.I), "%y-%m-%d"),
    (re.compile(r"mm[-/.]dd[-/.]yyyy", re.I), "%m-%d-%Y"),
    (re.compile(r"m/d/yyyy", re.I), "%m/%d/%Y"),
    (re.compile(r"yyyy[-/.]mm", re.I), "%Y-%m"),
    (re.compile(r"mm[-/.]dd", re.I), "%m-%d"),
]


def format_cell_display(value: object, number_format: str | None) -> str:
    """Turns `value` (`cell.value`) into a string similar to what a person
    sees in Excel, based on `number_format` (`cell.number_format`). If
    `value` is None, returns an empty string; if it's not a number (e.g. a
    string), formatting doesn't apply, so it's returned as plain
    `str(value)`."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return _format_date(value, number_format)
    if not isinstance(value, (int, float)):
        return str(value)

    fmt = number_format or "General"
    if fmt in ("General", "", "@"):
        return _format_general(value)

    try:
        return _format_with_sections(float(value), fmt)
    except Exception:  # noqa: BLE001 — unrecognized/malformed format string, fall back safely
        return str(value)


def _format_general(value: int | float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{value:.10g}"  # approximate Excel's General with ~10 significant digits


def _split_sections(fmt: str) -> list[str]:
    """Splits into `positive;negative;zero;text` sections. A `;` inside
    quotes or right after an escape isn't treated as a separator."""
    sections: list[str] = []
    current: list[str] = []
    in_quotes = False
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == "\\" and i + 1 < len(fmt):
            current.append(ch)
            current.append(fmt[i + 1])
            i += 1
        elif ch == ";" and not in_quotes:
            sections.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    sections.append("".join(current))
    return sections


def _format_with_sections(value: float, fmt: str) -> str:
    sections = _split_sections(fmt)
    if value > 0 or (value == 0 and len(sections) <= 2):
        section, add_sign = sections[0], False
    elif value < 0:
        if len(sections) >= 2:
            section, add_sign = sections[1], False  # dedicated negative section — the sign is expressed by the section itself (color/symbol)
        else:
            section, add_sign = sections[0], True  # only one section — Excel prepends '-' automatically
    else:  # value == 0, has a dedicated zero section
        section, add_sign = sections[2], False

    text = _format_numeric_section(abs(value), section)
    return f"-{text}" if add_sign else text


def _unescape_literals(text: str) -> str:
    text = _QUOTED_RE.sub(lambda m: m.group(1), text)
    return _ESCAPE_RE.sub(r"\1", text)


def _format_numeric_section(value: float, section: str) -> str:
    clean = _BRACKET_TAG_RE.sub("", section)
    is_percent = "%" in clean

    m = _DIGIT_MASK_RE.search(clean)
    if m is None:
        # a format with no digit placeholder (plain text/symbols only) — return just the literal
        return _unescape_literals(clean)

    digit_mask = m.group(0)
    decimals_match = re.search(r"\.([0#]+)", digit_mask)
    decimals = len(decimals_match.group(1)) if decimals_match else 0
    integer_part = digit_mask.split(".", 1)[0]
    use_thousands = "," in integer_part

    display_value = value * 100 if is_percent else value
    number_text = (
        f"{display_value:,.{decimals}f}" if use_thousands else f"{display_value:.{decimals}f}"
    )

    prefix = _unescape_literals(clean[: m.start()])
    suffix = _unescape_literals(clean[m.end():])
    return f"{prefix}{number_text}{suffix}"


def _format_date(value: datetime.date, number_format: str | None) -> str:
    fmt = number_format or ""
    for pattern, py_fmt in _DATE_PATTERNS:
        if pattern.search(fmt):
            try:
                return value.strftime(py_fmt)
            except ValueError:
                break
    if isinstance(value, datetime.datetime) and (value.hour or value.minute or value.second):
        return value.isoformat(sep=" ")
    if hasattr(value, "date") and callable(value.date):
        return value.date().isoformat()
    return value.isoformat()
