"""Plumbing tests for relations/table_structure.py — verified with no real
OpenAI/Granite-Docling call.

The Granite-Docling backend itself (an actual model download+inference) was
manually verified empirically on 2026-09-04
(`docs/vlm-integration-research.md` §10) — here, instead of running the
heavy model every time in CI, fake model output is fed through
`docling_core`'s DocTags parsing path (the real library) to verify only
"our own wiring."
"""
from __future__ import annotations

from pathlib import Path

import pytest

from articling.relations.table_structure import (
    _TableReadResult,
    fill_table_grids,
    read_table_grid,
    read_table_grid_openai,
)
from articling.schema import ArticDocument, Node, NodeType

docling_core = pytest.importorskip("docling_core")  # an optional dependency specific to the granite_docling backend


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


def _png(tmp_path: Path, name: str = "crop.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def test_read_table_grid_openai_returns_none_when_not_a_table(tmp_path: Path) -> None:
    png = _png(tmp_path)
    client = _FakeClient(_TableReadResult(is_table=False, rows=[]))

    result = read_table_grid_openai(str(png), client=client)

    assert result is None
    assert len(client.responses.calls) == 1


def test_read_table_grid_openai_returns_rows_when_table(tmp_path: Path) -> None:
    png = _png(tmp_path)
    client = _FakeClient(_TableReadResult(is_table=True, rows=[["항목", "값"], ["토크", "3.4 kgf"]]))

    result = read_table_grid_openai(str(png), client=client)

    assert result == [["항목", "값"], ["토크", "3.4 kgf"]]


class _FakeProcessor:
    """A fake mimicking `AutoProcessor` — verifies only
    `read_table_grid_granite_docling`'s wiring (building the prompt ->
    calling generate -> decoding -> docling_core parsing) with no real
    model."""

    def __init__(self, doctags: str):
        self._doctags = doctags

    def apply_chat_template(self, messages, add_generation_prompt=True):
        assert messages[0]["content"][1]["text"] == "Convert table to OTSL."
        return "FAKE_PROMPT"

    def __call__(self, text, images, return_tensors):
        import torch

        assert text == "FAKE_PROMPT"
        ids = torch.zeros((1, 5), dtype=torch.long)

        class _Inputs(dict):
            input_ids = ids

        return _Inputs(input_ids=ids)

    def batch_decode(self, ids, skip_special_tokens=False):
        return [self._doctags]


class _FakeModel:
    def generate(self, **kwargs):
        import torch

        # prompt_len(5) + a few extra tokens — batch_decode actually just
        # returns doctags as-is, so the value itself doesn't matter, only
        # the length needs to be right.
        return torch.zeros((1, 8), dtype=torch.long)


def test_read_table_grid_granite_docling_parses_otsl_via_docling_core(tmp_path: Path) -> None:
    from PIL import Image

    from articling.relations.table_structure import read_table_grid_granite_docling

    img_path = tmp_path / "table.png"
    Image.new("RGB", (10, 10), "white").save(img_path)

    doctags = (
        "<otsl><loc_0><loc_0><loc_500><loc_500>"
        "<fcel>항목<fcel>값<nl><fcel>토크<fcel>3.4 kgf<nl>"
        "</otsl><|end_of_text|>"
    )

    grid = read_table_grid_granite_docling(
        str(img_path), model=_FakeModel(), processor=_FakeProcessor(doctags)
    )

    assert grid == [["항목", "값"], ["토크", "3.4 kgf"]]


def test_read_table_grid_granite_docling_returns_empty_list_when_no_table_tag(tmp_path: Path) -> None:
    """If the model produces no table at all (e.g. there's no `<otsl>` tag
    at all), returns an empty list instead of raising — it's not this
    function's job to turn that into `None` (the caller, `read_table_grid`,
    handles that) — see the module docstring's note that this function has
    no self-gating."""
    from PIL import Image

    from articling.relations.table_structure import read_table_grid_granite_docling

    img_path = tmp_path / "table.png"
    Image.new("RGB", (10, 10), "white").save(img_path)

    doctags = "<doctag><picture><loc_0><loc_0><loc_500><loc_500><other></picture>\n</doctag><|end_of_text|>"

    grid = read_table_grid_granite_docling(
        str(img_path), model=_FakeModel(), processor=_FakeProcessor(doctags)
    )

    assert grid == []


def test_read_table_grid_granite_docling_gated_by_openai_when_requested(tmp_path: Path) -> None:
    """With `verify_with_openai=True`, OpenAI gates first, and if
    `is_table=False`, Granite-Docling (the slow local model) must never be
    called at all."""
    png = _png(tmp_path)
    client = _FakeClient(_TableReadResult(is_table=False, rows=[]))

    called = {"granite": False}

    def _fake_granite(*args, **kwargs):
        called["granite"] = True
        return [["should", "not", "run"]]

    import articling.relations.table_structure as ts

    original = ts.read_table_grid_granite_docling
    ts.read_table_grid_granite_docling = _fake_granite
    try:
        result = read_table_grid(
            str(png), backend="granite_docling", verify_with_openai=True, openai_client=client
        )
    finally:
        ts.read_table_grid_granite_docling = original

    assert result is None
    assert called["granite"] is False


def test_fill_table_grids_only_updates_tables_needing_grid(tmp_path: Path) -> None:
    png = _png(tmp_path)
    already_good = Node(
        id="t1", type=NodeType.TABLE, name="Table1",
        properties={"capture_path": str(png), "grid": [["a", "b"]]},  # no grid_source = trustworthy (e.g. xlsx)
    )
    needs_fill = Node(
        id="t2", type=NodeType.TABLE, name="Table2",
        properties={"capture_path": str(png), "grid": [["absorbed text"]], "grid_source": "text_fallback"},
    )
    doc = ArticDocument(source_path="x", format="pdf", nodes=[already_good, needs_fill], edges=[])
    client = _FakeClient(_TableReadResult(is_table=True, rows=[["항목", "값"]]))

    updated = fill_table_grids(doc, backend="openai", openai_client=client)

    assert updated == ["t2"]
    assert client.responses.calls  # only called for t2
    assert len(client.responses.calls) == 1
    assert needs_fill.properties["grid"] == [["항목", "값"]]
    assert needs_fill.properties["grid_source"] == "openai"
    assert already_good.properties["grid"] == [["a", "b"]], "an already-trustworthy grid is left untouched"
