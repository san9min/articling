"""Deterministic scaffold — File/Artifact nodes and PARENT_OF/NEXT edges.

Uses no VLM or LLM at all. An Artifact (slide/tab/whole document) is
determined by the file structure itself, so this stage is a deterministic
computation that cannot fail.
"""
from __future__ import annotations

import re
from pathlib import Path

from .schema import Edge, EdgeType, Node, NodeType

CAPTURE_SUBDIR = "captures"

# A "표 "/"그림 "/"Table "/"Figure "/... prefix immediately before a
# Table/Image is a cross-format signal, not a per-extractor one — docx.py,
# pdf.py and pptx.py all treat the same prefix set as a caption marker.
CAPTION_PREFIXES = ("표 ", "그림 ", "Table ", "Figure ", "<표", "<그림", "[표", "[그림")

# Same label vocabulary as CAPTION_PREFIXES, but as a "label + number"
# pattern searched anywhere in a text, not just a leading prefix — this is
# what lets `reference_label_edges` recognize a citation like "Figure 1" or
# "표1" wherever it's mentioned, not only at the start of a caption. `\s*`
# (not `\s+`) also matches the no-space Korean form ("표1").
_REFERENCE_LABEL_RE = re.compile(r"(표|그림|Table|Figure)\.?\s*(\d+)", re.IGNORECASE)


def _reference_label_key(word: str, number: str) -> tuple[str, str]:
    """Normalizes "표"/"Table" and "그림"/"Figure" (either language, either
    case) to the same key, so a Korean caption ("표 1") and an English
    in-body citation ("Table 1") of the same numbered item still match."""
    canonical = "table" if word.lower() in ("표", "table") else "figure"
    return (canonical, number)


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
    a Table or Image. Shared by docx.py/pdf.py/pptx.py/xlsx.py, since all
    four define "immediately before/after" the same way (adjacency within
    one Artifact's content list — for xlsx.py, one sheet's content sorted by
    row/col); an extractor may end up attaching the same heuristic edge to
    both a preceding and a following candidate on purpose, leaving
    disambiguation to review or to
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


def reference_label_edges(content_nodes: list[Node], caption_edges: list[Edge]) -> list[Edge]:
    """REFERENCES heuristic (same trust tier as `caption_prefix_edges` —
    safe to add straight to `doc.edges`, not an LLM proposal): when some
    other Text explicitly cites a caption's own label ("Figure 1", "표1"),
    that citing Text gets a REFERENCES edge to the same anchor the caption
    already resolved to. The same mechanism a paper's inline "[1]" has to
    its bibliography entry — just keyed by caption label+number instead of
    a citation index.

    `caption_edges`: the CAPTION_OF edges already resolved for this same
    `content_nodes` (normally `caption_prefix_edges`'s own output) — the
    source of truth for "which label points at which anchor." A bare
    "Figure 1" mention with no matching caption anywhere in `content_nodes`
    creates no edge; this only connects citations to labels that are
    already grounded, it never invents a label -> anchor mapping of its
    own. Citing text elsewhere in the graph that doesn't share a
    `content_nodes` call with its caption (e.g. a different pptx slide)
    isn't matched — same locality assumption `caption_prefix_edges` makes.
    """
    nodes_by_id = {n.id: n for n in content_nodes}
    label_to_anchor: dict[tuple[str, str], str] = {}
    caption_source_ids: set[str] = set()
    for e in caption_edges:
        if e.type != EdgeType.CAPTION_OF:
            continue
        caption_node = nodes_by_id.get(e.source_id)
        if caption_node is None:
            continue
        match = _REFERENCE_LABEL_RE.search(caption_node.properties.get("text", ""))
        if match is None:
            continue
        label_to_anchor[_reference_label_key(*match.groups())] = e.target_id
        caption_source_ids.add(e.source_id)

    if not label_to_anchor:
        return []

    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for n in content_nodes:
        # A caption's own text isn't treated as a citation of its own (or
        # any other) label — it's already linked via CAPTION_OF.
        if n.type != NodeType.TEXT or n.id in caption_source_ids:
            continue
        for word, number in _REFERENCE_LABEL_RE.findall(n.properties.get("text", "")):
            anchor_id = label_to_anchor.get(_reference_label_key(word, number))
            if anchor_id is None or (n.id, anchor_id) in seen:
                continue
            seen.add((n.id, anchor_id))
            edges.append(Edge(type=EdgeType.REFERENCES, source_id=n.id, target_id=anchor_id))
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

    # Check every edge even when fan-in is already invalid. An iterative
    # DFS avoids recursion limits on deeply nested documents.
    children: dict[str, list[str]] = {}
    for e in parent_of:
        children.setdefault(e.source_id, []).append(e.target_id)
    finished: set[str] = set()
    active: set[str] = set()
    for start in node_ids:
        stack = [(start, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                active.discard(current)
                finished.add(current)
            elif current in active:
                problems.append(f"PARENT_OF cycle found at {current}")
            elif current not in finished:
                active.add(current)
                stack.append((current, True))
                stack.extend((child, False) for child in children.get(current, []))

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
