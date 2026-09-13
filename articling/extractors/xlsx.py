"""XLSX -> ArticDocument, a fully native path (openpyxl only, no LibreOffice/VLM needed).

Artifact = a worksheet (hidden ones included).

1. **Table** — explicit Excel Tables (ws.tables, the blue-striped kind) plus
   border-detected ranges (`_border_table_ranges`: finds rectangles closed on
   all four sides by borders as table candidates — directly mimicking how a
   person recognizes "this is a table"). When the original formatting is
   broken up (e.g. a Total row right under the header is bordered separately
   and narrower than the header), what looks like one table to a person can
   get split into several rectangles — `_merge_touching_tables` merges
   candidates back together when they touch with zero gap (and overlap), or
   when one contains the other. Each Table node gets both a text grid and a
   PNG that **visually composites the table range as an actual image**
   (`capture.xlsx_capture`) — even a real embedded photo inside the table is
   composited at its exact position, so visual information isn't lost.
2. **Chart** — a native Excel chart (`ws._charts`), also emitted as a
   **Table** node (`is_chart=True`) rather than a new node type, since its
   extracted series/categories are exactly a small grid — same shape as a
   cell Table, just sourced from chart XML instead of cells. Unlike a
   Picture, a chart has no embedded pixels at all (Excel draws it live from
   series data), so its `capture_path` is a from-scratch reconstruction
   (`capture.xlsx_chart_capture`), not an extracted original — currently
   understands bar/line charts and combos of the two (e.g. bars on a
   primary axis + a line on a secondary one); anything else (pie/scatter/
   area/3D) is skipped rather than drawn wrong.
3. **Image** — pictures inserted in the worksheet, **only the ones with no
   anchor inside any Table range.** Images inside a table range never
   become a separate node ("Option A: absorb") — they're instead preserved
   visually inside that table's capture PNG (most overlap cases are images
   inside a table range). The same "absorb" principle applies when
   standalone images outside any table overlap each other (e.g. a small
   driver-icon photo placed separately on top of a large background photo —
   confirmed in a real screw fastening-strength review document) —
   `_group_overlapping_standalone_images` groups the overlapping ones
   together, keeps only the largest image in the group as the Image node,
   and composites the rest onto it, absorbing them
   (`images_absorbed_into_image` count). If that image (the base image after
   compositing) has annotations a person drew directly on top of it
   (highlight rectangles/arrows/label text, e.g. the red highlight box +
   yellow label in the screw fastening-strength review photo), the
   annotated/absorbed composite PNG is exported as `image_path` instead of
   the raw bytes (`capture.xlsx_capture.annotate_standalone_image`) —
   otherwise, unlike table capture, that annotation/overlapping photo would
   be preserved nowhere and quietly lost.
4. **Text** — non-empty cells not covered by a Table, grouped into
   **connected chunks (8-connected connected components)**. Non-table cells
   aren't all merged into one blob — physically separate chunks (a note next
   to a table, a standalone note) are split into separate Text nodes. A
   merged cell's anchor value is filled across the whole range so it's
   included in connectivity checks.
"""
from __future__ import annotations

import io
import warnings
from pathlib import Path

import openpyxl
from openpyxl.utils import range_boundaries
from PIL import Image as PILImage

from ..capture.number_format import format_cell_display
from ..capture.xlsx_capture import (
    annotate_standalone_image,
    build_sheet_grid,
    capture_table_image,
    image_anchor_rect,
    pic_anchor_flip,
    rects_overlap,
    shape_anchor_max_row_col,
    sheet_font_metrics_measured,
)
from ..capture.xlsx_chart_capture import extract_chart_data, parse_theme_colors, render_chart_image
from ..schema import ArticDocument, Edge, Node, NodeType
from ..scaffold import caption_prefix_edges, file_node, parent_edges, reference_label_edges, resolve_capture_dir, save_image_bytes


