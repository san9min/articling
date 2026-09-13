"""Articling schema — Node/Edge definitions.

A document-graph schema that follows the Artic notation from
`document-graphrag` (Document -> Structure -> Relation -> Information Unit).

The 6 node types:
    File     — one file (name = node name, location is metadata)
    Artifact — one PPT slide / one Excel tab / a whole DOC·PDF document (one page)
    Text     — any text not contained in a Table/Image
    Table    — any table not contained in an Image
    Image    — any image not contained in a Table
    Group    — a **synthetic structural node**. Represents a set of sibling
               nodes that form one conceptual unit by placement, repetition,
               or meaning even though the source has no explicit
               heading/container (e.g. several author name/affiliation/email
               blocks, several KPI cards). Unlike the other 5 types, it isn't
               content extracted verbatim from the source but a node "we
               made to understand the document" — so it has no `text` (a
               VLM-generated label is never stored as if it were source
               text). Instead `properties` carries `synthetic=True`,
               `group_type` (e.g. "authors" — optional; if absent, only the
               grouping itself is asserted, not what it's a grouping of),
               `confidence`, and `basis` (which signals drove the judgment:
               spatial/visual/structural/semantic/boundary). Membership is
               expressed with the existing `PARENT_OF` edge type rather than
               a new one (reifying an N-member group as one Group node
               instead of an N² web of relations) — see
               `relations/propose.py::propose_synthetic_groups`. Like any
               other content node, a Group is just one more node in the
               PARENT_OF tree, so `scaffold.check_invariants` applies to it
               unchanged.

The 4 edge types:
    PARENT_OF  — hierarchical parent-child (deterministic by default,
                 File->Artifact->content, exactly two levels). Two opt-in
                 exceptions: `relations/propose.py::propose_edges`'s
                 HEADING_PARENT role can reparent a heading Text -> content,
                 deepening the tree by one level (an LLM judgment, not part
                 of the default workflow — judged in the same call as
                 CAPTION_OF/REFERENCES for a Table/Image anchor, or via
                 `include_text_anchors=True`'s separate pass for a Text
                 anchor), and `propose_synthetic_groups` can reparent
                 several sibling nodes under a new Group node (see both
                 functions' docstrings). Each node has at most one
                 `PARENT_OF` parent (`scaffold.check_invariants`) — a
                 Group's members follow this rule with no exception.
    NEXT       — order between Artifacts only (currently just pptx.py's
                 slide order, deterministic). Content nodes inside one
                 Artifact are never linked by NEXT — Artifacts that aren't
                 slides (e.g. XLSX sheets) or formats with only one Artifact
                 (e.g. DOCX) never get a NEXT edge at all.
    CAPTION_OF — a Text describes a Table/Image. Two trust tiers: an explicit
                 label prefix ("표 "/"Table "/"Figure "/...) is resolved
                 deterministically (`scaffold.caption_prefix_edges`); with no
                 such prefix, `relations/propose.py::propose_edges` proposes
                 it as an LLM judgment, for human review.
    REFERENCES — the current node references another node the way a
                 citation points at its exact target, not a topical
                 relationship. Same two tiers as CAPTION_OF: citing a
                 caption's own label by name ("Figure 1", "표1") is resolved
                 deterministically (`scaffold.reference_label_edges`); a
                 pointer with no such label (quoting a specific value, or
                 saying "as shown above" with no number) is an LLM proposal
                 from `propose_edges`, for human review. pptx.py also adds a
                 third, unrelated deterministic source: a drawn
                 connector/arrow shape joining two nodes.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeType(str, Enum):
    FILE = "File"
    ARTIFACT = "Artifact"
    TEXT = "Text"
    TABLE = "Table"
    IMAGE = "Image"
    GROUP = "Group"


class EdgeType(str, Enum):
    PARENT_OF = "PARENT_OF"
    NEXT = "NEXT"
    CAPTION_OF = "CAPTION_OF"
    REFERENCES = "REFERENCES"


class Node(BaseModel):
    """One graph node. `properties` holds free-form data that differs per
    node type (Text: text/style, Table: grid/capture_path, Image:
    row/col/table_row/table_col, etc.). When exporting to Neo4j, nested
    structures (e.g. grid) are serialized as JSON strings, since Neo4j
    properties only allow scalars/arrays of scalars (see export/neo4j.py).
    """

    model_config = ConfigDict(frozen=False)

    id: str
    type: NodeType
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    type: EdgeType
    source_id: str
    target_id: str
    properties: dict[str, Any] = Field(default_factory=dict)


class ArticDocument(BaseModel):
    """One complete graph fragment produced by an extractor. When merging
    multiple documents, node ids/edges are already unique by filename, so
    simply concatenating the lists is enough."""

    source_path: str
    format: str
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def content_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.type not in (NodeType.FILE, NodeType.ARTIFACT)]

    def nodes_by_id(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def merge(self, other: "ArticDocument") -> "ArticDocument":
        return ArticDocument(
            source_path=f"{self.source_path}+{other.source_path}",
            format="mixed",
            nodes=[*self.nodes, *other.nodes],
            edges=[*self.edges, *other.edges],
        )
