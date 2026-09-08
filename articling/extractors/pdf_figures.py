"""PDF vector-graphics-based figure candidate detection and capture — finds
diagrams (plots, heatmaps, flowcharts, etc.) drawn directly as
curves/lines/filled shapes rather than raster images.

**Why this is needed**: `extractors/pdf.py`'s main loop only turns
`page.get_text("dict")`'s `type=1` (image) blocks into `Image` nodes — this
only catches images **embedded as raster** in the PDF. A figure drawn
directly as vector graphics (typically a plot rendered straight into the
PDF by matplotlib/TikZ, etc.) doesn't hit this block type and is silently
lost. Confirmed (2026-09-04, `1706.03762` "Attention Is All You Need"): the
3 attention heatmaps (Figure 3/4/5) in the "Attention Visualizations"
section were drawn as hundreds of small colored rectangles, so the "Figure
N. …" caption Text survived but not a single corresponding Image node was
created (see `docs/vlm-integration-research.md` §11).

**Same principle as `pdf_tables.py`** (no VLM, pure vector-graphics
coordinates only) — but the judgment criterion differs. A table looks for
the regular structure of "grid/ruled lines" (the pattern horizontal/vertical
line segments make), but a figure has no regular shape (curves, arrows,
filled shapes, colored cells, etc. vary so widely that structural pattern
matching doesn't work). Instead there's exactly one reliable signal —
density, i.e. **a very large number of vector shapes packed into one
area**. Confirmed (in the document above, counting `page.get_drawings()`
items): simple decorative lines/section dividers/table borders came to
0-53 per page (including pages with a real table), while the 3 vector
figures had 624-1030 — `_MIN_ITEMS_FOR_FIGURE` is chosen with a generous
safety margin between that gap.

**Known limitation**: this density signal is also, conversely, a strong
signal in exactly the content `pdf_tables.py`'s grid detector is prone to
false-positive on (an area densely packed with colored rectangles) — i.e.
it was also confirmed empirically that `detect_table_candidates` can
mistake such a vector-figure area for a table (neither function knows the
other's result), but that's out of this module's scope to fix (when using
`enrich_pdf_tables` alongside this module, the caller has to decide which to
run first). This module doesn't know which areas are already confirmed as
tables, so it doesn't exclude any on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from ..scaffold import save_image_bytes
from ..schema import ArticDocument, Edge, EdgeType, Node, NodeType
from .pdf import _CAPTION_PREFIXES, _normalized_bbox, _resort_pdf_content_nodes

_MIN_ITEMS_FOR_FIGURE = 100  # confirmed (module docstring): 0-53 for decorative lines/table borders vs. 624+ for a real vector figure
_MIN_FIGURE_SIZE = 30.0  # minimum candidate bbox width/height (pt) — same idea as pdf_tables.py's _MIN_TABLE_SIZE
_CLUSTER_GAP_TOL = 4.0  # shapes within this gap (pt) are grouped as forming the same figure
_DEDUP_IOU = 0.5  # skip as a duplicate capture if it overlaps an area already caught as a raster Image by at least this much


@dataclass
class FigureCandidate:
    """One vector-figure candidate. `bbox` is in PDF page coordinates (pt,
    origin at top-left) as (x0, y0, x1, y1). `item_count` is the number of
    vector shapes (lines/curves/rectangles etc.) that make up the candidate
    — kept as-is on the Image node's properties too, for debugging/
    confidence reference."""

    bbox: tuple[float, float, float, float]
    item_count: int


def _bbox_overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _expand(bbox: tuple[float, float, float, float], pad: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def _bbox_center_inside(bbox: tuple[float, float, float, float], region: tuple[float, float, float, float], tol: float = 0.0) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return (region[0] - tol <= cx <= region[2] + tol) and (region[1] - tol <= cy <= region[3] + tol)


def detect_vector_figure_regions(page: pymupdf.Page) -> list[FigureCandidate]:
    """Finds every vector-figure candidate bbox on one page. Groups the
    bboxes of each shape `page.get_drawings()` returns into the same
    cluster when they overlap or are within `_CLUSTER_GAP_TOL` of each
    other (a simple union-find, the same idea as
    `pdf_tables._detect_grid`), and only accepts a cluster as a candidate if
    it has at least `_MIN_ITEMS_FOR_FIGURE` shapes — cell content / what the
    figure actually depicts isn't this function's concern."""
    drawings = page.get_drawings()
    rects = [(d["rect"].x0, d["rect"].y0, d["rect"].x1, d["rect"].y1) for d in drawings if not d["rect"].is_empty]
    n = len(rects)
    if n == 0:
        return []

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

    expanded = [_expand(r, _CLUSTER_GAP_TOL) for r in rects]
    for i in range(n):
        for j in range(i + 1, n):
            if _bbox_overlaps(expanded[i], rects[j]):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    candidates: list[FigureCandidate] = []
    for members in clusters.values():
        if len(members) < _MIN_ITEMS_FOR_FIGURE:
            continue
        x0 = min(rects[i][0] for i in members)
        y0 = min(rects[i][1] for i in members)
        x1 = max(rects[i][2] for i in members)
        y1 = max(rects[i][3] for i in members)
        if x1 - x0 < _MIN_FIGURE_SIZE or y1 - y0 < _MIN_FIGURE_SIZE:
            continue
        candidates.append(FigureCandidate(bbox=(x0, y0, x1, y1), item_count=len(members)))

    return candidates


def enrich_pdf_figures(document: ArticDocument, capture_dir: Path | None = None) -> list[str]:
    """Reopens `document.source_path` (the PDF), finds vector-figure
    candidates with `detect_vector_figure_regions`, crops the page to that
    bbox to capture a raster PNG, and turns it into an `Image` node.
    **Needs no VLM/API key** — pure pymupdf vector-graphics coordinate
    detection + cropping, so unlike `enrich_pdf_tables` there's no model
    confirmation step (see the module docstring — false-positive risk still
    exists, hence keeping this opt-in).

    **This is a separate opt-in step `extract()` doesn't run automatically**
    (it can also be turned on together via `extract(..., enrich_figures=True)`).
    Existing Text/Image nodes inside a candidate region are absorbed and
    disappear (the same "Option A: absorb" principle as `enrich_pdf_tables`
    — a fragment of text like an axis label inside the figure doesn't
    survive as a separate top-level node, but is preserved as-is inside the
    new capture PNG). A candidate that overlaps an area already caught as a
    raster Image by a lot (at or above `_DEDUP_IOU`) is skipped to avoid a
    duplicate capture.

    Like `enrich_pdf_tables`, this doesn't re-attach a CAPTION_OF to the
    newly created Image node — even if a "Figure N." caption is right
    before/after it, this function doesn't link it. That rediscovery is left
    to `relations.propose.propose_edges` (an LLM proposal) — the same
    principle `enrich_pdf_tables` follows for Table (keeping the
    deterministic pipeline's separation of concerns).

    Returns: the list of newly created Image node ids."""
    artifact = next(n for n in document.nodes if n.type == NodeType.ARTIFACT)
    pdf_path = Path(document.source_path)
    capture_root = capture_dir if capture_dir is not None else pdf_path.parent / "captures"
    created: list[str] = []

    pdf = pymupdf.open(str(pdf_path))
    try:
        for page_index in range(pdf.page_count):
            page = pdf[page_index]
            w, h = page.rect.width, page.rect.height
            candidates = detect_vector_figure_regions(page)
            if not candidates:
                continue

            page_nodes = [
                n for n in document.nodes
                if n.type in (NodeType.TEXT, NodeType.IMAGE) and n.properties.get("page_index") == page_index
            ]
            existing_image_bboxes = [
                (n.properties["bbox"]["x_min"], n.properties["bbox"]["y_min"], n.properties["bbox"]["x_max"], n.properties["bbox"]["y_max"])
                for n in page_nodes if n.type == NodeType.IMAGE
            ]

            for i, cand in enumerate(candidates):
                cand_bbox_norm = _normalized_bbox(cand.bbox, w, h)
                cand_norm = (cand_bbox_norm["x_min"], cand_bbox_norm["y_min"], cand_bbox_norm["x_max"], cand_bbox_norm["y_max"])

                if any(_iou(cand_norm, existing) > _DEDUP_IOU for existing in existing_image_bboxes):
                    continue  # an area already captured as a raster image — avoid creating a duplicate

                absorbed = [
                    n for n in page_nodes
                    if _bbox_center_inside(
                        (n.properties["bbox"]["x_min"], n.properties["bbox"]["y_min"], n.properties["bbox"]["x_max"], n.properties["bbox"]["y_max"]),
                        cand_norm,
                    )
                    # A "Figure N."-style caption is never absorbed even if
                    # its bbox slightly overlaps the figure candidate — the
                    # caption has to stay a top-level Text so
                    # propose_edges (LLM) has something to later attach a
                    # CAPTION_OF to (confirmed: found a case where Figure
                    # 4's caption bbox overlapped the vector content on the
                    # y-axis and nearly got absorbed).
                    and not (n.type == NodeType.TEXT and n.properties.get("text", "").startswith(_CAPTION_PREFIXES))
                ]

                pixmap = page.get_pixmap(clip=pymupdf.Rect(*cand.bbox), dpi=200)
                stem = f"{pdf_path.stem}__figure_p{page_index}_{i}"
                saved = save_image_bytes(capture_root, stem, pixmap.tobytes("png"), "png")

                image_node = Node(
                    id=f"content:{pdf_path.name}:figure_p{page_index}_{i}",
                    type=NodeType.IMAGE,
                    name=f"Vector figure (p.{page_index + 1})",
                    properties={
                        "page_index": page_index,
                        "bbox": cand_bbox_norm,
                        "image_path": str(saved.resolve()),
                        "detection_mode": "vector_figure",
                        "vector_item_count": cand.item_count,
                    },
                )

                absorbed_ids = {n.id for n in absorbed}
                document.nodes = [n for n in document.nodes if n.id not in absorbed_ids]
                document.edges = [
                    e for e in document.edges
                    if e.source_id not in absorbed_ids and e.target_id not in absorbed_ids
                ]
                document.nodes.append(image_node)
                document.edges.append(Edge(type=EdgeType.PARENT_OF, source_id=artifact.id, target_id=image_node.id))
                created.append(image_node.id)
    finally:
        pdf.close()

    if created:
        _resort_pdf_content_nodes(document)

    return created
