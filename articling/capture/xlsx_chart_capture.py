"""Reconstructs a native Excel chart (bar/line, including a combo chart with
a secondary axis) as a PNG, purely from openpyxl's parsed chart XML — no
LibreOffice, no screenshot, no VLM.

A chart carries no embedded pixels at all (unlike a Picture or an OLE
preview, which store raw bytes ready to save as-is): Excel keeps only the
series/category *data* plus a cached copy of the last-computed values
(`numCache`/`strCache`), and draws the bars/lines itself at display time.
So this is a "draw from data" reconstruction — the same category of
approximation as `pptx_capture.py`'s shape-geometry redraw. Per-series
*styling* (fill/line color, dash, markers) is read back from the chart XML
too, though — a bar's `solidFill`/a line's `ln`/a marker's `spPr` are just as
deterministic as the values are, so there's no reason to fall back to a
generic palette when the workbook already states the real color. A
`schemeClr` (a named theme slot like "accent3", not a literal color) is
resolved through the workbook's own theme XML, including `lumMod`/`lumOff`
luminance shifts (confirmed on a real document: a "22 판매수" bar using
`bg1` shaded by `lumMod=85000` renders as light gray, not white). Only what
the workbook doesn't specify falls back to `_SERIES_COLORS`.

Only bar and line series are understood (including a combo of the two,
`chart._charts`, openpyxl's representation of a `chart1 += chart2`
authoring call — confirmed on a real document: a dual-axis "CASR/ASR/TSR"
chart with 판매수 bars on the right value axis and 실적 lines on the left
one — see `extract_chart_data`'s `axPos` handling). Pie/scatter/area/3D and
anything else return `None` from
`extract_chart_data` rather than drawing something wrong — no schema type
fits those yet, and this module follows the same partial-failure principle
as the rest of `articling`: skip what isn't understood, don't abort.

Two more pieces of the chart XML are read back purely as *metadata*, not
used by the renderer's own math but handed to the caller (`extractors/xlsx.py`)
for the Table node's `properties` — a chart's real value is a GraphRAG
target as much as a picture is:
`number_format`/`axis_number_formats` (each series' and axis' cached
`formatCode`, e.g. `"0.00"` vs `"General"` — the same distinction a plain
cell already carries) lets the caller format grid values exactly like a
person would read them, via the same `format_cell_display` a plain-cell
Table's grid already uses. `legend_box` (`_legend_manual_box`) is used here,
to keep a dragged legend from covering the axis it sits over (confirmed on
a real document: a wide top-left legend hid the topmost tick label) — the
plot's own margin is widened to clear it rather than drawing on top and
hoping.
"""
from __future__ import annotations

import colorsys
from pathlib import Path

from lxml import etree
from openpyxl.chart.bar_chart import BarChart
from openpyxl.chart.line_chart import LineChart
from PIL import Image, ImageDraw

from .number_format import format_cell_display
from .xlsx_capture import _load_capture_font

_SUPPORTED_CHART_TYPES = (BarChart, LineChart)
_SERIES_COLORS = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7"]

_THEME_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
# DrawingML charts reference the theme's dk1/lt1/dk2/lt2 slots by these
# "mapped" names instead — see ECMA-376 §20.1.10.48 (schemeClr) / the
# <p:clrMap>-style bg1/tx1 convention shared with PPTX.
_SCHEME_ALIASES = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}
# ECMA-376 prstDash values that read as "not solid" for our purposes — one
# dash pattern for all of them rather than reproducing each one exactly.
_DASHED_STYLES = {"dash", "sysDash", "dashDot", "sysDashDot", "sysDashDotDot", "dot", "sysDot", "lgDash", "lgDashDot"}


def parse_theme_colors(theme_xml: bytes | None) -> dict[str, str]:
    """Maps a workbook theme's named color-scheme slots
    (dk1/lt1/dk2/lt2/accent1-6/hlink/folHlink) to "RRGGBB" hex — `sysClr`'s
    `lastClr` (the last color Office actually resolved it to) is used the
    same as a literal `srgbClr`. Pass `workbook.loaded_theme`. Returns {} if
    the workbook has no theme part, so `_resolve_color` always has a dict to
    look up (an empty one just means every `schemeClr` falls through to the
    default palette)."""
    if not theme_xml:
        return {}
    scheme = etree.fromstring(theme_xml).find(".//a:clrScheme", _THEME_NS)
    if scheme is None:
        return {}
    colors: dict[str, str] = {}
    for child in scheme:
        srgb = child.find("a:srgbClr", _THEME_NS)
        sysc = child.find("a:sysClr", _THEME_NS)
        value = srgb.get("val") if srgb is not None else (sysc.get("lastClr") if sysc is not None else None)
        if value:
            colors[etree.QName(child).localname] = value
    return colors


