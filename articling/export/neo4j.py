"""ArticDocument -> Neo4j mapping.

- Node.type -> node label (the `NodeType` value as-is: File/Artifact/Text/Table/Image/Group)
- Edge.type -> relationship type (the `EdgeType` value as-is: PARENT_OF/NEXT/CAPTION_OF/REFERENCES)
- Node.id -> the `id` property (target of the uniqueness constraint). Filename-based,
  so it stays unique even when merging multiple documents (see `ArticDocument.merge`
  in schema.py).
- Nested values (list/dict — e.g. Table's `grid`, `capture_size`) inside
  Node.properties/Edge.properties are serialized as JSON strings. Neo4j
  properties only allow scalars or arrays of scalars, not arbitrary nested
  structures. Read them back with `json.loads`.

Two usage modes are supported:
1. `to_cypher_script(document)` — builds a `.cypher` script string that runs
   with no connection needed. Paste it straight into `cypher-shell` or Neo4j
   Browser. This is the default so it can be verified in CI/offline
   environments too.
2. `push_to_neo4j(document, uri, auth)` — pushes it directly with the
   `neo4j` Python driver (an optional dependency — the rest of the package
   works fine without the `neo4j` package installed as long as you don't
   call this function).
"""
from __future__ import annotations

import json
from typing import Any

from ..schema import ArticDocument

_UNIQUE_CONSTRAINT_LABELS = ("File", "Artifact", "Text", "Table", "Image")


def _serialize_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Serialize nested structures (dict/list-of-non-scalar) that Neo4j can't
    take as JSON strings.

    Scalars (str/int/float/bool/None) and lists made up entirely of scalars
    are left as-is — those are array properties Neo4j supports natively.
    """
    out: dict[str, Any] = {}
    for k, v in props.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in v):
            out[k] = v
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out


def _cypher_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_cypher_literal(v) for v in value) + "]"
    # string — escape backslash/quote/newline
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"


def _props_literal(props: dict[str, Any]) -> str:
    if not props:
        return ""
    body = ", ".join(f"{k}: {_cypher_literal(v)}" for k, v in props.items())
    return f" {{{body}}}"


def to_cypher_script(document: ArticDocument, *, with_constraints: bool = True) -> str:
    """Build a Cypher script that runs with no connection needed (MERGE-based
    — safe to run more than once)."""
    lines: list[str] = [f"// articling export — source: {document.source_path} ({document.format})"]

    if with_constraints:
        lines.append("// uniqueness constraints — keep nodes from being duplicated when merging documents")
        for label in _UNIQUE_CONSTRAINT_LABELS:
            lines.append(
                f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE;"
            )
        lines.append("")

    lines.append("// nodes")
    for node in document.nodes:
        props = _serialize_properties({"name": node.name, **node.properties})
        body = ", ".join(f"n.{k} = {_cypher_literal(v)}" for k, v in props.items())
        set_clause = f" SET {body}" if body else ""
        lines.append(f"MERGE (n:{node.type.value} {{id: {_cypher_literal(node.id)}}}){set_clause};")

    lines.append("")
    lines.append("// edges")
    for edge in document.edges:
        props = _serialize_properties(edge.properties)
        rel_props = _props_literal(props)
        lines.append(
            f"MATCH (a {{id: {_cypher_literal(edge.source_id)}}}), (b {{id: {_cypher_literal(edge.target_id)}}}) "
            f"MERGE (a)-[:{edge.type.value}{rel_props}]->(b);"
        )

    return "\n".join(lines) + "\n"


def push_to_neo4j(document: ArticDocument, uri: str, auth: tuple[str, str], *, database: str | None = None) -> None:
    """Push the document graph directly with the `neo4j` Python driver
    (optional dependency).

    Nodes are MERGEd on `id`, so running this more than once doesn't
    duplicate anything — though for bulk data, building a script with
    `to_cypher_script` and batch-running it via `cypher-shell` is faster
    (no per-call driver round-trip overhead).
    """
    from neo4j import GraphDatabase  # lazy import — neo4j isn't needed unless this function is called

    driver = GraphDatabase.driver(uri, auth=auth)
    try:
        with driver.session(database=database) as session:
            for label in _UNIQUE_CONSTRAINT_LABELS:
                session.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")

            for node in document.nodes:
                props = _serialize_properties({"name": node.name, **node.properties})
                session.run(
                    f"MERGE (n:{node.type.value} {{id: $id}}) SET n += $props",
                    id=node.id,
                    props=props,
                )

            for edge in document.edges:
                props = _serialize_properties(edge.properties)
                session.run(
                    f"MATCH (a {{id: $source_id}}), (b {{id: $target_id}}) "
                    f"MERGE (a)-[r:{edge.type.value}]->(b) SET r += $props",
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    props=props,
                )
    finally:
        driver.close()
