"""Deterministic scaffold — File/Artifact nodes and PARENT_OF/NEXT edges.

Uses no VLM or LLM at all. An Artifact (slide/tab/whole document) is
determined by the file structure itself, so this stage is a deterministic
computation that cannot fail.
"""
from __future__ import annotations

from pathlib import Path

from .schema import Edge, EdgeType, Node, NodeType

CAPTURE_SUBDIR = "captures"

# A "표 "/"그림 "/"Table "/"Figure "/... prefix immediately before a
# Table/Image is a cross-format signal, not a per-extractor one — docx.py,
# pdf.py and pptx.py all treat the same prefix set as a caption marker.
CAPTION_PREFIXES = ("표 ", "그림 ", "Table ", "Figure ", "<표", "<그림", "[표", "[그림")


def resolve_capture_dir(source_path: Path, capture_dir: Path | None) -> Path:
    """Resolve the directory an extractor should write visual captures into.

    Every extractor accepts an optional `capture_dir` override and otherwise
    defaults to a `captures/` sibling of the source file — centralized here
    so the default name can't drift between formats (see docx.py/pptx.py/
    pdf.py/xlsx.py, all of which pass their own `path`/`capture_dir` through
    this same rule).
    """
    return capture_dir if capture_dir is not None else source_path.parent / CAPTURE_SUBDIR


def file_node(path: Path) -> Node:
    return Node(
        id=f"file:{path.name}",
        type=NodeType.FILE,
        name=path.name,
        properties={"path": str(path.resolve())},
    )


def save_image_bytes(capture_dir: Path, stem: str, data: bytes, ext: str = ".png") -> Path:
    """Save the raw image bytes under `capture_dir` and return the path.

    Serves the same purpose as a Table node's visual capture
    (`capture/xlsx_capture.py`'s `capture_path`) — an Image node's raw bytes
    need to stick around too, in case a pixel-based post-process (e.g. a VLM
    caption) gets attached later.
    """
    capture_dir.mkdir(parents=True, exist_ok=True)
    if not ext.startswith("."):
        ext = f".{ext}"
    out_path = capture_dir / f"{stem}{ext}"
    out_path.write_bytes(data)
    return out_path


def parent_edges(parent: Node, children: list[Node]) -> list[Edge]:
    """Create only PARENT_OF edges from an Artifact (or Table) to its
    content nodes. File -> Artifact is also built with this function (e.g.
    xlsx.py's sheet Artifacts — sheets aren't linked to each other by NEXT).

    `NEXT` is currently only used for pptx.py's slide order — it never links
    content nodes within one Artifact, nor is it used when the Artifact
    isn't a slide (a sheet or a whole document).
    """
    return [Edge(type=EdgeType.PARENT_OF, source_id=parent.id, target_id=n.id) for n in children]


def caption_prefix_edges(content_nodes: list[Node]) -> list[Edge]:
    """CAPTION_OF heuristic proposal (safe to add straight to `doc.edges` —
    see the articling-conventions skill's "two trust tiers" note; this is
    the deterministic tier, not an LLM proposal).

    A Text node whose text starts with a caption-style prefix (see
    `CAPTION_PREFIXES`) gets a CAPTION_OF edge to whichever of its immediate
    neighbors in `content_nodes` (by reading order, one before/one after) is
    a Table or Image. Shared by docx.py/pdf.py/pptx.py, since all three
    define "immediately before/after" the same way (adjacency within one
    Artifact's content list); an extractor may end up attaching the same
    heuristic edge to both a preceding and a following candidate on purpose,
    leaving disambiguation to review or to
    `relations.propose.resolve_ambiguous_captions`.
    """
    edges: list[Edge] = []
    for i, n in enumerate(content_nodes):
        if n.type != NodeType.TEXT:
            continue
        text = n.properties.get("text", "")
        if not text.startswith(CAPTION_PREFIXES):
            continue
        neighbors = (
            content_nodes[i - 1] if i > 0 else None,
            content_nodes[i + 1] if i + 1 < len(content_nodes) else None,
        )
        for neighbor in neighbors:
            if neighbor is not None and neighbor.type in (NodeType.TABLE, NodeType.IMAGE):
                edges.append(Edge(type=EdgeType.CAPTION_OF, source_id=n.id, target_id=neighbor.id))
    return edges


def check_invariants(nodes: list[Node], edges: list[Edge]) -> list[str]:
    """Check graph invariants: PARENT_OF fan-in<=1 (excluding File), NEXT
    chains are intact, no cycles, no dangling edges. Returns a list of
    human-readable descriptions of any problems found (empty list = no
    problems)."""
    problems: list[str] = []
    node_ids = {n.id for n in nodes}

    for e in edges:
        if e.source_id not in node_ids or e.target_id not in node_ids:
            problems.append(f"dangling edge: {e.type} {e.source_id} -> {e.target_id}")

    parent_of = [e for e in edges if e.type == EdgeType.PARENT_OF]
    incoming: dict[str, int] = {}
    for e in parent_of:
        incoming[e.target_id] = incoming.get(e.target_id, 0) + 1
    for target_id, count in incoming.items():
        if count > 1:
            problems.append(f"{target_id} has PARENT_OF fan-in={count} (expected <=1)")

    next_edges = [e for e in edges if e.type == EdgeType.NEXT]
    out_deg: dict[str, int] = {}
    in_deg: dict[str, int] = {}
    for e in next_edges:
        out_deg[e.source_id] = out_deg.get(e.source_id, 0) + 1
        in_deg[e.target_id] = in_deg.get(e.target_id, 0) + 1
    for nid, d in out_deg.items():
        if d > 1:
            problems.append(f"{nid} has {d} outgoing NEXT edges (violates linear chain)")
    for nid, d in in_deg.items():
        if d > 1:
            problems.append(f"{nid} has {d} incoming NEXT edges (violates linear chain)")

    next_map = {e.source_id: e.target_id for e in next_edges}
    visited: set[str] = set()
    for start in node_ids:
        if start in visited:
            continue
        path_stack = [start]
        seen_in_path = {start}
        cur = start
        while True:
            nxt = next_map.get(cur)
            if nxt is None:
                break
            if nxt in seen_in_path:
                problems.append(f"NEXT cycle found: {' -> '.join(path_stack)} -> {nxt}")
                break
            seen_in_path.add(nxt)
            path_stack.append(nxt)
            cur = nxt
        visited |= seen_in_path

    return problems
