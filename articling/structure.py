"""Shared structural interpretation after native extraction and geometry.

A hierarchy scope is a document, slide, or sheet, never a rendered page.
Missing outline entries remain barriers so descendants cannot borrow an
unrelated ancestor. Native placement is retained independently of reparenting.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .schema import ArticDocument, Edge, EdgeType, Node, NodeType
from .scaffold import caption_prefix_edges, parent_edges, reference_label_edges


class NativeLocation(BaseModel):
    artifact_id: str
    page_index: int | None = None


class StructureEvent(BaseModel):
    node: Node | None = None
    level: int | None = Field(default=None, ge=0)
    excluded: bool = False


def hierarchy_edges(
    artifact: Node,
    content: list[Node],
    events: list[StructureEvent],
    *,
    source: str,
    owns_body: bool = False,
) -> list[Edge]:
    """Interpret normalized evidence with one stack across all scope pages.

    Adapters decide evidence strength: Word outline levels own body blocks;
    PDF navigation entries only prove relationships between matched headings.
    An event without a node closes/opens outline levels without inventing text.
    """
    edges = {e.target_id: e for e in parent_edges(artifact, content)}
    stack: list[tuple[int, Node | None]] = []
    for event in events:
        node, level = event.node, event.level
        if event.excluded:
            continue
        if level is not None:
            while stack and stack[-1][0] >= level:
                stack.pop()
        parent = stack[-1][1] if stack else artifact
        if node is not None and (level is not None or owns_body):
            if parent is not None and parent.id != node.id:
                edge = parent_edges(parent, [node])[0]
                if stack or level is not None:
                    edge.properties["structural_source"] = source
                edges[node.id] = edge
        if level is not None:
            stack.append((level, node))
    return list(edges.values())


def finalize_structure(document: ArticDocument) -> ArticDocument:
    """Preserve native scope and run deterministic relations for every format.

    Scope traversal ignores page boundaries. Slides/sheets retain separate
    Artifacts, preventing accidental caption/reference matching across them.
    Existing evidence is retained; repeated finalization adds no duplicate edges.
    """
    artifact_by_node = content_artifact_map(document)
    scopes: dict[str, list[Node]] = {}
    for node in document.content_nodes():
        artifact_id = artifact_by_node.get(node.id)
        if artifact_id is None:
            continue
        if "native_location" not in node.properties:
            node.properties["native_location"] = NativeLocation(
                artifact_id=artifact_id, page_index=node.properties.get("page_index"),
            ).model_dump(exclude_none=True)
        scopes.setdefault(artifact_id, []).append(node)
    existing = {(e.type, e.source_id, e.target_id) for e in document.edges}
    for content in scopes.values():
        captions = caption_prefix_edges(content)
        for edge in [*captions, *reference_label_edges(content, captions)]:
            key = (edge.type, edge.source_id, edge.target_id)
            if key not in existing:
                document.edges.append(edge)
                existing.add(key)
    return document


def content_artifact_map(document: ArticDocument) -> dict[str, str]:
    artifact_ids = {node.id for node in document.nodes if node.type == NodeType.ARTIFACT}
    parent_by_child = {
        edge.target_id: edge.source_id for edge in document.edges if edge.type == EdgeType.PARENT_OF
    }
    mapping: dict[str, str] = {}
    for node in document.content_nodes():
        location = node.properties.get("native_location")
        if location is not None:
            native = NativeLocation.model_validate(location)
            if native.artifact_id in artifact_ids:
                mapping[node.id] = native.artifact_id
                continue
        current = node.id
        seen = {current}
        while current in parent_by_child:
            current = parent_by_child[current]
            if current in artifact_ids:
                mapping[node.id] = current
                break
            if current in seen:
                break
            seen.add(current)
    return mapping