def _table_ranges(ws) -> dict[tuple[int, int, int, int], str]:
    # ws.tables is a TableList (dict subclass): TableList.items() deliberately
    # yields (name, ref_string) pairs, not (name, Table) — unlike plain
    # dict.items(). Use the ref string directly rather than `.ref` on it.
    ranges = {}
    for name, ref in ws.tables.items():
        min_col, min_row, max_col, max_row = range_boundaries(ref)
        ranges[(min_row, min_col, max_row, max_col)] = name
    return ranges


def _has_style(side) -> bool:
    return side is not None and side.style is not None


def _content_bounds(ws, xlsx_path: Path | None = None, padding: int = 5) -> tuple[int, int]:
    """Compute the (max_row, max_col) actually worth scanning.

    `ws.max_row`/`ws.max_column` just trust the `<dimension>` declaration
    baked into the file, which a common Excel habit — "select whole
    rows/columns and apply formatting only" (borders/fill with no value) —
    can inflate all the way up to 1,048,575 rows / 16,384 columns regardless
    of the actual data range. Using that as-is for the border-scan/text-scan
    range would make an O(rows*cols) loop effectively infinite — so
    `ws.max_row`/`ws.max_column` are never used at all; the range is
    determined purely from content actually found (below).

    We collect **cells with a value**, merged ranges, formal `ws.tables`
    ranges, and the bounding boxes of image/shape anchors, then add a small
    padding — the border-only, valueless edge rows/columns of a real table
    are usually within a few rows/columns of a cell that has a value, so a
    small padding is enough to catch them. Images/shapes are caught by
    neither the `<dimension>` declaration nor a value-cell scan (both go by
    "does the cell have a value") — missing a photo placed entirely outside
    the value-cell range (e.g. one inserted in its own area rather than next
    to a note) and the annotation shapes on top of it means the sheet-wide
    coordinate system (`build_sheet_grid`) that `annotate_standalone_image`
    depends on won't cover that anchor cell, so the overlap itself can never
    be seen.
    """
    max_row = max_col = 0
    for (r, c), cell in getattr(ws, "_cells", {}).items():
        if cell.value is not None:
            max_row = max(max_row, r)
            max_col = max(max_col, c)
    for m in ws.merged_cells.ranges:
        max_row = max(max_row, m.max_row)
        max_col = max(max_col, m.max_col)
    for tbl in ws.tables.values():
        _, _, tc, tr = range_boundaries(tbl.ref)
        max_row = max(max_row, tr)
        max_col = max(max_col, tc)
    for img in getattr(ws, "_images", []):
        for edge in (getattr(img.anchor, "_from", None), getattr(img.anchor, "to", None)):
            if edge is not None:
                max_row = max(max_row, edge.row + 1)
                max_col = max(max_col, edge.col + 1)
    if xlsx_path is not None:
        shape_max_row, shape_max_col = shape_anchor_max_row_col(xlsx_path, ws)
        max_row, max_col = max(max_row, shape_max_row), max(max_col, shape_max_col)

    if max_row == 0 and max_col == 0:
        return 0, 0
    return max_row + padding, max_col + padding