def _apply_luminance(hex_color: str, lum_mod: float | None, lum_off: float | None) -> str:
    """OOXML's `lumMod`/`lumOff` operate in HSL space (thousandths-of-a-percent
    units, e.g. `lumMod=85000` == 85%): scale then shift lightness, clamp,
    convert back. `colorsys` (stdlib) does the RGB<->HLS legwork."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if lum_mod is not None:
        l *= lum_mod / 100000
    if lum_off is not None:
        l += lum_off / 100000
    l = min(1.0, max(0.0, l))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return f"{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def _resolve_color(choice, theme: dict[str, str]) -> str | None:
    """A `ColorChoice`-like object (`GraphicalProperties.solidFill`,
    `LineProperties.solidFill`, a `Marker.spPr.solidFill`, ...) to a
    "#RRGGBB" string. None if unset, or a fill type this doesn't handle
    (gradient/pattern/picture fill) — the caller falls back to the default
    palette rather than guess."""
    if choice is None:
        return None
    if getattr(choice, "srgbClr", None):
        return f"#{choice.srgbClr}"
    scheme_clr = getattr(choice, "schemeClr", None)
    if scheme_clr is not None and scheme_clr.val:
        base = theme.get(_SCHEME_ALIASES.get(scheme_clr.val, scheme_clr.val))
        if base is None:
            return None
        return f"#{_apply_luminance(base, scheme_clr.lumMod, scheme_clr.lumOff)}"
    sys_clr = getattr(choice, "sysClr", None)
    if sys_clr is not None and sys_clr.lastClr:
        return f"#{sys_clr.lastClr}"
    return None


def _series_style(series, kind: str, theme: dict[str, str]) -> dict:
    """Bar: fill color (`None` if `noFill`, meaning "draw with the default
    palette instead" — a series that's deliberately invisible in Excel is
    outside this module's scope). Line: stroke color/width/dash plus, if
    present, marker symbol + fill/outline color — absent marker metadata
    (`symbol` unset or `"none"`) means Excel draws no markers for that
    series, which is itself meaningful (confirmed on a real document: a
    dashed comparison line with no markers next to a solid line that has
    them) so it's left unset rather than defaulted to a circle."""
    style: dict = {}
    gp = series.graphicalProperties
    if gp is None:
        return style
    if kind == "bar":
        if not gp.noFill:
            style["fill"] = _resolve_color(gp.solidFill, theme)
        return style
    ln = gp.ln
    if ln is not None:
        style["line_color"] = _resolve_color(ln.solidFill, theme)
        if ln.prstDash:
            style["dash"] = ln.prstDash
        if ln.w:
            style["line_width"] = max(1, round(ln.w / 12700))
    marker = getattr(series, "marker", None)
    if marker is not None and marker.symbol and marker.symbol != "none":
        style["marker"] = marker.symbol
        if marker.spPr is not None:
            style["marker_fill"] = _resolve_color(marker.spPr.solidFill, theme)
            if marker.spPr.ln is not None:
                style["marker_outline"] = _resolve_color(marker.spPr.ln.solidFill, theme)
    return style


def _series_name(series) -> str:
    tx = series.tx
    if tx is not None:
        if tx.v:
            return str(tx.v)
        if tx.strRef is not None and tx.strRef.strCache is not None and tx.strRef.strCache.pt:
            return str(tx.strRef.strCache.pt[0].v)
    return "Series"


def _series_categories(series) -> list[str] | None:
    cat = series.cat
    if cat is None:
        return None
    if cat.strRef is not None and cat.strRef.strCache is not None:
        return [p.v for p in cat.strRef.strCache.pt]
    if cat.numRef is not None and cat.numRef.numCache is not None:
        return [str(p.v) for p in cat.numRef.numCache.pt]
    return None


def _series_values(series) -> list[float | None] | None:
    val = series.val
    if val is None or val.numRef is None or val.numRef.numCache is None:
        return None
    count = val.numRef.numCache.ptCount or 0
    values: list[float | None] = [None] * count
    for pt in val.numRef.numCache.pt:
        if pt.idx < count:
            values[pt.idx] = pt.v
    return values


def _series_number_format(series) -> str | None:
    """The number format Excel last used to display this series' values
    (cached alongside the values themselves, e.g. `"0.00"` for a ratio vs.
    `"General"` for a raw count) — confirmed on a real document: the same
    chart's bar series (판매수, counts) and line series (실적, a ratio) use
    different formats. `format_cell_display` (the same helper a plain cell
    Table's grid already goes through) turns a raw float plus this format
    into what a person actually reads, instead of a bare `4.66`."""
    val = series.val
    if val is None or val.numRef is None or val.numRef.numCache is None:
        return None
    return val.numRef.numCache.formatCode


def _axis_number_format(part) -> str | None:
    num_fmt = part.y_axis.numFmt
    return num_fmt.formatCode if num_fmt is not None else None


def _chart_title(chart) -> str | None:
    title = chart.title
    if title is None or title.tx is None or title.tx.rich is None:
        return None
    text = "".join(r.t for p in title.tx.rich.p for r in (p.r or []) if r.t)
    return text.strip() or None


def extract_chart_data(chart, theme: dict[str, str] | None = None) -> dict | None:
    """`{"title", "categories", "series": [{"name", "kind", "axis", "values",
    "number_format", ...style}], "axis_number_formats": {"left"|"right": str|None},
    "legend_box": (x, y, w, h) | None}`,
    or `None` if this chart's type (or combo of types) isn't `_SUPPORTED_CHART_TYPES`.
    `theme`: `parse_theme_colors(workbook.loaded_theme)`, for resolving a
    series' `schemeClr` styling — omit only when style doesn't matter (every
    `schemeClr`-based color then resolves to None, so the renderer's default
    palette takes over).

    A combo chart exposes every part through `chart._charts` (self-inclusive
    — `chart` is always `_charts[0]`), each with its own `axId` pair and its
    own `y_axis.axPos` ("l"/"r") — which physical side a part's values are
    scaled/labeled against (`"axis": "left"|"right"`), independent of
    whether `chart` itself is on the left (confirmed on a real document: the
    *bar* part's axis is `axPos="r"` and the *line* part's is `axPos="l"`,
    the reverse of Excel's usual bars-primary-left default). Categories are
    usually cached on only one series (Excel doesn't repeat them per
    series), so the first non-empty one found across every part is used for
    all of them; a series missing its own value cache is dropped rather than
    guessed.
    """
    parts = getattr(chart, "_charts", None) or [chart]
    if not all(isinstance(part, _SUPPORTED_CHART_TYPES) for part in parts):
        return None
    theme = theme or {}

    categories: list[str] | None = None
    series_out: list[dict] = []
    seen_ax_ids: list[int] = []
    axis_number_formats: dict[str, str | None] = {}
    for part in parts:
        kind = "bar" if isinstance(part, BarChart) else "line"
        # Which physical side (left/right) a value axis draws on is
        # `axPos`, not "primary vs. secondary" — a combo chart's *first*
        # part isn't necessarily the one on the left (confirmed on a real
        # document: the bar part's axPos is "r" and the line part's is "l",
        # the opposite of the usual default). Falls back to first-seen-axId
        # = left only when `axPos` itself is absent (rare).
        pos = part.y_axis.axPos
        if pos in ("l", "r"):
            axis = "left" if pos == "l" else "right"
        else:
            if part.axId[1] not in seen_ax_ids:
                seen_ax_ids.append(part.axId[1])
            axis = "left" if seen_ax_ids.index(part.axId[1]) == 0 else "right"
        axis_number_formats.setdefault(axis, _axis_number_format(part))
        for series in part.series:
            cats = _series_categories(series)
            if cats and categories is None:
                categories = cats
            values = _series_values(series)
            if values is None:
                continue
            series_out.append({
                "name": _series_name(series), "kind": kind, "axis": axis, "values": values,
                "number_format": _series_number_format(series),
                **_series_style(series, kind, theme),
            })
    if not series_out:
        return None
    if categories is None:
        n = max(len(s["values"]) for s in series_out)
        categories = [str(i + 1) for i in range(n)]
    return {
        "axis_number_formats": axis_number_formats,
        "title": _chart_title(chart), "categories": categories, "series": series_out,
        "legend_box": _legend_manual_box(chart),
    }


def _legend_manual_box(chart) -> tuple[float, float, float, float] | None:
    """A legend's manual position/size as (x, y, w, h) fractions of the
    whole chart area (ECMA-376's `manualLayout`, `xMode`/`yMode="edge"`) —
    confirmed on a real document: three charts on one sheet, each with the
    legend explicitly dragged to a different spot (top-left, top-right,
    inside the plot), none of them at the `render_chart_image` default
    (below the plot). `None` when the chart has no legend, or has one at
    Excel's un-dragged default position — the caller falls back to a plain
    row under the plot either way, so there's no need to also model
    `position` ("t"/"b"/"l"/"r"/"tr") for the un-dragged case."""
    legend = chart.legend
    if legend is None or legend.layout is None or legend.layout.manualLayout is None:
        return None
    m = legend.layout.manualLayout
    if None in (m.x, m.y, m.w, m.h):
        return None
    return (m.x, m.y, m.w, m.h)


def _draw_dashed_line(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], fill: str, width: int) -> None:
    """PIL's `line()` has no dash support, so a "dashed" style is walked
    segment by segment, alternating a fixed on/off length — visually a
    dashed line, not a reproduction of any particular `prstDash` pattern."""
    on, off = 6, 4
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg_len = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if seg_len == 0:
            continue
        ux, uy = (x1 - x0) / seg_len, (y1 - y0) / seg_len
        pos = 0.0
        while pos < seg_len:
            end = min(pos + on, seg_len)
            draw.line((x0 + ux * pos, y0 + uy * pos, x0 + ux * end, y0 + uy * end), fill=fill, width=width)
            pos += on + off


