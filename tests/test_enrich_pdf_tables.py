"""Integration tests for `relations/table_structure.enrich_pdf_tables` —
verifies the full detect->crop->(fake) model reading->Table-node-creation
path against a synthetic PDF that has a vector table actually drawn with
pymupdf. No real OpenAI/Granite-Docling call.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fixtures.make_fixtures import build_pdf_with_vector_table  # noqa: E402

from articling.extractors.pdf import extract  # noqa: E402
from articling.relations.table_structure import (  # noqa: E402
    _TableReadResult,
    enrich_pdf_tables,
)
from articling.scaffold import check_invariants  # noqa: E402
from articling.schema import EdgeType, NodeType  # noqa: E402


class _FakeResponse:
    def __init__(self, parsed):
        self.output_parsed = parsed


class _RecordingResponses:
    def __init__(self, result):
        self._result = result
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._result)


class _FakeClient:
    def __init__(self, result):
        self.responses = _RecordingResponses(result)


def test_enrich_pdf_tables_creates_table_node_and_absorbs_content(tmp_path: Path) -> None:
    path = build_pdf_with_vector_table(tmp_path / "table.pdf")
    doc = extract(path, capture_dir=tmp_path / "captures")

    assert NodeType.TABLE not in {n.type for n in doc.nodes}, "extract() alone still doesn't create a Table"
    n_content_before = len(doc.content_nodes())

    client = _FakeClient(_TableReadResult(is_table=True, rows=[["Item", "Value"], ["Torque", "3.4 kgf"]]))
    created = enrich_pdf_tables(doc, backend="openai", openai_client=client, capture_dir=tmp_path / "captures")

    assert len(created) == 1
    table_nodes = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert len(table_nodes) == 1
    table = table_nodes[0]
    assert table.properties["grid"] == [["Item", "Value"], ["Torque", "3.4 kgf"]]
    assert table.properties["grid_source"] == "openai"
    assert Path(table.properties["capture_path"]).exists()

    # the paragraph outside the table ("Report heading") is untouched
    headings = [n for n in doc.nodes if n.type == NodeType.TEXT and "heading" in n.properties.get("text", "")]
    assert len(headings) == 1

    # the Text nodes inside the table were absorbed and disappeared -> the content node count drops
    assert len(doc.content_nodes()) < n_content_before

    # an Artifact -> Table PARENT_OF exists, no dangling edges
    assert any(e.type == EdgeType.PARENT_OF and e.target_id == table.id for e in doc.edges)
    assert check_invariants(doc.nodes, doc.edges) == []


def test_extract_with_enrich_tables_true_calls_enrich_pdf_tables(tmp_path: Path) -> None:
    """Calling `extractors.pdf.extract()` once with `enrich_tables=True`
    must produce the same result as calling `enrich_pdf_tables` directly —
    `articling.extract()` (the top-level dispatcher) still doesn't take
    this option (a format-specific option like capture_dir, so
    extractors.pdf.extract has to be called directly)."""
    path = build_pdf_with_vector_table(tmp_path / "table.pdf")
    client = _FakeClient(_TableReadResult(is_table=True, rows=[["Item", "Value"], ["Torque", "3.4 kgf"]]))

    doc = extract(
        path,
        capture_dir=tmp_path / "captures",
        enrich_tables=True,
        openai_client=client,
    )

    table_nodes = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert len(table_nodes) == 1
    assert table_nodes[0].properties["grid"] == [["Item", "Value"], ["Torque", "3.4 kgf"]]
    assert check_invariants(doc.nodes, doc.edges) == []


def test_extract_default_does_not_enrich_tables(tmp_path: Path) -> None:
    """The default (`enrich_tables=False`) keeps the old behavior exactly —
    there should be no API call and no Table node (backward compatibility/
    offline guarantee)."""
    path = build_pdf_with_vector_table(tmp_path / "table.pdf")

    doc = extract(path, capture_dir=tmp_path / "captures")

    assert NodeType.TABLE not in {n.type for n in doc.nodes}


def test_enrich_pdf_tables_skips_when_model_says_not_a_table(tmp_path: Path) -> None:
    path = build_pdf_with_vector_table(tmp_path / "table.pdf")
    doc = extract(path, capture_dir=tmp_path / "captures")
    n_nodes_before = len(doc.nodes)

    client = _FakeClient(_TableReadResult(is_table=False, rows=[]))
    created = enrich_pdf_tables(doc, backend="openai", openai_client=client, capture_dir=tmp_path / "captures")

    assert created == []
    assert NodeType.TABLE not in {n.type for n in doc.nodes}
    assert len(doc.nodes) == n_nodes_before, "when judged not a table, the existing nodes must be left as-is"