def _border_table_ranges(ws, max_row: int, max_col: int) -> dict[tuple[int, int, int, int], str]:
    """Find rectangles closed on all four sides by borders as table
    candidates, even when they aren't a formal Table object.

    Takes a cell with both a top and left border as a top-left candidate,
    extends right as far as the top border continues and down as far as the
    left border continues, then verifies the whole bottom row of that
    rectangle has a bottom border and the whole right column has a right
    border. `max_row`/`max_col` is the real-data-based scan range already
    computed by `_content_bounds`.
    """
    if max_row == 0 or max_col == 0:
        return {}

    top: dict[tuple[int, int], bool] = {}
    left: dict[tuple[int, int], bool] = {}
    bottom: dict[tuple[int, int], bool] = {}
    right: dict[tuple[int, int], bool] = {}
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            b = ws.cell(row=r, column=c).border
            top[(r, c)] = _has_style(b.top)
            left[(r, c)] = _has_style(b.left)
            bottom[(r, c)] = _has_style(b.bottom)
            right[(r, c)] = _has_style(b.right)

    candidates: list[tuple[int, int, int, int]] = []
    for r1 in range(1, max_row + 1):
        for c1 in range(1, max_col + 1):
            if not (top.get((r1, c1)) and left.get((r1, c1))):
                continue
            c2 = c1
            while c2 + 1 <= max_col and top.get((r1, c2 + 1)):
                c2 += 1
            r2 = r1
            while r2 + 1 <= max_row and left.get((r2 + 1, c1)):
                r2 += 1
            if r2 == r1 and c2 == c1:
                continue  # don't treat a 1x1 cell as a table (avoid noise)
            if all(bottom.get((r2, c)) for c in range(c1, c2 + 1)) and all(
                right.get((r, c2)) for r in range(r1, r2 + 1)
            ):
                candidates.append((r1, c1, r2, c2))

    # Every individual cell inside a table also has borders, so many small
    # rectangles get caught too — drop anything fully contained in another
    # candidate and keep only the outermost rectangles.
    outermost = [cand for cand in candidates if not any(_contains(other, cand) for other in candidates)]

    return {(r1, c1, r2, c2): f"BorderTable{i + 1}" for i, (r1, c1, r2, c2) in enumerate(outermost)}


def _ranges_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ar1, ac1, ar2, ac2 = a
    br1, bc1, br2, bc2 = b
    return not (ar2 < br1 or br2 < ar1 or ac2 < bc1 or bc2 < ac1)


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    or1, oc1, or2, oc2 = outer
    ir1, ic1, ir2, ic2 = inner
    return outer != inner and or1 <= ir1 and oc1 <= ic1 and or2 >= ir2 and oc2 >= ic2