def _draw_marker(draw: ImageDraw.ImageDraw, x: float, y: float, symbol: str, fill: str, outline: str | None) -> None:
    r = 4
    box = (x - r, y - r, x + r, y + r)
    if symbol == "square":
        draw.rectangle(box, fill=fill, outline=outline)
    elif symbol == "diamond":
        draw.polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)], fill=fill, outline=outline)
    elif symbol == "triangle":
        draw.polygon([(x, y - r), (x + r, y + r), (x - r, y + r)], fill=fill, outline=outline)
    elif symbol in ("x", "plus", "star"):
        draw.line((x - r, y, x + r, y), fill=outline or fill, width=2)
        draw.line((x, y - r, x, y + r), fill=outline or fill, width=2)
    else:  # circle, and anything else not specifically drawn
        draw.ellipse(box, fill=fill, outline=outline)


def render_chart_image(chart_data: dict, out_path: Path, width: int = 900, height: int = 500) -> tuple[int, int]:
    """Draws bar series as grouped columns and line series as lines+markers
    (shape follows each series' `kind`), with independent left/right value
    axes when both `axis` sides are actually used — a series' `axis` picks
    which axis it's *scaled and labeled* against, entirely independent of
    whether it draws as a bar or a line (a combo chart can put either kind
    on either side; see `extract_chart_data`'s `axPos` handling). Each
    series' own fill/line/marker style (from `extract_chart_data`) is used
    where the workbook specifies one; `_SERIES_COLORS` only fills in what it
    left unset. Not a pixel match of Excel's own renderer (font metrics,
    exact bar spacing, gridline density, legend placement all differ), but
    the same visual shape with the real colors and the real axis sides."""
    categories = chart_data["categories"]
    series = chart_data["series"]
    n = max(len(categories), 1)

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = _load_capture_font(13)
    title_font = _load_capture_font(16, bold=True)

    # Legend geometry is needed before the plot's margins are fixed: a
    # top-positioned legend box (every manually-dragged legend seen on a
    # real document landed in the top half) would otherwise sit on top of
    # the topmost gridline/tick label rather than making room for it —
    # confirmed on a real document (a wide legend box hid the "12.91" tick).
    # Pushing `margin_top` below the box's bottom edge fixes that without
    # a general layout solver, since every case seen so far is top-only.
    legend_box = chart_data.get("legend_box")
    legend_geometry = None
    margin_left, margin_right, margin_top, margin_bottom = 70, 70, 50, 90
    if legend_box is not None:
        bx, by = legend_box[0] * width, legend_box[1] * height
        row_h = 20
        pad = 4
        box_h = pad * 2 + row_h * len(series)
        box_w = pad * 2 + max((16 + 6 + draw.textlength(s["name"], font=font) for s in series), default=100)
        legend_geometry = (bx, by, box_w, box_h, row_h, pad)
        if by < height * 0.5:
            margin_top = max(margin_top, round(by + box_h) + 10)

    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    plot_bottom = margin_top + plot_h

    if chart_data.get("title"):
        draw.text((width / 2, 15), chart_data["title"], fill="black", font=title_font, anchor="mm")

    left_series = [s for s in series if s["axis"] == "left"]
    right_series = [s for s in series if s["axis"] == "right"]
    bars = [s for s in series if s["kind"] == "bar"]
    lines = [s for s in series if s["kind"] == "line"]

    def axis_max(group: list[dict]) -> float:
        values = [v for s in group for v in s["values"] if v is not None]
        return max(values) if values else 1.0

    def series_color(s: dict, kind_key: str, index: int, offset: int = 0) -> str:
        return s.get(kind_key) or _SERIES_COLORS[(offset + index) % len(_SERIES_COLORS)]

    def series_max(s: dict) -> float:
        return left_max if s["axis"] == "left" else right_max

    left_max = axis_max(left_series) or 1.0
    right_max = axis_max(right_series) or 1.0
    axis_formats = chart_data.get("axis_number_formats") or {}

    def tick_label(value: float, side: str, fallback: str) -> str:
        fmt = axis_formats.get(side)
        return format_cell_display(value, fmt) if fmt and fmt != "General" else fallback

    ticks = 5
    for i in range(ticks + 1):
        y = plot_bottom - plot_h * i / ticks
        draw.line((margin_left, y, margin_left + plot_w, y), fill="#e0e0e0")
        if left_series:
            v = left_max * i / ticks
            draw.text((margin_left - 8, y), tick_label(v, "left", f"{v:.2f}"), fill="#444", font=font, anchor="rm")
        if right_series:
            v = right_max * i / ticks
            draw.text(
                (margin_left + plot_w + 8, y), tick_label(v, "right", f"{v:.0f}"), fill="#444", font=font, anchor="lm"
            )
    draw.rectangle((margin_left, margin_top, margin_left + plot_w, plot_bottom), outline="black")

    cat_w = plot_w / n
    for i, cat in enumerate(categories):
        cx = margin_left + cat_w * (i + 0.5)
        draw.text((cx, plot_bottom + 8), str(cat), fill="#444", font=font, anchor="ma")
        if bars:
            group_w, bar_w = cat_w * 0.7, cat_w * 0.7 / len(bars)
            x0 = cx - group_w / 2
            for bi, s in enumerate(bars):
                v = s["values"][i] if i < len(s["values"]) else None
                if v is None:
                    continue
                bar_h = plot_h * v / series_max(s)
                bx0 = x0 + bi * bar_w
                draw.rectangle(
                    (bx0, plot_bottom - bar_h, bx0 + bar_w * 0.9, plot_bottom),
                    fill=series_color(s, "fill", bi),
                )

    for si, s in enumerate(lines):
        color = series_color(s, "line_color", si, offset=len(bars))
        line_width = s.get("line_width", 3)
        smax = series_max(s)
        points = [
            (margin_left + cat_w * (i + 0.5), plot_bottom - plot_h * v / smax)
            for i, v in enumerate(s["values"]) if v is not None
        ]
        if len(points) >= 2:
            if s.get("dash") in _DASHED_STYLES:
                _draw_dashed_line(draw, points, color, line_width)
            else:
                draw.line(points, fill=color, width=line_width)
        if s.get("marker"):
            marker_fill = s.get("marker_fill") or color
            marker_outline = s.get("marker_outline")
            for x, y in points:
                _draw_marker(draw, x, y, s["marker"], marker_fill, marker_outline)

    def legend_color(i: int, s: dict) -> str:
        return s.get("fill") or s.get("line_color") or _SERIES_COLORS[i % len(_SERIES_COLORS)]

    if legend_geometry is not None:
        # A dragged legend overlays the plot at an explicit spot (see
        # `_legend_manual_box`) — stacked vertically, since that's the shape
        # every manually-positioned legend took on the real document this
        # was built against (a corner box, narrower than it is tall). Drawn
        # last (on top of bars/lines) but its footprint was already carved
        # out of `margin_top` above, so it doesn't cover the axis itself.
        bx, by, box_w, box_h, row_h, pad = legend_geometry
        draw.rectangle((bx, by, bx + box_w, by + box_h), fill="white", outline="#cccccc")
        for i, s in enumerate(series):
            ry = by + pad + i * row_h
            draw.rectangle((bx + pad, ry + 3, bx + pad + 14, ry + 17), fill=legend_color(i, s))
            draw.text((bx + pad + 20, ry + row_h / 2), s["name"], fill="black", font=font, anchor="lm")
    else:
        lx, ly = margin_left, height - margin_bottom + 35
        for i, s in enumerate(series):
            draw.rectangle((lx, ly, lx + 14, ly + 14), fill=legend_color(i, s))
            draw.text((lx + 20, ly + 7), s["name"], fill="black", font=font, anchor="lm")
            lx += 30 + draw.textlength(s["name"], font=font)

    img.save(out_path, format="PNG")
    return img.size
