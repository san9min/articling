from __future__ import annotations

from articling.export.neo4j import to_cypher_script
from articling.schema import ArticDocument, Edge, EdgeType, Node, NodeType


def _sample_document() -> ArticDocument:
    file_n = Node(id="file:a.xlsx", type=NodeType.FILE, name="a.xlsx")
    artifact = Node(id="artifact:a.xlsx:sheet0", type=NodeType.ARTIFACT, name="Sheet1")
    table = Node(
        id="content:a.xlsx:s0:tbl1",
        type=NodeType.TABLE,
        name="표 1",
        properties={"grid": [["항목", "값"], ["토크", "3.4 kgf"]]},
    )
    return ArticDocument(
        source_path="a.xlsx",
        format="xlsx",
        nodes=[file_n, artifact, table],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id=file_n.id, target_id=artifact.id),
            Edge(type=EdgeType.PARENT_OF, source_id=artifact.id, target_id=table.id),
        ],
    )


def test_to_cypher_script_contains_merge_statements() -> None:
    doc = _sample_document()
    script = to_cypher_script(doc)

    assert "CREATE CONSTRAINT" in script
    assert "MERGE (n:File {id: 'file:a.xlsx'})" in script
    assert "MERGE (n:Table {id: 'content:a.xlsx:s0:tbl1'})" in script
    assert "[:PARENT_OF]" in script
    # a nested structure (grid) must be serialized as a JSON string (Neo4j can't hold nested properties)
    assert '\\"항목\\"' in script or '"항목"' in script


def test_content_nodes_excludes_container_nodes() -> None:
    doc = _sample_document()
    content = doc.content_nodes()
    assert all(n.type not in (n.type.FILE, n.type.ARTIFACT) for n in content)
    assert len(content) == 1


def test_merge_concatenates_nodes_and_edges() -> None:
    a = _sample_document()
    b = _sample_document()
    b.source_path = "b.xlsx"
    merged = a.merge(b)
    assert len(merged.nodes) == len(a.nodes) + len(b.nodes)
    assert len(merged.edges) == len(a.edges) + len(b.edges)