def _touches(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """Checks whether two table candidates touch with zero gap, and whether
    they actually overlap in the direction perpendicular to the shared edge.
    Meant to catch two typical over-splitting patterns:

    1. A "Total" row right under the header is bordered separately, in a
       narrower range than the header, and gets caught as a separate table
       (touching vertically, column ranges overlap).
    2. A "category" column right next to the main table is missing its left
       border on one row, splitting it off from the main table as its own
       single-column table (touching horizontally, row ranges overlap).

    To a person these are obviously one table whose original formatting
    happens to be broken up, causing `_border_table_ranges` to split it into
    two rectangles — tables with an actual gap (genuinely separate) are left
    untouched.
    """
    ar1, ac1, ar2, ac2 = a
    br1, bc1, br2, bc2 = b
    if ar2 + 1 == br1 or br2 + 1 == ar1:  # touching vertically
        if min(ac2, bc2) - max(ac1, bc1) >= 0:  # column ranges actually overlap
            return True
    if ac2 + 1 == bc1 or bc2 + 1 == ac1:  # touching horizontally
        if min(ar2, br2) - max(ar1, br1) >= 0:  # row ranges actually overlap
            return True
    return False


def _merge_touching_tables(
    table_ranges: dict[tuple[int, int, int, int], str],
) -> dict[tuple[int, int, int, int], str]:
    """Merges table candidates that `_touches` finds touching into their
    combined bounding box. Merging can create new touching pairs (a chain of
    3+ touching candidates), so this repeats until nothing more can be
    merged.

    Merges on **containment** too, not just touching — for example, merging
    table A (header+body) with table B right below it (a Total row) first
    can make the resulting bounding box geometrically swallow table C
    (a single-column "category" strip that was to A's left from the start)
    whole (no longer "touching" but "containment"). That case needs to be
    absorbed too for everything to end up as the single table a person
    actually sees.
    """
    ranges = dict(table_ranges)
    changed = True
    while changed:
        changed = False
        keys = list(ranges.keys())
        for i, a in enumerate(keys):
            if a not in ranges:
                continue
            for b in keys[i + 1 :]:
                if b not in ranges:
                    continue
                if _touches(a, b) or _contains(a, b) or _contains(b, a):
                    merged_key = (
                        min(a[0], b[0]), min(a[1], b[1]),
                        max(a[2], b[2]), max(a[3], b[3]),
                    )
                    name = ranges.pop(a)
                    ranges.pop(b)
                    ranges[merged_key] = name
                    changed = True
                    break
            if changed:
                break
    return ranges


def _cell_in_any_table(row: int, col: int, table_ranges: dict) -> bool:
    return any(
        min_row <= row <= max_row and min_col <= col <= max_col
        for (min_row, min_col, max_row, max_col) in table_ranges
    )


def _merged_anchor_map(ws) -> dict[tuple[int, int], tuple[int, int]]:
    """Every cell inside a merged range -> its anchor (top-left) cell coordinates."""
    mapping = {}
    for merged in ws.merged_cells.ranges:
        min_col, min_row, max_col, max_row = merged.min_col, merged.min_row, merged.max_col, merged.max_row
        anchor = (min_row, min_col)
        for r in range(min_row, max_row + 1):
            for c in range(min_col, max_col + 1):
                mapping[(r, c)] = anchor
    return mapping


def _connected_components(cells: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    """Groups cells adjacent by 8-connectivity (diagonals included) into one chunk."""
    remaining = set(cells)
    components = []
    while remaining:
        start = next(iter(remaining))
        stack = [start]
        comp: set[tuple[int, int]] = set()
        while stack:
            cur = stack.pop()
            if cur in comp:
                continue
            comp.add(cur)
            r, c = cur
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nb = (r + dr, c + dc)
                    if nb in remaining and nb not in comp:
                        stack.append(nb)
        remaining -= comp
        components.append(comp)
    return components


def _group_overlapping_standalone_images(
    rects: dict[int, tuple[int, int, int, int]],
) -> list[list[int]]:
    """Groups mutually-overlapping image indices into one group (BFS, based
    on `rects_overlap`) — this is what applies the same "Option A: absorb"
    principle when standalone images outside any table overlap each other
    (e.g. a small driver-icon photo placed separately on top of a large
    background photo, a pattern found in a real document). Same BFS skeleton
    as `_connected_components`, but that one goes by grid adjacency
    (8-connectivity) while this one goes by whether two arbitrary rectangles
    overlap, so it's a separate function. A sheet has at most a few dozen
    images in practice, so an O(n^2) pairwise comparison is plenty fast
    enough."""
    remaining = set(rects)
    groups: list[list[int]] = []
    while remaining:
        start = next(iter(remaining))
        stack = [start]
        group: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in group:
                continue
            group.add(cur)
            remaining.discard(cur)
            for other in list(remaining):
                if rects_overlap(rects[cur], rects[other]):
                    stack.append(other)
        groups.append(sorted(group))
    return groups


def extract(path: Path, capture_dir: Path | None = None) -> ArticDocument:
    """Converts an XLSX into an ArticDocument.

    `capture_dir`: the directory to save table visual-capture PNGs into
    (default: `captures/` next to `path`). Left open for the caller to
    choose the output location, since this is an open-source package.
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    capture_root = resolve_capture_dir(path, capture_dir)
    # Resolved once per workbook — every chart's `schemeClr` styling shares
    # the same theme part, so there's no reason to reparse it per chart.
    theme_colors = parse_theme_colors(wb.loaded_theme)

    file_n = file_node(path)
    nodes: list[Node] = [file_n]
    edges: list[Edge] = []
    artifacts: list[Node] = []
    counts = {
        "text": 0, "table": 0, "image": 0, "chart": 0,
        "images_absorbed_into_table": 0, "images_absorbed_into_image": 0,
    }
    # Default font names of sheets whose font couldn't be measured (fell back
    # to Calibri) — instead of warning per sheet, these are collected and
    # reported once at the end of extract() (to avoid the same warning
    # firing dozens of times when a document has dozens of sheets).
    unmeasured_fonts: set[str] = set()

    for sheet_idx, name in enumerate(wb.sheetnames):
        ws = wb[name]
        font_measured, default_font_name = sheet_font_metrics_measured(ws)
        if not font_measured:
            unmeasured_fonts.add(default_font_name)
        artifact = Node(
            id=f"artifact:{path.name}:sheet{sheet_idx}",
            type=NodeType.ARTIFACT,
            name=name,
            properties={
                "sheet_index": sheet_idx, "kind": "worksheet", "hidden": ws.sheet_state != "visible",
                # False means this sheet's editAs="twoCell" shape/image
                # positions were computed with an approximated Calibri
                # column width instead of the actual measured font — they
                # can end up visibly off from another anchor on the same
                # photo (e.g. editAs="oneCell") (see Experiments.md
                # "Install the workbook's font locally to improve accuracy").
                "column_width_font_measured": font_measured,
            },
        )
        nodes.append(artifact)
        artifacts.append(artifact)

        content_max_row, content_max_col = _content_bounds(ws, path)
        # Compute the sheet-wide coordinate system once and reuse it for
        # every image (see build_sheet_grid), so that annotation compositing
        # over a standalone image (annotate_standalone_image) can resolve
        # image anchors and shape anchors into the same absolute pixel space.
        sheet_row_off, sheet_col_off, sheet_canvas_w, sheet_canvas_h = build_sheet_grid(
            ws, content_max_row, content_max_col
        )

        table_ranges = _table_ranges(ws)
        border_ranges = _border_table_ranges(ws, content_max_row, content_max_col)
        for key, bname in border_ranges.items():
            if not any(_ranges_overlap(key, existing) for existing in table_ranges):
                table_ranges[key] = bname
        # Merge cases where the original formatting is broken up so what
        # looks like one table to a person got split into several rectangles
        # (e.g. a Total row right under the header caught as a separate
        # table). See _touches.
        table_ranges = _merge_touching_tables(table_ranges)
        content: list[Node] = []

        # 1. Table nodes — produce both grid text and a visual capture
        # (including embedded images inside the table)
        for (min_row, min_col, max_row, max_col), tbl_name in table_ranges.items():
            # Printing cell.value alone with str() ignores formatting
            # (number_format) entirely — e.g. a '0.00' format cell whose
            # stored value is a double-precision float prints with up to 15
            # decimal digits, or a '0%' percent format cell prints the raw
            # value (0.99...) as-is, showing a number 100x wrong (see
            # capture/number_format.py).
            grid = [
                [
                    format_cell_display(ws.cell(row=r, column=c).value, ws.cell(row=r, column=c).number_format)
                    for c in range(min_col, max_col + 1)
                ]
                for r in range(min_row, max_row + 1)
            ]
            counts["table"] += 1
            table_id = f"content:{path.name}:s{sheet_idx}:tbl{counts['table']}"

            capture_path = capture_root / f"{path.stem}__s{sheet_idx}__tbl{counts['table']}.png"
            cw, ch, pasted_count = capture_table_image(
                ws, min_row, min_col, max_row, max_col, capture_path, xlsx_path=path
            )
            counts["images_absorbed_into_table"] += pasted_count

            content.append(
                Node(
                    id=table_id,
                    type=NodeType.TABLE,
                    name=f"{tbl_name} ({max_row - min_row + 1} rows x {max_col - min_col + 1} cols)",
                    properties={
                        "row": min_row,
                        "col": min_col,
                        "range": f"R{min_row}C{min_col}:R{max_row}C{max_col}",
                        "grid": grid,
                        "capture_path": str(capture_path.resolve()) if pasted_count or (cw and ch) else None,
                        "capture_size": {"width": cw, "height": ch},
                        "capture_embedded_image_count": pasted_count,
                    },
                )
            )

        # 2. Chart nodes — a Table node too (its extracted series/categories
        # are exactly a small grid, the same shape as a real cell Table),
        # since a chart carries no embedded pixels to fall back on the way a
        # Picture/OLE object does — see `xlsx_chart_capture.py`. A chart
        # type this doesn't understand (pie/scatter/area/3D) is skipped, not
        # guessed.
        for chart in getattr(ws, "_charts", []):
            data = extract_chart_data(chart, theme_colors)
            if data is None:
                continue
            anchor_from = getattr(chart.anchor, "_from", None)
            row = (anchor_from.row + 1) if anchor_from is not None else None
            col = (anchor_from.col + 1) if anchor_from is not None else None
            counts["chart"] += 1
            # Format each row through the same number-format-aware display
            # a plain cell already goes through (`format_cell_display`) —
            # a series cached as "0.00" reads as "4.66", not the bare float
            # `4.66000000000001` a naive str() would sometimes produce, and
            # a "0%"-formatted series would read as an actual percentage.
            grid = [[""] + list(data["categories"])]
            grid += [
                [s["name"]] + [format_cell_display(v, s.get("number_format")) for v in s["values"]]
                for s in data["series"]
            ]
            # A GraphRAG consumer reading `grid` alone can't tell which row
            # is a bar vs. a line, or which axis it's scaled against — kept
            # here as structured metadata instead of folded into `grid`'s
            # text, so it's queryable without re-parsing row order.
            series_meta = [
                {"name": s["name"], "kind": s["kind"], "axis": s["axis"], "number_format": s.get("number_format")}
                for s in data["series"]
            ]
            capture_path = capture_root / f"{path.stem}__s{sheet_idx}__chart{counts['chart']}.png"
            # Render at the chart's own anchored size, not a fixed default —
            # these vary a lot (confirmed on a real document: three combo
            # charts on one sheet, from a wide/short ~940x230 to a taller
            # ~965x850), and a hardcoded aspect ratio would visibly distort
            # bar/gap proportions relative to the original. Clamped to a
            # sane range so a tiny or malformed anchor still leaves room for
            # labels, and a huge one doesn't produce an oversized PNG.
            rect = image_anchor_rect(chart, sheet_row_off, sheet_col_off, sheet_canvas_w, sheet_canvas_h)
            render_w = min(1600, max(400, rect[2] - rect[0])) if rect else 900
            render_h = min(1000, max(260, rect[3] - rect[1])) if rect else 500
            cw, ch = render_chart_image(data, capture_path, width=render_w, height=render_h)
            content.append(
                Node(
                    id=f"content:{path.name}:s{sheet_idx}:chart{counts['chart']}",
                    type=NodeType.TABLE,
                    name=data["title"] or f"Chart {counts['chart']}",
                    properties={
                        "row": row,
                        "col": col,
                        "grid": grid,
                        "capture_path": str(capture_path.resolve()),
                        "capture_size": {"width": cw, "height": ch},
                        "chart_type": sorted({s["kind"] for s in data["series"]}),
                        "is_chart": True,
                        "series_meta": series_meta,
                        "axis_number_formats": data["axis_number_formats"],
                    },
                )
            )

        # 3. Image nodes — "Option A: absorb" — images inside a table range
        # never become a separate node (already preserved visually inside
        # the table capture PNG). Only images outside any table range remain.
        images = getattr(ws, "_images", [])
        standalone_indices: list[int] = []
        standalone_rects: dict[int, tuple[int, int, int, int]] = {}
        for idx, img in enumerate(images):
            anchor = img.anchor
            row = getattr(getattr(anchor, "_from", None), "row", 0) + 1 if hasattr(anchor, "_from") else 0
            col = getattr(getattr(anchor, "_from", None), "col", 0) + 1 if hasattr(anchor, "_from") else 0
            if _cell_in_any_table(row, col, table_ranges):
                continue  # absorbed into a table — skip a separate node
            standalone_indices.append(idx)
            rect = image_anchor_rect(img, sheet_row_off, sheet_col_off, sheet_canvas_w, sheet_canvas_h)
            if rect is not None:
                standalone_rects[idx] = rect

        # Apply the same "absorb" principle when standalone images outside
        # any table overlap each other too (e.g. a small driver-icon photo
        # placed separately on top of a large background photo — confirmed
        # in a real screw fastening-strength review document) — only the
        # base image in the group becomes a node, and the rest are
        # composited onto it. An image whose anchor couldn't be resolved
        # (e.g. outside the sheet scan range, rare) can't be checked for
        # overlap at all, so it's left as a group of one.
        groups = _group_overlapping_standalone_images(standalone_rects) if standalone_rects else []
        grouped_idx = {idx for group in groups for idx in group}
        groups.extend([idx] for idx in standalone_indices if idx not in grouped_idx)

        for group in groups:
            # Use the earliest in document order (=z-order, since
            # `ws._images` follows the drawing XML anchors' appearance order
            # as-is) as the base image — placing a background photo first
            # and layering annotation/icon photos on top later is the usual
            # authoring order (instead of the earlier guess of "the largest
            # area is the background," this uses the same "later is on top"
            # rule already used for shapes
            # (`_load_sheet_shapes`/`capture_table_image`)). `group` is
            # already sorted, so `min(group)` is just `group[0]`.
            base_idx = min(group)
            overlay_idx_list = [idx for idx in group if idx != base_idx]
            counts["images_absorbed_into_image"] += len(overlay_idx_list)

            base_img = images[base_idx]
            anchor = base_img.anchor
            row = getattr(getattr(anchor, "_from", None), "row", 0) + 1 if hasattr(anchor, "_from") else 0
            col = getattr(getattr(anchor, "_from", None), "col", 0) + 1 if hasattr(anchor, "_from") else 0
            counts["image"] += 1
            props = {"row": row, "col": col}
            # Images outside any table range aren't absorbed into a capture
            # PNG, so the original has to be saved separately or the pixels
            # would be lost (same decision as docx.py/pptx.py).
            try:
                data = base_img._data()
                fmt = PILImage.open(io.BytesIO(data)).format or "PNG"
                stem = f"{path.stem}__s{sheet_idx}__img{counts['image']}"
                saved = save_image_bytes(capture_root, stem, data, f".{fmt.lower()}")
                props["image_path"] = str(saved.resolve())

                # Pre-collect the raw bytes + absolute rectangle + flipH/flipV
                # of other overlapping standalone images (absorption
                # targets) — annotate_standalone_image composites them
                # together with the shapes, mirroring each one that needs it
                # (see pic_anchor_flip's docstring for why: an image's own
                # flip is otherwise silently dropped).
                overlay_images: list[tuple[bytes, tuple[int, int, int, int], bool, bool]] = []
                for ov_idx in overlay_idx_list:
                    ov_rect = standalone_rects.get(ov_idx)
                    if ov_rect is None:
                        continue
                    try:
                        ov_flip_h, ov_flip_v = pic_anchor_flip(images[ov_idx].anchor)
                        overlay_images.append((images[ov_idx]._data(), ov_rect, ov_flip_h, ov_flip_v))
                    except Exception:  # noqa: BLE001 — e.g. a corrupted embedded image, skip just that one
                        continue

                # If this photo has annotations a person drew directly on
                # top of it (highlight box/arrow/label) or other
                # overlapping standalone images, export the composited PNG
                # instead of the raw bytes — if neither is present (most of
                # the time), annotate_standalone_image returns None and the
                # raw bytes are left as-is.
                annotated_path = capture_root / f"{stem}__annotated.png"
                annotated = annotate_standalone_image(
                    ws, base_img, data, path, sheet_row_off, sheet_col_off, sheet_canvas_w, sheet_canvas_h,
                    annotated_path, overlay_images=overlay_images,
                )
                if annotated is not None:
                    _, _, shape_count, image_count = annotated
                    props["image_path"] = str(annotated_path.resolve())
                    if shape_count:
                        props["annotation_shape_count"] = shape_count
                    if image_count:
                        props["absorbed_image_count"] = image_count
            except Exception:  # noqa: BLE001 — e.g. a corrupted embedded image, keep at least the metadata
                pass
            content.append(
                Node(
                    id=f"content:{path.name}:s{sheet_idx}:img{counts['image']}",
                    type=NodeType.IMAGE,
                    name=f"Image {counts['image']}",
                    properties=props,
                )
            )

        # 4. Text nodes — connected chunks of non-table, non-empty cells
        merged_anchor = _merged_anchor_map(ws)
        occupied: set[tuple[int, int]] = set()
        values: dict[tuple[int, int], str] = {}
        max_row_used, max_col_used = content_max_row, content_max_col
        for r in range(1, max_row_used + 1):
            for c in range(1, max_col_used + 1):
                if _cell_in_any_table(r, c, table_ranges):
                    continue
                anchor = merged_anchor.get((r, c))
                src = anchor if anchor else (r, c)
                src_cell = ws.cell(row=src[0], column=src[1])
                v = src_cell.value
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                occupied.add((r, c))
                # A standalone numeric cell outside any table (e.g. a
                # percentage value next to a separate note) also needs
                # formatting applied, or it repeats the same bug as
                # Table.grid above.
                values[(r, c)] = format_cell_display(v, src_cell.number_format).strip()

        for comp in _connected_components(occupied):
            # Extract unique text keyed only by the original anchor
            # coordinates, so the same value (filled in by a merge) isn't
            # listed more than once.
            seen_text_cells: dict[tuple[int, int], str] = {}
            for r, c in comp:
                anchor = merged_anchor.get((r, c), (r, c))
                seen_text_cells[anchor] = values[(r, c)]
            ordered = sorted(seen_text_cells.items())
            text = " / ".join(t for _, t in ordered)
            min_r = min(r for r, _ in comp)
            min_c = min(c for _, c in comp)
            max_r = max(r for r, _ in comp)
            max_c = max(c for _, c in comp)
            counts["text"] += 1
            content.append(
                Node(
                    id=f"content:{path.name}:s{sheet_idx}:txt{counts['text']}",
                    type=NodeType.TEXT,
                    name=text[:40] + ("…" if len(text) > 40 else ""),
                    properties={
                        "row": min_r,
                        "col": min_c,
                        "range": f"R{min_r}C{min_c}:R{max_r}C{max_c}",
                        "text": text,
                    },
                )
            )

        content.sort(key=lambda n: (n.properties.get("row", 0), n.properties.get("col", 0)))
        nodes.extend(content)
        edges.extend(parent_edges(artifact, content))

        # Same deterministic CAPTION_OF/REFERENCES heuristics docx.py/pdf.py/
        # pptx.py use — a standalone text cell right above/below a
        # table/chart/image (in row/col reading order, same as `content`'s
        # own sort just above) starting with "표 "/"Table "/... is its
        # caption, and any other cell on the sheet citing that same label by
        # name (e.g. "Table 1") gets a REFERENCES edge to it.
        caption_edges = caption_prefix_edges(content)
        edges.extend(caption_edges)
        edges.extend(reference_label_edges(content, caption_edges))

    # Sheets aren't linked by NEXT to each other — NEXT is currently only
    # used for pptx.py's slide order.
    edges.extend(parent_edges(file_n, artifacts))

    if unmeasured_fonts:
        # Once per file, not once per sheet — the font name is also kept on
        # the Artifact node's `column_width_font_measured` (check the graph
        # for which sheet it applies to); this warning is just an immediate
        # signal that such a sheet existed.
        warnings.warn(
            f"{path.name}: could not find the following font(s) locally, so "
            f"column-width calculation fell back to a Calibri approximation: "
            f"{sorted(unmeasured_fonts)} — editAs=\"twoCell\" shape/image "
            "positions may be inaccurate. Installing that font locally makes "
            "the next extraction accurate automatically (though not just any "
            "substitute font will do — see Experiments.md \"Install the "
            "workbook's font locally to improve accuracy\").",
            stacklevel=2,
        )

    return ArticDocument(source_path=str(path.resolve()), format="xlsx", nodes=nodes, edges=edges)
