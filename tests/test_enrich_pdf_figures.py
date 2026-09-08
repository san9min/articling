"""Integration tests for `extractors/pdf_figures.enrich_pdf_figures` —
verifies the full detect->crop->Image-node-creation path against a
synthetic PDF that has a vector figure (hundreds of small filled
rectangles, heatmap-style) actually drawn with pymupdf. No VLM/API call at
all (the module itself is model-free — see the module docstring).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fixtures.make_fixtures import build_pdf_with_vector_figure  # noqa: E402

from articling.extractors.pdf import extract  # noqa: E402
from articling.extractors.pdf_figures import (  # noqa: E402
    detect_vector_figure_regions,
    enrich_pdf_figures,
)
from articling.scaffold import check_invariants  # noqa: E402
from articling.schema import EdgeType, NodeType  # noqa: E402

import pymupdf  # noqa: E402


def test_detect_vector_figure_regions_ignores_sparse_decoration(tmp_path: Path) -> None:
    """A low-density decorative rectangle (4 items) must not become a
    candidate, and only the dense grid (120 items) should — avoiding false
    positives on table borders/dividers is this function's core basis for
    trust (see the empirical numbers in the module docstring)."""
    path = build_pdf_with_vector_figure(tmp_path / "fig.pdf")
    pdf = pymupdf.open(str(path))
    try:
        candidates = detect_vector_figure_regions(pdf[0])
    finally:
        pdf.close()

    assert len(candidates) == 1, "only the dense grid should be a candidate (the decorative border must be excluded)"
    assert candidates[0].item_count == 120


def test_enrich_pdf_figures_creates_image_node_and_absorbs_content(tmp_path: Path) -> None:
    path = build_pdf_with_vector_figure(tmp_path / "fig.pdf")
    doc = extract(path, capture_dir=tmp_path / "captures")

    n_content_before = len(doc.content_nodes())
    created = enrich_pdf_figures(doc, capture_dir=tmp_path / "captures")

    assert len(created) == 1
    image_nodes = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(image_nodes) == 1
    image = image_nodes[0]
    assert image.properties["detection_mode"] == "vector_figure"
    assert image.properties["vector_item_count"] == 120
    assert Path(image.properties["image_path"]).exists()

    # the paragraph outside the figure ("Report heading") and the caption
    # ("Figure 1. …") are left untouched — the caption in particular must
    # never be absorbed even if its bbox overlaps (it has to remain a
    # target propose_edges can later attach a CAPTION_OF to).
    remaining_texts = {n.properties.get("text") for n in doc.nodes if n.type == NodeType.TEXT}
    assert "Report heading (outside the figure)" in remaining_texts
    assert "Figure 1. Synthetic attention heatmap" in remaining_texts

    # the individual rectangles making up the grid were never content nodes
    # (pure vector graphics, not a get_text block), so content_nodes should
    # only grow by +1 (the new Image)
    assert len(doc.content_nodes()) == n_content_before + 1

    assert any(e.type == EdgeType.PARENT_OF and e.target_id == image.id for e in doc.edges)
    assert check_invariants(doc.nodes, doc.edges) == []


def test_enrich_pdf_figures_skips_regions_already_captured_as_raster_image(tmp_path: Path) -> None:
    """If a vector figure candidate overlaps heavily with an area already
    caught as a raster Image node (e.g. an embedded image extract() already
    caught), it must not be created again as a duplicate."""
    from articling.schema import ArticDocument, Edge, Node

    path = build_pdf_with_vector_figure(tmp_path / "fig.pdf")
    pdf = pymupdf.open(str(path))
    w, h = pdf[0].rect.width, pdf[0].rect.height
    candidates = detect_vector_figure_regions(pdf[0])
    pdf.close()
    assert len(candidates) == 1
    from articling.extractors.pdf import _normalized_bbox

    cand_bbox = _normalized_bbox(candidates[0].bbox, w, h)

    artifact = Node(id="artifact:fig.pdf:doc", type=NodeType.ARTIFACT, name="fig", properties={})
    existing_image = Node(
        id="content:fig.pdf:img1", type=NodeType.IMAGE, name="already-existing image",
        properties={"page_index": 0, "bbox": cand_bbox},
    )
    doc = ArticDocument(
        source_path=str(path.resolve()), format="pdf",
        nodes=[artifact, existing_image],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id=artifact.id, target_id=existing_image.id)],
    )

    created = enrich_pdf_figures(doc, capture_dir=tmp_path / "captures")

    assert created == [], "must not create a new one when it overlaps heavily with an area already captured as raster"
    assert [n.id for n in doc.nodes if n.type == NodeType.IMAGE] == ["content:fig.pdf:img1"]


def test_extract_with_enrich_figures_true_calls_enrich_pdf_figures(tmp_path: Path) -> None:
    """Calling `extractors.pdf.extract()` once with `enrich_figures=True`
    must produce the same result as calling `enrich_pdf_figures` directly."""
    path = build_pdf_with_vector_figure(tmp_path / "fig.pdf")

    doc = extract(path, capture_dir=tmp_path / "captures", enrich_figures=True)

    image_nodes = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(image_nodes) == 1
    assert image_nodes[0].properties["detection_mode"] == "vector_figure"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_extract_default_does_not_enrich_figures(tmp_path: Path) -> None:
    """The default (`enrich_figures=False`) keeps the old behavior exactly
    — no new Image node should be created even if a vector figure is
    present (backward compatibility)."""
    path = build_pdf_with_vector_figure(tmp_path / "fig.pdf")

    doc = extract(path, capture_dir=tmp_path / "captures")

    assert NodeType.IMAGE not in {n.type for n in doc.nodes}
