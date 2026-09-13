"""Real PDF content-stream fragmentation and independent adjacent text."""
from pathlib import Path

import pymupdf
import pytest

from articling.extractors.pdf import extract
from articling.schema import ArticDocument, NodeType
from articling.scaffold import check_invariants


def make_pdf(path: Path, *, size=12, gap=2, baseline_shift=0, font="helv", rule=False,
             left="Native", right="restoration"):
    with pymupdf.open() as source:
        page = source.new_page()
        page.insert_text((72, 100), left, fontsize=size)
        # Non-contiguous paint order keeps separate blocks in MuPDF despite
        # their visual adjacency, as happens with independently emitted runs.
        page.insert_text((72, 200), "Independent paragraph", fontsize=size)
        end = 72 + pymupdf.get_text_length(left, fontsize=size)
        page.insert_text((end + gap, 100 + baseline_shift), right, fontsize=size, fontname=font)
        if rule:
            page.draw_line((end + gap / 2, 80), (end + gap / 2, 110))
        source.save(path)


@pytest.mark.parametrize("size", [8, 12, 24])
def test_native_restores_same_line_fragments_before_graph_creation(tmp_path: Path, size):
    path = tmp_path / "fragmented.pdf"
    make_pdf(path, size=size, gap=size / 6)
    with pymupdf.open(path) as source:
        assert len([b for b in source[0].get_text("dict")["blocks"] if b["type"] == 0]) == 3
    doc = extract(path, capture_dir=tmp_path / "captures")
    texts = [n for n in doc.nodes if n.type == NodeType.TEXT]
    assert [n.properties["text"] for n in texts] == ["Native restoration", "Independent paragraph"]
    merged = texts[0]
    assert merged.properties["native_merged_by"] == "same_line_continuation"
    assert len(merged.properties["pdf_block_numbers"]) == 2
    lines = merged.properties["pdf_text_lines"]
    assert [line["spans"][0]["text"] for line in lines] == ["Native", "restoration"]
    assert lines[0]["spans"][0]["origin"][1] == lines[1]["spans"][0]["origin"][1] == 100
    assert merged.properties["bbox"]["x_max"] > lines[1]["bbox"][0] / 595 * 1000
    assert ArticDocument.model_validate_json(doc.model_dump_json()) == doc
    assert check_invariants(doc.nodes, doc.edges) == []


@pytest.mark.parametrize("options", [
    {"gap": 30, "left": "Alice", "right": "Bob"},
    {"baseline_shift": 2},
    {"font": "hebo"},
    {"rule": True},
    {"left": "12", "right": "34"},
])
def test_native_preserves_separate_labels_cells_and_typography(tmp_path: Path, options):
    path = tmp_path / "separate.pdf"
    make_pdf(path, **options)
    doc = extract(path, capture_dir=tmp_path / "captures")
    texts = [n for n in doc.nodes if n.type == NodeType.TEXT]
    assert len(texts) == 3
    assert all("native_merged_by" not in n.properties for n in texts)
    assert check_invariants(doc.nodes, doc.edges) == []


def test_native_preserves_rotated_plot_labels(tmp_path: Path):
    path = tmp_path / "rotated.pdf"
    with pymupdf.open() as source:
        page = source.new_page()
        page.insert_text((100, 100), "It", fontsize=8, rotate=90)
        page.insert_text((72, 200), "Other text")
        page.insert_text((112, 100), "is", fontsize=8, rotate=90)
        source.save(path)
    doc = extract(path, capture_dir=tmp_path / "captures")
    labels = [n for n in doc.nodes if n.properties.get("text") in {"It", "is"}]
    assert len(labels) == 2
    assert all(n.properties["pdf_text_lines"][0]["dir"] == [0, -1] for n in labels)
    assert all("native_merged_by" not in n.properties for n in labels)


@pytest.mark.parametrize("drift, expected", [(0, ["restoration"]), (0.15, ["restor", "ation"])])
def test_word_fragments_preserve_spacing_and_do_not_accumulate_baseline_drift(tmp_path: Path, drift, expected):
    path = tmp_path / "word.pdf"
    with pymupdf.open() as source:
        page = source.new_page()
        x = 72
        for index, part in enumerate(("re", "stor", "ation")):
            page.insert_text((x, 100 + index * drift), part, fontsize=12)
            page.insert_text((72, 200 + index * 30), f"Marker {index}")
            x += pymupdf.get_text_length(part, fontsize=12) + 0.1
        source.save(path)
    doc = extract(path, capture_dir=tmp_path / "captures")
    words = [n.properties["text"] for n in doc.nodes if n.type == NodeType.TEXT and not n.properties["text"].startswith("Marker")]
    assert words == expected
    assert check_invariants(doc.nodes, doc.edges) == []
