"""PDF table candidate bbox detection — a deterministic heuristic that only
looks at vector graphics (lines/rectangles).

Uses no VLM/LLM at all. Since PDF has no notion of a table in the format
itself (see the `extractors/pdf.py` docstring), this cheaply finds only the
areas that look like a table among the lines/rectangles actually drawn as
vector graphics on the page — stage 1 of the "MinerU2.5-style two-stage"
approach (`docs/vlm-integration-research.md` §2 priority 3), which crops
just the found bboxes and hands them to the model in
`relations/table_structure.py` instead of feeding the whole page to a VLM.

Empirically, real tables split into two styles (both supported):

- **Boxed (grid)** — a table with a border around every cell (common in
  Excel/Word). Horizontal and vertical lines meet to form a grid. Same idea
  as `extractors/xlsx.py`'s `_border_table_ranges`, but looking at
  arbitrary-coordinate vector lines instead of a cell grid.
- **Ruled rows (booktabs)** — a table with only horizontal rules and no
  vertical lines at all (a style common in academic papers: a top rule /
  header rule / bottom rule). Boxed detection completely misses this
  because there are no vertical lines — confirmed as a real case (Table 1
  of `2501.17887v1_docling.pdf`), so it's handled as a separate mode.

Both look only at pure vector-graphics coordinates, not text layout, so
they have no idea what's inside the table — this module only outputs
position information (a bbox) saying "there's something table-shaped here."
The actual cell content (grid) is either filled in by `extractors/pdf.py`
absorbing the Text nodes inside that bbox to preserve at least the raw text
(a fallback), or by `relations/table_structure.py`'s model-based reading.
"""
from __future__ import annotations

from dataclasses import dataclass

import pymupdf

_LINE_TOL = 1.0  # within this much tolerance, treated as "exactly horizontal/vertical" (pt)
_XSPAN_TOL = 3.0  # ruled-rows mode: x0/x1 tolerance for counting lines as on the same row (pt)
_MIN_TABLE_SIZE = 15.0  # minimum candidate bbox width/height (pt) — filters out noise like underlines
_PAD = 2.0  # margin added to a candidate bbox (pt)


@dataclass
class TableCandidate:
    """One table candidate. `bbox` is in PDF page coordinates (pt, not
    top-left-origin necessarily — pymupdf's default: top-left is (0,0)), as
    (x0, y0, x1, y1)."""

    bbox: tuple[float, float, float, float]
    mode: str  # "grid" | "ruled_rows" — which heuristic found it (for debugging/confidence reference)


def _iter_line_segments(page: pymupdf.Page) -> list[tuple[str, float, float, float, float]]:
    """Extracts only perfectly horizontal/vertical line segments from
    `page.get_drawings()`.
    Returns: instead of (orientation, a, b0, b1, ...), simply a list of
    ("h", x0, x1, y) or ("v", y0, y1, x) tuples."""
    segments: list[tuple[str, float, float, float, float]] = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < _LINE_TOL and abs(p1.x - p2.x) > _LINE_TOL:
                    segments.append(("h", min(p1.x, p2.x), max(p1.x, p2.x), p1.y, 0.0))
                elif abs(p1.x - p2.x) < _LINE_TOL and abs(p1.y - p2.y) > _LINE_TOL:
                    segments.append(("v", min(p1.y, p2.y), max(p1.y, p2.y), p1.x, 0.0))
            elif item[0] == "re":
                rect = item[1]
                x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
                if x1 - x0 < _LINE_TOL or y1 - y0 < _LINE_TOL:
                    continue
                # decompose the rectangle's 4 sides into 4 segments — reused as grid lines by boxed mode
                segments.append(("h", x0, x1, y0, 0.0))
                segments.append(("h", x0, x1, y1, 0.0))
                segments.append(("v", y0, y1, x0, 0.0))
                segments.append(("v", y0, y1, x1, 0.0))
    return segments


def _detect_ruled_rows(segments: list[tuple[str, float, float, float, float]]) -> list[TableCandidate]:
    """Detects a table made up of horizontal lines only, with no vertical
    lines (booktabs style). If horizontal lines sharing the same x-span
    (tolerance `_XSPAN_TOL`) appear at 2 or more different y positions, that
    range is treated as a table candidate — the pattern observed in a real
    academic paper's Table (top rule / header rule / bottom rule)."""
    horiz = [(x0, x1, y) for kind, x0, x1, y, _ in segments if kind == "h"]
    used = [False] * len(horiz)
    candidates: list[TableCandidate] = []

    for i, (x0_i, x1_i, y_i) in enumerate(horiz):
        if used[i]:
            continue
        group = [i]
        for j in range(i + 1, len(horiz)):
            if used[j]:
                continue
            x0_j, x1_j, y_j = horiz[j]
            if abs(x0_i - x0_j) <= _XSPAN_TOL and abs(x1_i - x1_j) <= _XSPAN_TOL:
                group.append(j)
        if len(group) < 2:
            continue
        for idx in group:
            used[idx] = True
        ys = [horiz[idx][2] for idx in group]
        x0 = min(horiz[idx][0] for idx in group)
        x1 = max(horiz[idx][1] for idx in group)
        bbox = (x0 - _PAD, min(ys) - _PAD, x1 + _PAD, max(ys) + _PAD)
        if bbox[2] - bbox[0] >= _MIN_TABLE_SIZE and bbox[3] - bbox[1] >= _MIN_TABLE_SIZE:
            candidates.append(TableCandidate(bbox=bbox, mode="ruled_rows"))

    return candidates


