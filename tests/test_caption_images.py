"""Plumbing tests for caption_content_nodes — verified with a fake client, no real OpenAI call.

Only verifies "does it pick out just the nodes with a pixel path and fill in
properties in place" and "does it skip a node with no pixel file" (comparing
the quality/cost of model choice itself is out of scope).
"""
from __future__ import annotations

from pathlib import Path

from articling.relations.caption_images import ImageDescription, caption_content_nodes
from articling.schema import ArticDocument, Node, NodeType


class _FakeResponse:
    def __init__(self, parsed):
        self.output_parsed = parsed


class _FakeResponses:
    def __init__(self, description: str):
        self._description = description

    def parse(self, **kwargs):
        return _FakeResponse(ImageDescription(description=self._description, content_type="photo", confidence=0.9))


class _FakeClient:
    def __init__(self, description: str = "test description"):
        self.responses = _FakeResponses(description)


def test_captions_only_nodes_with_pixel_paths(tmp_path: Path) -> None:
    png = tmp_path / "img.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")  # doesn't need to be a real PNG — the fake client never looks at the content

    with_image = Node(id="img1", type=NodeType.IMAGE, name="photo", properties={"image_path": str(png)})
    without_image = Node(id="img2", type=NodeType.IMAGE, name="vector shape", properties={"nearby_text": "arrow"})
    table_with_capture = Node(id="tbl1", type=NodeType.TABLE, name="table", properties={"capture_path": str(png)})
    text_node = Node(id="txt1", type=NodeType.TEXT, name="body text", properties={"text": "..."})

    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[with_image, without_image, table_with_capture, text_node],
    )

    updated = caption_content_nodes(doc, client=_FakeClient("photo description"))

    assert set(updated) == {"img1", "tbl1"}
    assert with_image.properties["vlm_description"] == "photo description"
    assert with_image.properties["vlm_content_type"] == "photo"
    assert table_with_capture.properties["vlm_description"] == "photo description"
    assert "vlm_description" not in without_image.properties, "a node with no pixel path must be skipped"
    assert "vlm_description" not in text_node.properties


def test_skips_missing_pixel_file(tmp_path: Path) -> None:
    missing = Node(
        id="img1", type=NodeType.IMAGE, name="photo",
        properties={"image_path": str(tmp_path / "does_not_exist.png")},
    )
    doc = ArticDocument(source_path="x", format="docx", nodes=[missing])

    updated = caption_content_nodes(doc, client=_FakeClient())

    assert updated == []
    assert "vlm_description" not in missing.properties
