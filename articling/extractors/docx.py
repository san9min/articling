"""DOCX -> ArticDocument, a fully native path (python-docx only, no LibreOffice/VLM needed).

Walks `document.element.body` in raw XML order so paragraphs (w:p) and
tables (w:tbl) are processed in the order they actually appear — python-docx's
`document.paragraphs`/`document.tables` are each iterated separately and lose
the original document order, so the body is walked directly instead.

Node: Text (paragraph), Table (cell grid; a picture inside one of its cells
      is absorbed as `embedded_images` metadata rather than a separate node
      — the same "Option A: absorb" rule xlsx.py applies to images inside a
      table range, minus the composited capture PNG, since DOCX table
      column widths/row heights are frequently unset and python-docx never
      computes them, so there's no reliable pixel grid to paste onto),
      Image (an inline **raster** image outside any table. A vector shape
      like an arrow/textbox (`w:drawing` present but no `a:blip`) never
      becomes an Image — judging by `w:drawing` presence alone would
      over-include vector shapes too)
Edge: PARENT_OF (Artifact/Table -> content), CAPTION_OF (a heuristic
      proposal — a Text starting with "표 "/"그림 "/"Table "/"Figure " is
      proposed as describing the immediately preceding/following Table/
      Image. A proposal for human review, not a final answer). NEXT is an
      edge only used for order between Artifacts (slides/sheets), and since
      DOCX has only one Artifact ("the whole document"), NEXT is never
      created at all.
"""
from __future__ import annotations

from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from ..schema import ArticDocument, Edge, EdgeType, Node, NodeType
from ..scaffold import file_node, parent_edges, save_image_bytes

_CAPTION_PREFIXES = ("표 ", "그림 ", "Table ", "Figure ", "<표", "<그림", "[표", "[그림")
CAPTURE_SUBDIR = "captures"


def _paragraph_has_image(p: Paragraph) -> bool:
    """True only for a paragraph that has an actual raster image (`a:blip`).

    Judging by `w:drawing` presence alone would catch not just photos but
    vector shapes like arrows/textboxes (`wsp`/`txbxContent`) too. `a:blip`
    is the same tag `_paragraph_image_bytes` looks at when pulling out image
    bytes, so the test criterion and what can actually be saved always
    agree.
    """
    return bool(p._p.findall(".//" + qn("a:blip")))


def _paragraph_image_bytes(p: Paragraph) -> tuple[bytes, str] | None:
    """The raw bytes + extension of the first image in a paragraph. If a
    paragraph has multiple images (rare), only the first is used — matching
    how the current schema only creates one Image node per paragraph (see
    where `_paragraph_has_image` is used).
    """
    blips = p._p.findall(f".//{qn('a:blip')}")
    if not blips:
        return None
    rid = blips[0].get(qn("r:embed"))
    if not rid:
        return None
    part = p.part.related_parts.get(rid)
    if part is None:
        return None
    return part.blob, Path(part.partname).suffix or ".png"


def extract(path: Path, capture_dir: Path | None = None) -> ArticDocument:
    """`capture_dir`: the directory to save inline image originals into
    (default: `captures/` next to `path`). Same purpose as Table's visual
    capture (xlsx) — keeps the raw bytes around for a pixel-based
    post-process like a VLM caption."""
    capture_root = capture_dir if capture_dir is not None else path.parent / CAPTURE_SUBDIR
    d = docx.Document(str(path))
    file_n = file_node(path)
    artifact = Node(
        id=f"artifact:{path.name}:doc",
        type=NodeType.ARTIFACT,
        name=path.stem,
        properties={"kind": "whole_document"},
    )
    nodes: list[Node] = [file_n, artifact]
    edges: list[Edge] = [Edge(type=EdgeType.PARENT_OF, source_id=file_n.id, target_id=artifact.id)]

    content_nodes: list[Node] = []
    body = d.element.body
    counters = {"p": 0, "tbl": 0, "img": 0}

    # walk body's children in XML order, mapping each to its real python-docx object (Paragraph/Table)
    tables_by_element = {t._tbl: t for t in d.tables}
    paragraphs_by_element = {p._p: p for p in d.paragraphs}

    for child in body:
        tag = child.tag
        if tag == qn("w:p"):
            para = paragraphs_by_element.get(child)
            if para is None:
                continue
            text = para.text.strip()
            if _paragraph_has_image(para):
                counters["img"] += 1
                props = {"order": len(content_nodes), "nearby_text": text[:80]}
                image_bytes = _paragraph_image_bytes(para)
                if image_bytes is not None:
                    data, ext = image_bytes
                    saved = save_image_bytes(capture_root, f"{path.stem}__img{counters['img']}", data, ext)
                    props["image_path"] = str(saved.resolve())
                img_node = Node(
                    id=f"content:{path.name}:img{counters['img']}",
                    type=NodeType.IMAGE,
                    name=f"Inline image {counters['img']}",
                    properties=props,
                )
                content_nodes.append(img_node)
            if text:
                counters["p"] += 1
                text_node = Node(
                    id=f"content:{path.name}:p{counters['p']}",
                    type=NodeType.TEXT,
                    name=text[:40] + ("…" if len(text) > 40 else ""),
                    properties={
                        "order": len(content_nodes),
                        "text": text,
                        "style": para.style.name if para.style else None,
                    },
                )
                content_nodes.append(text_node)
        elif tag == qn("w:tbl"):
            table = tables_by_element.get(child)
            if table is None:
                continue
            grid = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            counters["tbl"] += 1
            table_id = f"content:{path.name}:tbl{counters['tbl']}"
            table_node = Node(
                id=table_id,
                type=NodeType.TABLE,
                name=f"Table {counters['tbl']} ({len(grid)} rows x {len(grid[0]) if grid else 0} cols)",
                properties={"order": len(content_nodes), "grid": grid},
            )
            content_nodes.append(table_node)

            # "Option A: absorb" (xlsx.py's rule for images inside a table
            # range, applied here without a composited capture PNG — see the
            # module docstring): don't miss a picture inside a table cell
            # (looking at cell.text alone silently loses it), but don't give
            # it its own node either. A merged cell has the same _tc element
            # appear multiple times in row.cells, so it's only processed
            # once.
            embedded_images: list[dict] = []
            seen_cells = set()
            for ri, row in enumerate(table.rows):
                for ci, cell in enumerate(row.cells):
                    if cell._tc in seen_cells:
                        continue
                    seen_cells.add(cell._tc)
                    for cell_para in cell.paragraphs:
                        if not _paragraph_has_image(cell_para):
                            continue
                        counters["img"] += 1
                        entry = {
                            "table_row": ri,
                            "table_col": ci,
                            "nearby_text": cell_para.text.strip()[:80],
                        }
                        cell_image_bytes = _paragraph_image_bytes(cell_para)
                        if cell_image_bytes is not None:
                            data, ext = cell_image_bytes
                            saved = save_image_bytes(capture_root, f"{path.stem}__img{counters['img']}", data, ext)
                            entry["image_path"] = str(saved.resolve())
                        embedded_images.append(entry)
            if embedded_images:
                table_node.properties["embedded_images"] = embedded_images
                table_node.properties["embedded_image_count"] = len(embedded_images)

    nodes.extend(content_nodes)
    edges.extend(parent_edges(artifact, content_nodes))

    # CAPTION_OF heuristic proposal (a proposal for human review — not final)
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

    return ArticDocument(source_path=str(path.resolve()), format="docx", nodes=nodes, edges=edges)