def _detect_grid(segments: list[tuple[str, float, float, float, float]]) -> list[TableCandidate]:
    """Detects a table (boxed) where horizontal and vertical lines meet to
    form a grid. Groups segments that overlap or are adjacent into one
    cluster (a simple union-find), and treats a cluster as a candidate when
    it has both horizontal and vertical lines **and** either 2+ distinct
    horizontal y-positions or 2+ distinct vertical x-positions — a single
    plain rectangular border (just 2 horizontal + 2 vertical lines) alone is
    too weak a basis to call a "table," so it's only recognized as one when
    there's an internal divider line (3+ horizontal or 3+ vertical lines)."""
    if not segments:
        return []

    def seg_bbox(s: tuple[str, float, float, float, float]) -> tuple[float, float, float, float]:
        kind, a0, a1, b, _ = s
        if kind == "h":
            return (a0, b, a1, b)
        return (b, a0, b, a1)

    tol = 2.0

    def connected(i: int, j: int) -> bool:
        """Only treats two segments as the same grid when they actually meet
        or overlap — a naive bbox-proximity check (like `overlaps`, looking
        only at an absolute tolerance) had a bug where unrelated shapes
        chained together across the whole page (confirmed: an entire page
        of a Korean report got caught as a single table candidate). Instead
        this only checks "do a horizontal and a vertical line actually
        cross/touch" and "do same-direction segments overlap or touch"."""
        ki, ai0, ai1, bi, _ = segments[i]
        kj, aj0, aj1, bj, _ = segments[j]
        if ki == kj:
            # same direction (both horizontal or both vertical): must be at the same position (b) and their spans overlap or touch
            if abs(bi - bj) > tol:
                return False
            return not (ai1 + tol < aj0 or aj1 + tol < ai0)
        # one horizontal + one vertical: a real crossing/touch requires the
        # vertical line's x to fall within the horizontal line's x span, and
        # the horizontal line's y to fall within the vertical line's y span
        h_a0, h_a1, h_b = (ai0, ai1, bi) if ki == "h" else (aj0, aj1, bj)
        v_a0, v_a1, v_b = (ai0, ai1, bi) if ki == "v" else (aj0, aj1, bj)
        return (h_a0 - tol <= v_b <= h_a1 + tol) and (v_a0 - tol <= h_b <= v_a1 + tol)

    n = len(segments)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    boxes = [seg_bbox(s) for s in segments]
    for i in range(n):
        for j in range(i + 1, n):
            if connected(i, j):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    candidates: list[TableCandidate] = []
    for members in clusters.values():
        h_ys = {round(segments[i][3], 1) for i in members if segments[i][0] == "h"}
        v_xs = {round(segments[i][3], 1) for i in members if segments[i][0] == "v"}
        if not h_ys or not v_xs:
            continue
        if len(h_ys) < 3 and len(v_xs) < 3:
            continue  # just a rectangular border with no internal divider — not enough basis to call it a table
        xs0 = min(boxes[i][0] for i in members)
        ys0 = min(boxes[i][1] for i in members)
        xs1 = max(boxes[i][2] for i in members)
        ys1 = max(boxes[i][3] for i in members)
        bbox = (xs0 - _PAD, ys0 - _PAD, xs1 + _PAD, ys1 + _PAD)
        if bbox[2] - bbox[0] >= _MIN_TABLE_SIZE and bbox[3] - bbox[1] >= _MIN_TABLE_SIZE:
            candidates.append(TableCandidate(bbox=bbox, mode="grid"))

    return candidates


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def detect_table_candidates(page: pymupdf.Page) -> list[TableCandidate]:
    """Finds every table candidate bbox on one page (boxed + ruled-rows
    merged, overlapping candidates combined into one). Provides only the
    minimum information (a bbox) needed to make one crop image per table —
    cell content isn't this function's concern."""
    segments = _iter_line_segments(page)
    candidates = _detect_grid(segments) + _detect_ruled_rows(segments)

    # merge candidates that overlap by IoU (the same table can be caught by both modes at once) — keep the larger one
    merged: list[TableCandidate] = []
    for cand in sorted(candidates, key=lambda c: -(c.bbox[2] - c.bbox[0]) * (c.bbox[3] - c.bbox[1])):
        if any(_iou(cand.bbox, m.bbox) > 0.3 for m in merged):
            continue
        merged.append(cand)

    return merged
