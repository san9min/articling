"""PDF -> ArticDocument, a fully native path (pymupdf only, no LibreOffice/VLM/OCR needed).

PDF has no notion of a table in the format itself (characters are just
placed at coordinates, so text blocks alone can't tell you a table's
boundary) — this path deliberately never creates a Table (a vector-graphics-
based table-detection heuristic for PDF is left as separate work, see
`docs/docling-compat-harness.md` §3-a).

**Schema decision**: like DOCX, PDF is treated as the whole file = one
Artifact (not split per page, see the `schema.py` docstring). So no NEXT
edge is ever created (only one Artifact — same reason as docx.py). Page
boundary information isn't lost though: every content node has a
`page_index` (0-based).

Uses `page.get_text("dict", sort=True)` blocks (`type 0` = text, `type 1` =
image) as-is — pymupdf already groups them at the paragraph level so no
extra clustering is needed, and an image block already carries the raw
bytes (`block["image"]`), so no separate extraction call is needed.

**Why `sort=True` is needed**: without `sort`, blocks come out in PDF
content-stream order (the order text boxes were created — effectively
z-order in a document exported from PPT). Empirically confirmed
(`docs/docling-compat-harness.md` §4a) that a document title at the very top
of the page showed up 15th out of 16 in the node list, unrelated to visual
position — since `relations/propose.py`'s `build_context_windows` assumes
"node list order ≈ document order," without this sort a caption candidate
would get pushed outside the window from the start. `sort=True` is
pymupdf's built-in top-to-bottom/left-to-right sort — not perfect column
recognition (a complex multi-column layout can still mix some columns), but
confirmed to be a big improvement over raw stream order on both documents
tested.

**Why `_reorder_same_line_blocks` is needed**: there's one more spot where
`sort=True` isn't perfect — it doesn't always get the left-to-right order
right within blocks that share the same line (overlapping y) when a line
gets split into several blocks. This shows up especially with inline math
that has sub/superscripts: MuPDF splits one line into multiple blocks
because of slightly different font size/baseline spans, and scrambles their
relative order (confirmed: `1706.03762`'s MultiHead attention formula came
out with "Q, K, V" order as "Q, V, K", see
`docs/vlm-integration-research.md` §11). Text is never merged — merging
would be riskier (the same document also had two authors' names sitting
side by side on the same line: "Ashish Vaswani∗" and "Noam Shazeer∗" —
merging those would corrupt the data instead). Instead, **only order is
fixed, in ascending x, among blocks whose y overlaps** — "read left-first
when at the same height" is always safe whether it's a math fragment,
side-by-side subheadings, or a list of author names (no counterexample
found). Since only order changes and each block still stays an independent
Node, there's no risk of misjudging what to merge and mashing different
semantic units together.

Node: Text (paragraph block), Image (raster image block — there's no notion
      of "inside a table cell" for PDF, so it's always top-level content).
Edge: PARENT_OF (Artifact -> content), CAPTION_OF (the same deterministic
      heuristic as docx.py/pptx.py — a Text starting with "표 "/"그림 "/
      "Table "/"Figure " is proposed as describing the immediately
      preceding/following Image. A proposal for human review, not a final
      answer). The LLM proposal (`relations/propose.py`) alone often misses
      even an explicit caption label, so the same deterministic heuristic as
      docx.py/pptx.py is kept here too (`docs/vlm-integration-research.md`
      §7.3).
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

from ..schema import ArticDocument, Edge, EdgeType, Node, NodeType
from ..scaffold import file_node, parent_edges, save_image_bytes

_CAPTION_PREFIXES = ("표 ", "그림 ", "Table ", "Figure ", "<표", "<그림", "[표", "[그림")
CAPTURE_SUBDIR = "captures"
_LINE_Y_OVERLAP = 0.5  # y must overlap at least this much to count as "the same line" and get its x order fixed
_LINE_MAX_HEIGHT = 30.0  # only considered a reorder candidate when both are at or under this height (pt) (explained below)


def _y_overlap_frac(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    y0, y1 = max(a[1], b[1]), min(a[3], b[3])
    if y1 <= y0:
        return 0.0
    return (y1 - y0) / min(a[3] - a[1], b[3] - b[1])


def _reorder_same_line_blocks(blocks: list[dict]) -> list[dict]:
    """See the module docstring's "Why `_reorder_same_line_blocks` is
    needed" — groups blocks whose y overlaps by at least `_LINE_Y_OVERLAP`
    (union-find) and reorders only within each group in ascending x. Text is
    never merged; only the block list's order changes — a block that
    doesn't overlap anything (alone) stays exactly where it is.

    Only grouped when both are at or under `_LINE_MAX_HEIGHT` — otherwise a
    tall block (a rotated watermark, a multi-line paragraph) would bridge
    several unrelated "lines" that happen to fall within its y range
    (union-find's transitive connectivity), lumping blocks from completely
    different positions into one group and scrambling their x order
    (confirmed: `1706.03762` page 1 — a tall arXiv watermark grouped the
    title/authors/Abstract all together, pushing the "Abstract" heading
    behind the body text and making the order worse than before, see
    `docs/vlm-integration-research.md` §11). In the documents checked,
    sub/superscript math fragments and author name/affiliation blocks were
    all 30pt or under, while paragraph/footnote/watermark blocks were 90pt
    or more — a wide enough gap."""
    n = len(blocks)
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

    def _height(bbox: tuple[float, float, float, float]) -> float:
        return bbox[3] - bbox[1]

    for i in range(n):
        if _height(blocks[i]["bbox"]) > _LINE_MAX_HEIGHT:
            continue
        for j in range(i + 1, n):
            if _height(blocks[j]["bbox"]) > _LINE_MAX_HEIGHT:
                continue
            if _y_overlap_frac(blocks[i]["bbox"], blocks[j]["bbox"]) >= _LINE_Y_OVERLAP:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    result: list[dict | None] = [None] * n
    for members in clusters.values():
        positions = sorted(members)  # the original slots (keeps document order)
        by_x = sorted(members, key=lambda i: blocks[i]["bbox"][0])  # the order to fill those slots (left to right)
        for pos, idx in zip(positions, by_x):
            result[pos] = blocks[idx]
    return result


def _normalized_bbox(bbox: tuple[float, float, float, float], width: float, height: float) -> dict:
    """0-1000 normalized coordinates (origin at top-left) — matches the
    `bounding_box` convention in `vlm_metadata_extraction/schema.py`, so it
    can be reused as-is later for a table-detection heuristic (§4-1) or VLM
    grounding."""
    x0, y0, x1, y1 = bbox
    return {
        "x_min": round(x0 / width * 1000),
        "y_min": round(y0 / height * 1000),
        "x_max": round(x1 / width * 1000),
        "y_max": round(y1 / height * 1000),
    }


def _resort_pdf_content_nodes(document: ArticDocument) -> None:
    """Re-sorts content nodes by (page_index, y_min) after
    `enrich_pdf_tables`/`enrich_pdf_figures` have absorbed/added to them —
    since the newly created Table/Image nodes were just appended to the end
    of the list, this restores the property "node order ≈ document order"
    (hard-won above via `sort=True`) that `relations/propose.py`'s
    `build_context_windows` assumes, so it doesn't get broken. Why
    `extractors/pdf.py` owns this: like `_normalized_bbox`, it's the
    lowest-level helper dealing with the sort-order invariant of PDF content
    nodes, so it belongs here and gets pulled in from `relations/` (not the
    other way around) — that's the correct dependency direction."""
    def sort_key(n: Node):
        page = n.properties.get("page_index", 0)
        y = n.properties.get("bbox", {}).get("y_min", 0)
        return (page, y)

    head = [n for n in document.nodes if n.type in (NodeType.FILE, NodeType.ARTIFACT)]
    content = [n for n in document.nodes if n.type not in (NodeType.FILE, NodeType.ARTIFACT)]
    content.sort(key=sort_key)
    document.nodes = head + content


def extract(
    path: Path,
    capture_dir: Path | None = None,
    enrich_tables: bool = False,
    table_backend: str = "openai",
    enrich_figures: bool = False,
    **table_kwargs,
) -> ArticDocument:
    """`capture_dir`: the directory to save inline image originals into
    (default: `captures/` next to `path`) — the same convention as
    docx.py/pptx.py/xlsx.py (keeps raw bytes around for a pixel-based
    post-process like a VLM caption).

    If `enrich_tables=True`, `relations.table_structure.enrich_pdf_tables`
    is automatically run right before returning, to fill in Table nodes —
    **the default is False**. It's been a project-wide invariant that this
    extractor (and the `articling.extract()` top-level dispatcher) is fully
    native, offline, and deterministic (stated repeatedly in
    README/AGENTS.md/the user-facing SKILL.md, and the related tests assume
    they run with no API key) — turning this option on breaks exactly that
    part (the OpenAI backend needs `OPENAI_API_KEY` + network, and results
    can vary call to call), so it's made to turn on only via explicit opt-in
    (`articling.extract()` doesn't take this argument — following the
    existing convention that a format-specific option like `capture_dir` is
    accessed by calling `articling.extractors.pdf.extract` directly).

    `table_backend`/`table_kwargs` are passed straight through to
    `enrich_pdf_tables` (`backend="granite_docling"`,
    `verify_with_openai=True`, etc.). There's a cost to reopening the PDF
    (`enrich_pdf_tables` opens it separately via `source_path`) — if you
    want to fill tables into an `ArticDocument` you already built, calling
    `relations.table_structure.enrich_pdf_tables` directly gives the same
    result.

    If `enrich_figures=True`, `pdf_figures.enrich_pdf_figures` is
    automatically run right before returning, to find figures drawn
    **directly as vector graphics (lines/curves/filled shapes) rather than
    raster** (plots, heatmaps, etc.) and turn them into Image nodes — the
    main loop above only looks at `page.get_text("dict")`'s `type=1`
    (image) blocks, so such vector figures are otherwise silently lost
    (confirmed: `1706.03762`'s "Attention Visualizations" figures — the
    attention heatmaps were drawn as hundreds of small colored rectangles,
    so only the caption Text survived and not a single Image node was
    created, see `docs/vlm-integration-research.md` §11). Unlike
    `enrich_tables`, this **needs no VLM/API key** (pure pymupdf
    vector-graphics coordinate detection — see the `pdf_figures.py`
    docstring) — but the default is still False: this heuristic is also a
    separate judgment with some false-positive risk (for the same reason as
    `enrich_tables`), so it keeps the explicit opt-in. Calling
    `pdf_figures.enrich_pdf_figures` directly gives the same result."""
    capture_root = capture_dir if capture_dir is not None else path.parent / CAPTURE_SUBDIR
    pdf = pymupdf.open(str(path))
    file_n = file_node(path)
    artifact = Node(
        id=f"artifact:{path.name}:doc",
        type=NodeType.ARTIFACT,
        name=path.stem,
        properties={"kind": "whole_document", "page_count": pdf.page_count},
    )
    nodes: list[Node] = [file_n, artifact]
    edges: list[Edge] = [Edge(type=EdgeType.PARENT_OF, source_id=file_n.id, target_id=artifact.id)]

    content_nodes: list[Node] = []
    counters = {"p": 0, "img": 0}

    try:
        for page_index in range(pdf.page_count):
            page = pdf[page_index]
            w, h = page.rect.width, page.rect.height
            raw = page.get_text("dict", sort=True)
            page_blocks = _reorder_same_line_blocks(raw["blocks"])

            for block in page_blocks:
                bbox = _normalized_bbox(block["bbox"], w, h)
                if block["type"] == 0:  # text block
                    text = "\n".join(
                        "".join(span["text"] for span in line["spans"]) for line in block["lines"]
                    ).strip()
                    if not text:
                        continue
                    counters["p"] += 1
                    content_nodes.append(Node(
                        id=f"content:{path.name}:p{counters['p']}",
                        type=NodeType.TEXT,
                        name=text[:40] + ("…" if len(text) > 40 else ""),
                        properties={
                            "order": len(content_nodes),
                            "text": text,
                            "page_index": page_index,
                            "bbox": bbox,
                        },
                    ))
                elif block["type"] == 1:  # image block — already carries the raw bytes
                    counters["img"] += 1
                    props = {
                        "order": len(content_nodes),
                        "page_index": page_index,
                        "bbox": bbox,
                    }
                    data = block.get("image")
                    if data:
                        ext = block.get("ext") or "png"
                        saved = save_image_bytes(
                            capture_root, f"{path.stem}__img{counters['img']}", data, ext
                        )
                        props["image_path"] = str(saved.resolve())
                    content_nodes.append(Node(
                        id=f"content:{path.name}:img{counters['img']}",
                        type=NodeType.IMAGE,
                        name=f"Inline image {counters['img']} (p.{page_index + 1})",
                        properties=props,
                    ))
    finally:
        pdf.close()

    nodes.extend(content_nodes)
    edges.extend(parent_edges(artifact, content_nodes))

    # CAPTION_OF heuristic proposal (a proposal for human review — not final, same as docx.py/pptx.py)
    for i, n in enumerate(content_nodes):
        if n.type != NodeType.TEXT:
            continue
        text = n.properties.get("text", "")
        if not text.startswith(_CAPTION_PREFIXES):
            continue
        neighbors = (
            content_nodes[i - 1] if i > 0 else None,
            content_nodes[i + 1] if i + 1 < len(content_nodes) else None,
        )
        for neighbor in neighbors:
            if neighbor is not None and neighbor.type in (NodeType.TABLE, NodeType.IMAGE):
                edges.append(Edge(type=EdgeType.CAPTION_OF, source_id=n.id, target_id=neighbor.id))

    document = ArticDocument(source_path=str(path.resolve()), format="pdf", nodes=nodes, edges=edges)

    if enrich_tables:
        from ..relations.table_structure import enrich_pdf_tables  # lazy import — avoids a circular import and keeps openai/torch unneeded when this option isn't used

        enrich_pdf_tables(document, backend=table_backend, capture_dir=capture_root, **table_kwargs)

    if enrich_figures:
        from .pdf_figures import enrich_pdf_figures  # lazy import — avoids a circular import (pdf_figures.py imports this module)

        enrich_pdf_figures(document, capture_dir=capture_root)

    return document
