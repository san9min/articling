"""Plumbing tests for propose_edges — verified with a fake client, no real OpenAI call.

Covers the branch where an image is sent along when the anchor node has a
pixel path, and falls back to text-only when it doesn't — actual judgment
quality can't be verified with a fake client, so that's out of scope.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fixtures.make_fixtures import (  # noqa: E402
    build_pdf_with_figure_caption, build_pptx, build_xlsx_with_table_caption,
)

from articling.extractors import pdf as pdf_extractor  # noqa: E402
from articling.extractors import pptx as pptx_extractor  # noqa: E402
from articling.extractors import xlsx as xlsx_extractor  # noqa: E402
from articling.relations.propose import (  # noqa: E402
    _CaptionDisambiguation, _EdgeProposal, _EdgeProposalResult, _FragmentMergeBatchResult,
    _FragmentMergeDecision, _HeadingParentBatchResult, _HeadingParentChoice,
    _SemanticRegionBatchResult, _SemanticRegionDecision, _SemanticTextGroup,
    _SiblingRegionBatchResult, _SiblingRegionDecision, _SyntheticGroupCandidate,
    _build_document_context_windows, _find_same_line_text_clusters,
    _find_semantic_text_regions, _propose_for_anchor,
    apply_vlm_enrichment, build_context_windows, merge_fragmented_text,
    merge_semantic_text_groups, nest_numbered_headings,
    promote_heading_parents, propose_edges, propose_synthetic_groups, resolve_ambiguous_captions,
)
from articling.schema import ArticDocument, Edge, EdgeType, Node, NodeType  # noqa: E402
from articling.scaffold import check_invariants  # noqa: E402


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


def _text(id_: str, text: str) -> Node:
    return Node(id=id_, type=NodeType.TEXT, name=text[:20], properties={"text": text})


def _bbox(x_min: float, y_min: float, x_max: float, y_max: float) -> dict:
    return {"x_min": x_min, "y_min": y_min, "x_max": x_max, "y_max": y_max}


def _pdf_text(id_: str, text: str, bbox: dict, page_index: int = 0) -> Node:
    """A Text node meeting the condition `_find_same_line_text_clusters`
    targets (`bbox`+`page_index` properties) — mimics the shape
    `extractors/pdf.py` actually produces."""
    return Node(
        id=id_, type=NodeType.TEXT, name=text[:20],
        properties={"text": text, "bbox": bbox, "page_index": page_index},
    )


def test_build_context_windows_pairs_anchor_with_nearby_text() -> None:
    nodes = [
        _text("t1", "표 1. 측정 결과"),
        Node(id="tbl1", type=NodeType.TABLE, name="표", properties={"grid": [["a", "b"]]}),
        _text("t2", "위 표 참고"),
    ]
    windows = build_context_windows(nodes, window=3)
    assert len(windows) == 1
    anchor, candidates = windows[0]
    assert anchor.id == "tbl1"
    assert {c.id for c in candidates} == {"t1", "t2"}


def test_build_context_windows_stops_at_neighboring_anchor() -> None:
    """In a document where tables/images sit closely packed (`Table A
    Caption A Table B Caption B Table C Caption C`), a generous window (3)
    could let a caption beyond another table leak into the context
    candidates — Table A and Caption C are 3 slots apart, so the old code
    would let Caption C into Table A's window too. Encountering another
    Table/Image must stop the search in that direction right there, so a
    caption right past one boundary might sneak in, but one past two or
    more boundaries must be excluded from the candidates — otherwise
    propose_edges risks wrongly linking a caption to the wrong table via
    CAPTION_OF."""
    tbl_a = Node(id="tbl_a", type=NodeType.TABLE, name="표A", properties={"grid": [["a"]]})
    cap_a = _text("cap_a", "표 A. 측정 결과")
    tbl_b = Node(id="tbl_b", type=NodeType.TABLE, name="표B", properties={"grid": [["b"]]})
    cap_b = _text("cap_b", "표 B. 측정 결과")
    tbl_c = Node(id="tbl_c", type=NodeType.TABLE, name="표C", properties={"grid": [["c"]]})
    cap_c = _text("cap_c", "표 C. 측정 결과")
    nodes = [tbl_a, cap_a, tbl_b, cap_b, tbl_c, cap_c]

    windows = build_context_windows(nodes, window=3)

    by_anchor = {anchor.id: {c.id for c in candidates} for anchor, candidates in windows}
    assert "cap_c" not in by_anchor["tbl_a"], "Table A must not receive Caption C, past Table B, as a candidate"
    assert "cap_a" not in by_anchor["tbl_c"], "Table C must not receive Caption A, past Table B, as a candidate"


def test_propose_for_anchor_attaches_image_when_pixel_path_exists(tmp_path: Path) -> None:
    png = tmp_path / "capture.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    anchor = Node(id="tbl1", type=NodeType.TABLE, name="표", properties={"grid": [["a"]], "capture_path": str(png)})
    candidate = _text("t1", "표 1. 측정 결과")

    fake_result = _EdgeProposalResult(proposals=[_EdgeProposal(context_index=0, edge_type="CAPTION_OF", rationale="match")])
    client = _FakeClient(fake_result)

    edges = _propose_for_anchor(client, "gpt-5.6-terra", anchor, [candidate])

    assert len(client.responses.calls) == 1
    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, list), "with a pixel path, the content must be multimodal (a list)"
    types = [c["type"] for c in content]
    assert "input_image" in types

    assert len(edges) == 1
    assert edges[0].source_id == "t1"
    assert edges[0].target_id == "tbl1"


def test_propose_for_anchor_falls_back_to_text_when_no_pixel_path() -> None:
    anchor = Node(id="img1", type=NodeType.IMAGE, name="이미지 1", properties={})  # no image_path
    candidate = _text("t1", "그림 1. 시료 사진")

    fake_result = _EdgeProposalResult(proposals=[_EdgeProposal(context_index=0, edge_type="NONE", rationale="unrelated")])
    client = _FakeClient(fake_result)

    _propose_for_anchor(client, "gpt-5.6-terra", anchor, [candidate])

    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, str), "with no pixel path, it must be text-only (a string) as before"


class _SequencedResponses:
    """Returns pre-prepared results in order, one per call (for testing
    `resolve_ambiguous_captions`, which needs a different verdict per group)
    — raises if an element of `outputs` is an `Exception`."""

    def __init__(self, outputs: list):
        self._outputs = outputs
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        out = self._outputs[len(self.calls) - 1]
        if isinstance(out, Exception):
            raise out
        return _FakeResponse(out)


class _SequencedClient:
    def __init__(self, outputs: list):
        self.responses = _SequencedResponses(outputs)


class _ShapeAwareResponses:
    """Returns the result matching the schema requested via `text_format` —
    for tests verifying that anchors with a pixel (individual calls,
    `_HeadingParentChoice`) and anchors without one (batched calls,
    `_HeadingParentBatchResult`) use the same client concurrently (since
    call order isn't guaranteed with concurrent thread-pool execution, this
    branches on the request schema regardless of order)."""

    def __init__(self, results_by_type: dict):
        self._results = results_by_type
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._results[kwargs["text_format"]])


class _ShapeAwareClient:
    def __init__(self, results_by_type: dict):
        self.responses = _ShapeAwareResponses(results_by_type)


def _image_node(id_: str, name: str, image_path: str) -> Node:
    return Node(id=id_, type=NodeType.IMAGE, name=name, properties={"image_path": image_path})


def test_propose_edges_skips_anchor_on_api_failure() -> None:
    """Even if the API call for one anchor fails (a rate limit, etc.), only
    that anchor should be skipped and the proposals for the rest of the
    anchors should still be returned — the whole run must not stop
    (AGENTS.md "Failure handling in batch / LLM-backed code")."""
    t1 = _text("t1", "앞 문단")
    tbl1 = Node(id="tbl1", type=NodeType.TABLE, name="표", properties={"grid": [["a"]]})
    t2 = _text("t2", "가운데 문단")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    t3 = _text("t3", "그림 1. 뒤 문단")
    doc = ArticDocument(source_path="x", format="docx", nodes=[t1, tbl1, t2, img1, t3], edges=[])

    # the tbl1 anchor call fails, the img1 anchor call succeeds — consumed in order.
    img1_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=1, edge_type="CAPTION_OF", rationale="일치")]
    )
    client = _SequencedClient([RuntimeError("simulated API error"), img1_result])

    proposals = propose_edges(doc, client=client, window=1)

    assert len(client.responses.calls) == 2, "the next anchor must still be processed after a failed one"
    assert len(proposals) == 1, "there must be no proposal for the failed anchor, only the successful one's"
    assert proposals[0].source_id == "t3"
    assert proposals[0].target_id == "img1"


def test_promote_heading_parents_reparents_when_model_picks_heading() -> None:
    """If the model picks a heading among the candidates, the PARENT_OF
    that was Artifact->Image must be reparented to heading Text->Image
    (deepening the tree)."""
    heading = _text("h1", "3. 측정 결과")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    other = _text("t2", "본문 설명")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[heading, img1, other],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")],
    )
    fake_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=0, parent_index=0, rationale="측정 결과 섹션 제목")]
    )
    client = _FakeClient(fake_result)

    promoted = promote_heading_parents(doc, client=client, window=3)

    assert promoted == ["img1"]
    parent_of = [e for e in doc.edges if e.type == EdgeType.PARENT_OF]
    assert len(parent_of) == 1, "the old structural edge must be removed, leaving only one new edge"
    assert parent_of[0].source_id == "h1" and parent_of[0].target_id == "img1"
    assert parent_of[0].properties["reparented_from"] == "art1"
    assert check_invariants(doc.nodes, doc.edges) == [], "the fan-in<=1 invariant must still hold after reparenting"


def test_promote_heading_parents_leaves_structure_when_model_finds_no_heading() -> None:
    heading = _text("h1", "그냥 아무 문단")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[heading, img1],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")],
    )
    fake_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=0, parent_index=None, rationale="헤딩 아님")]
    )
    client = _FakeClient(fake_result)

    promoted = promote_heading_parents(doc, client=client, window=3)

    assert promoted == []
    assert doc.edges == [Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")]


def test_promote_heading_parents_skips_anchor_on_api_failure() -> None:
    """Even if the API call fails (a rate limit, etc.), that anchor's
    original structure (Artifact as parent) must be left as-is, following
    the same partial-failure principle as the other batch code."""
    heading = _text("h1", "3. 측정 결과")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    original_edge = Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")
    doc = ArticDocument(source_path="x", format="docx", nodes=[heading, img1], edges=[original_edge])
    client = _SequencedClient([RuntimeError("simulated API error")])

    promoted = promote_heading_parents(doc, client=client, window=3)

    assert promoted == []
    assert doc.edges == [original_edge]


def test_promote_heading_parents_default_ignores_text_anchors() -> None:
    """With `include_text_anchors` defaulting to False, Text never becomes a
    reparent target (anchor), and if the document has no Table/Image at
    all, there must be no API call whatsoever — backward compatibility."""
    heading = _text("h1", "1. 개요")
    body = _text("body", "이 섹션은 배경을 설명한다")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[heading, body],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="body")],
    )
    fake_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=0, parent_index=0, rationale="개요 섹션 소속")]
    )
    client = _FakeClient(fake_result)

    promoted = promote_heading_parents(doc, client=client, window=3)

    assert promoted == []
    assert len(client.responses.calls) == 0, "with no Table/Image, Text must not become an anchor"
    assert doc.edges == [Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="body")]


def test_promote_heading_parents_reparents_text_under_text_when_included() -> None:
    """With `include_text_anchors=True`, a body paragraph Text can also be
    reparented under a heading Text — the "Text-Text PARENT_OF" case a user
    requested."""
    heading = _text("h1", "1. 개요")
    body = _text("body", "이 섹션은 배경을 설명한다")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[heading, body],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="body")],
    )
    # anchor_index 0=heading (candidates=[body]), 1=body (candidates=[heading])
    # — an answer where heading picks body as its parent is silently
    # ignored since h1 has no existing PARENT_OF (the original code does the
    # same), so filling in only body's answer is enough.
    fake_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=1, parent_index=0, rationale="개요 섹션 소속")]
    )
    client = _FakeClient(fake_result)

    promoted = promote_heading_parents(doc, client=client, window=3, include_text_anchors=True)

    assert promoted == ["body"]
    parent_of = [e for e in doc.edges if e.type == EdgeType.PARENT_OF]
    assert len(parent_of) == 1
    assert parent_of[0].source_id == "h1" and parent_of[0].target_id == "body"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_promote_heading_parents_skips_reparent_that_would_create_cycle() -> None:
    """The cycle risk that arises once Text can be both parent and child —
    with a->b already linked, if the model picks b as a's new parent (the
    opposite direction), that would create a cycle, so it must be skipped.
    The original structure survives unchanged."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    a = _text("a", "가. 섹션 A")
    b = _text("b", "나. 섹션 B")
    edges = [
        Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="a"),
        Edge(type=EdgeType.PARENT_OF, source_id="a", target_id="b"),  # b is already a's child
    ]
    doc = ArticDocument(source_path="x", format="docx", nodes=[art1, a, b], edges=list(edges))

    # both a and b are text-only anchors with no pixel, so they're processed
    # together in one batch (1 call) — anchor_index 0=a (candidates=[b])
    # picks b (attempting to create a cycle) -> must be skipped.
    # anchor_index 1=b (candidates=[a]) picks nothing (kept irrelevant to
    # keep this test focused only on a's cycle check).
    fake_result = _HeadingParentBatchResult(
        choices=[
            _HeadingParentChoice(anchor_index=0, parent_index=0, rationale="사이클 유발 시도"),
            _HeadingParentChoice(anchor_index=1, parent_index=None, rationale="헤딩 아님"),
        ]
    )
    client = _FakeClient(fake_result)

    promoted = promote_heading_parents(doc, client=client, window=3, include_text_anchors=True)

    assert promoted == [], "a reparent that creates a cycle must not be applied"
    assert doc.edges == edges, "the original structure must be preserved as-is"
    assert check_invariants(doc.nodes, doc.edges) == []
    assert len(client.responses.calls) == 1, "the 2 pixel-less anchors must be processed as one batch (1 call)"


def test_promote_heading_parents_processes_pixel_and_text_only_anchors_separately(tmp_path: Path) -> None:
    """An anchor with a pixel (a Table's `capture_path`) must be handled
    with an individual call to avoid mixing several images into one batch,
    while a pixel-less anchor must be handled with a batched call — both
    run concurrently on the same thread pool, but the reparent result must
    be correct for both (2026-09-05, `docs/vlm-integration-research.md`
    §14)."""
    png = tmp_path / "capture.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    heading = _text("h1", "1. 개요")
    tbl1 = Node(id="tbl1", type=NodeType.TABLE, name="표", properties={"grid": [["a"]], "capture_path": str(png)})
    body = _text("body", "본문 문단")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[heading, tbl1, body],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="tbl1"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="body"),
        ],
    )

    # tbl1 (has a pixel): picks heading (index 0) among candidates=[heading,
    # body] — an individual call. heading/body (no pixel, text_only):
    # processed together in one batch — filling in only body's answer is
    # enough (heading's own reparent attempt is ignored since h1 has no
    # existing edge, the same pattern as the other tests above).
    client = _ShapeAwareClient({
        _HeadingParentChoice: _HeadingParentChoice(parent_index=0, rationale="개요 섹션 소속(표)"),
        _HeadingParentBatchResult: _HeadingParentBatchResult(
            choices=[_HeadingParentChoice(anchor_index=1, parent_index=0, rationale="개요 섹션 소속(본문)")]
        ),
    })

    promoted = promote_heading_parents(doc, client=client, window=3, include_text_anchors=True)

    assert set(promoted) == {"tbl1", "body"}
    assert len(client.responses.calls) == 2, "pixel anchor (1 individual call) + pixel-less anchors (1 batch call) = 2 calls total"
    parent_by_target = {e.target_id: e.source_id for e in doc.edges if e.type == EdgeType.PARENT_OF}
    assert parent_by_target["tbl1"] == "h1"
    assert parent_by_target["body"] == "h1"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_apply_vlm_enrichment_runs_promotion_then_proposal() -> None:
    """apply_vlm_enrichment runs promote_heading_parents and propose_edges
    in order with the same client, so both the structural reparent
    (PARENT_OF) and the edge proposal (CAPTION_OF) must be reflected in doc
    — the behavior the CLI --vlm-enrichment/demo checkbox expects.
    (merge_fragmented_text also runs first, but this test's nodes have no
    bbox/page_index so no cluster is caught, making it a silent no-op with
    no API call — the call count of 2 below stays the same.)"""
    heading = _text("h1", "3. 측정 결과")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    caption = _text("t2", "그림 1. 측정 결과 사진")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[heading, img1, caption],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")],
    )

    # the first call is promote_heading_parents (one img1 anchor = 1 batch
    # call, candidates=[heading, caption]): picks the heading (index 0). The
    # second call is propose_edges for the same anchor (not batched, as-is):
    # proposes that caption (index 1) is a CAPTION_OF img1.
    heading_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=0, parent_index=0, rationale="측정 결과 섹션 제목")]
    )
    edge_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=1, edge_type="CAPTION_OF", rationale="그림 1로 명시적 지칭")]
    )
    client = _SequencedClient([heading_result, edge_result])

    promoted = apply_vlm_enrichment(doc, client=client, include_text_anchors=False)

    assert promoted == ["img1"], "must return exactly the reparented-node id list that promote_heading_parents returned"
    parent_of = [e for e in doc.edges if e.type == EdgeType.PARENT_OF]
    assert len(parent_of) == 1 and parent_of[0].source_id == "h1", "PARENT_OF must be reparented under the heading"
    caption_edges = [e for e in doc.edges if e.type == EdgeType.CAPTION_OF]
    assert len(caption_edges) == 1 and caption_edges[0].source_id == "t2", "the propose_edges proposal must be added to doc.edges"
    assert len(client.responses.calls) == 2, "must be called twice total: once for heading reparent, once for edge proposal"


def test_nest_numbered_headings_reparents_subsections_under_their_section() -> None:
    """Reproduces heading text exactly as confirmed (`1706.03762`) —
    "3.1"/"3.2" must be reparented under "3", "3.2.1" under "3.2", and the
    top-level sections ("1", "3") must stay in their original place
    (Artifact). There must be no API call at all (no VLM/API needed)."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    intro = _text("h_intro", "1\nIntroduction")
    model_arch = _text("h_3", "3\nModel Architecture")
    h31 = _text("h_31", "3.1\nEncoder and Decoder Stacks")
    h32 = _text("h_32", "3.2\nAttention")
    h321 = _text("h_321", "3.2.1\nScaled Dot-Product Attention")
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[art1, intro, model_arch, h31, h32, h321],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_intro"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_3"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_31"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_32"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_321"),
        ],
    )

    nested = nest_numbered_headings(doc)

    assert set(nested) == {"h_31", "h_32", "h_321"}
    parent_by_target = {e.target_id: e.source_id for e in doc.edges if e.type == EdgeType.PARENT_OF}
    assert parent_by_target["h_31"] == "h_3"
    assert parent_by_target["h_32"] == "h_3"
    assert parent_by_target["h_321"] == "h_32", "3.2.1 must go under its immediate parent section 3.2, not 3"
    assert parent_by_target["h_intro"] == "art1", "a top-level section (no dot in its number) must stay under the Artifact"
    assert parent_by_target["h_3"] == "art1"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_nest_numbered_headings_ignores_table_data_rows() -> None:
    """Not everything starting with a digit is a heading — a table data row
    ("1 512 512 5.29 24.9") must not be mistaken for a heading, since
    another digit (not a letter) follows the number (a confirmed
    counterexample, `1706.03762` Table 3)."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    section3 = _text("h_3", "3\nModel Architecture")
    table_row = _text("row", "1 512 512 5.29 24.9 4 128 128 5.00 25.5 16 32 32 4.91 2")
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[art1, section3, table_row],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_3"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="row"),
        ],
    )

    nested = nest_numbered_headings(doc)

    assert nested == []
    assert {e.target_id: e.source_id for e in doc.edges if e.type == EdgeType.PARENT_OF}["row"] == "art1"


def test_nest_numbered_headings_leaves_orphan_when_parent_section_missing() -> None:
    """If the parent section's heading isn't in the document (dropped
    during extraction, or never there to begin with), that subsection is
    left where it is — no parent is invented."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    h51 = _text("h_51", "5.1\nTraining Data and Batching")  # "5\nTraining" isn't in the document
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[art1, h51],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_51")],
    )

    nested = nest_numbered_headings(doc)

    assert nested == []
    assert doc.edges == [Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h_51")]


def test_find_same_line_text_clusters_groups_by_y_overlap_and_x_order() -> None:
    """Reproduces coordinates exactly as confirmed
    (`1706.03762`'s MultiHead attention formula,
    `docs/vlm-integration-research.md` §11.2) — only fragments whose y
    overlaps should form a cluster, sorted in ascending x. Another line (a
    standalone node) must not enter the cluster."""
    multihead = _pdf_text("multihead", "MultiHead(...)", _bbox(186.94, 145.82, 410.92, 158.40))
    where_q = _pdf_text("where_Q", "where headi = Attention(QW Q", _bbox(224.497, 161.956, 358.449, 175.865))
    frag_v = _pdf_text("frag_V", "i , V W V", _bbox(381.956, 162.630, 418.974, 176.833))
    frag_close = _pdf_text("frag_close", "i )", _bbox(412.883, 164.407, 425.061, 176.833))
    frag_k = _pdf_text("frag_K", "i , KW K", _bbox(350.789, 162.630, 390.035, 177.147))
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[multihead, where_q, frag_v, frag_close, frag_k],
        edges=[],
    )

    clusters = _find_same_line_text_clusters(doc)

    assert len(clusters) == 1, "the MultiHead(...) line is alone and must not become a cluster"
    assert [n.id for n in clusters[0]] == ["where_Q", "frag_K", "frag_V", "frag_close"]


def test_merge_fragmented_text_merges_when_model_confirms() -> None:
    """If the model answers "should merge," it must merge into the
    cluster's first node, and the rest of the fragment nodes / edges
    pointing at them must be removed — the first node's existing PARENT_OF
    must carry over unchanged, with no need to create a new edge."""
    where_q = _pdf_text("where_Q", "where headi = Attention(QW Q", _bbox(224.5, 162.0, 358.4, 175.9))
    frag_k = _pdf_text("frag_K", "i , KW K", _bbox(350.8, 162.6, 390.0, 177.1))
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[where_q, frag_k],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="where_Q"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="frag_K"),
        ],
    )
    merged_text = "where headi = Attention(QW Q i , KW K"
    fake_result = _FragmentMergeBatchResult(
        decisions=[_FragmentMergeDecision(cluster_index=0, should_merge=True, merged_text=merged_text)]
    )
    client = _FakeClient(fake_result)

    merged = merge_fragmented_text(doc, client=client, model="test-model")

    assert merged == ["where_Q"]
    assert [n.id for n in doc.nodes] == ["where_Q"], "the rest of the fragment nodes must be removed"
    anchor = doc.nodes[0]
    assert anchor.properties["text"] == merged_text
    assert anchor.properties["merged_from"] == ["frag_K"]
    assert anchor.properties["merged_by"] == "llm:test-model"
    assert anchor.properties["bbox"] == _bbox(224.5, 162.0, 390.0, 177.1), "bbox must enclose the whole cluster"
    assert [e.target_id for e in doc.edges] == ["where_Q"], "the edge pointing at frag_K must be removed, leaving only where_Q's"


def test_merge_fragmented_text_leaves_cluster_when_model_declines() -> None:
    """If the model answers "don't merge" (should_merge=False), nothing
    should change — the safeguard that came out of a real case (two
    authors' names on the same line, §11.2)."""
    ashish = _pdf_text("ashish", "Ashish Vaswani∗", _bbox(132.9, 233.5, 203.9, 245.0))
    noam = _pdf_text("noam", "Noam Shazeer∗", _bbox(239.1, 233.5, 304.8, 245.0))
    doc = ArticDocument(source_path="x", format="pdf", nodes=[ashish, noam], edges=[])
    fake_result = _FragmentMergeBatchResult(
        decisions=[_FragmentMergeDecision(cluster_index=0, should_merge=False, merged_text="")]
    )
    client = _FakeClient(fake_result)

    merged = merge_fragmented_text(doc, client=client)

    assert merged == []
    assert {n.id for n in doc.nodes} == {"ashish", "noam"}
    assert doc.nodes[0].properties["text"] == "Ashish Vaswani∗", "the original text must survive unchanged"


def test_merge_fragmented_text_skips_cluster_on_api_failure() -> None:
    """Even if the API call fails (a rate limit, etc.), that cluster must
    be left fragmented as-is — the same partial-failure principle as the
    other batch code."""
    a = _pdf_text("a", "frag a", _bbox(72.0, 100.0, 150.0, 112.0))
    b = _pdf_text("b", "frag b", _bbox(160.0, 100.0, 230.0, 112.0))
    doc = ArticDocument(source_path="x", format="pdf", nodes=[a, b], edges=[])
    client = _SequencedClient([RuntimeError("simulated API error")])

    merged = merge_fragmented_text(doc, client=client)

    assert merged == []
    assert {n.id for n in doc.nodes} == {"a", "b"}


def test_merge_fragmented_text_batches_multiple_clusters_into_one_call() -> None:
    """If several clusters all fit into one `batch_size`, there must be
    exactly 1 API call, and the batch response's `cluster_index` must map
    exactly back to each cluster — verifies the batching itself that cuts
    round trips (2026-09-05, `docs/vlm-integration-research.md` §14)."""
    a1 = _pdf_text("a1", "frag a1", _bbox(72.0, 100.0, 130.0, 112.0))
    a2 = _pdf_text("a2", "frag a2", _bbox(140.0, 100.0, 200.0, 112.0))
    b1 = _pdf_text("b1", "frag b1", _bbox(72.0, 200.0, 130.0, 212.0))
    b2 = _pdf_text("b2", "frag b2", _bbox(140.0, 200.0, 200.0, 212.0))
    doc = ArticDocument(source_path="x", format="pdf", nodes=[a1, a2, b1, b2], edges=[])

    fake_result = _FragmentMergeBatchResult(
        decisions=[
            _FragmentMergeDecision(cluster_index=0, should_merge=True, merged_text="A1 A2"),
            _FragmentMergeDecision(cluster_index=1, should_merge=False, merged_text=""),
        ]
    )
    client = _FakeClient(fake_result)

    merged = merge_fragmented_text(doc, client=client, batch_size=25, max_workers=1)

    assert len(client.responses.calls) == 1, "if 2 clusters both fit into one batch, there must be exactly 1 call"
    assert merged == ["a1"]
    assert {n.id for n in doc.nodes} == {"a1", "b1", "b2"}, "only cluster 0 (a1,a2) should merge; cluster 1 (b1,b2) must stay as-is"


def test_merge_fragmented_text_ignores_nodes_without_bbox() -> None:
    """A Text with no bbox/page_index (a DOCX/PPTX/XLSX node) isn't a
    cluster target, so there must be no API call at all — a silent no-op
    on other format documents."""
    t1 = _text("t1", "일반 문단 1")
    t2 = _text("t2", "일반 문단 2")
    doc = ArticDocument(source_path="x", format="docx", nodes=[t1, t2], edges=[])
    client = _RecordingResponses(None)
    fake_client = _FakeClient(None)
    fake_client.responses = client

    merged = merge_fragmented_text(doc, client=fake_client)

    assert merged == []
    assert len(client.calls) == 0


def test_find_semantic_text_regions_uses_2d_neighbors_not_only_same_line() -> None:
    """The same author's name and affiliation/email, stacked vertically with
    no y overlap, must still land in the same VLM review region. At the
    same time, a neighboring author on the same row must also be included
    in the region, so the VLM can look at the 2D column layout and
    partition the two into separate semantic groups."""
    ashish = _pdf_text("ashish", "Ashish Vaswani", _bbox(217, 295, 333, 309))
    noam = _pdf_text("noam", "Noam Shazeer", _bbox(391, 295, 498, 309))
    ashish_info = _pdf_text("ashish_info", "Google Brain\navaswani@google.com", _bbox(191, 311, 353, 337))
    noam_info = _pdf_text("noam_info", "Google Brain\nnoam@google.com", _bbox(377, 311, 505, 337))
    abstract = _pdf_text("abstract", "Abstract", _bbox(464, 487, 536, 502))
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[ashish, noam, ashish_info, noam_info, abstract], edges=[],
    )

    regions = _find_semantic_text_regions(doc)

    assert len(regions) == 1
    assert [node.id for node in regions[0]] == ["ashish", "noam", "ashish_info", "noam_info"]


def test_merge_semantic_text_groups_applies_vlm_partition_without_rewriting_text() -> None:
    """The VLM only picks group indices within the region, and the actual
    merged text must join the source text in spatial order. Another
    author's node in the same region must be kept independent."""
    ashish = _pdf_text("ashish", "Ashish Vaswani", _bbox(217, 295, 333, 309))
    noam = _pdf_text("noam", "Noam Shazeer", _bbox(391, 295, 498, 309))
    ashish_info = _pdf_text("ashish_info", "Google Brain\navaswani@google.com", _bbox(191, 311, 353, 337))
    noam_info = _pdf_text("noam_info", "Google Brain\nnoam@google.com", _bbox(377, 311, 505, 337))
    doc = ArticDocument(
        source_path="missing.pdf", format="pdf",
        nodes=[ashish, noam, ashish_info, noam_info],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="artifact", target_id=node.id)
            for node in (ashish, noam, ashish_info, noam_info)
        ],
    )
    result = _SemanticRegionBatchResult(decisions=[
        _SemanticRegionDecision(
            region_index=0,
            groups=[
                _SemanticTextGroup(node_indices=[0, 2], rationale="name with affiliation and email below it"),
                _SemanticTextGroup(node_indices=[1, 3], rationale="name with affiliation and email below it"),
            ],
        )
    ])

    merged = merge_semantic_text_groups(doc, client=_FakeClient(result), model="test-model", max_workers=1)

    assert merged == ["ashish", "noam"]
    assert [node.id for node in doc.nodes] == ["ashish", "noam"]
    assert ashish.properties["text"] == "Ashish Vaswani\nGoogle Brain\navaswani@google.com"
    assert noam.properties["text"] == "Noam Shazeer\nGoogle Brain\nnoam@google.com"
    assert ashish.properties["merged_by"] == "vlm-semantic:test-model"
    assert {edge.target_id for edge in doc.edges} == {"ashish", "noam"}


def test_propose_synthetic_groups_creates_group_and_reparents_members() -> None:
    """When 4 sibling author blocks (no 'Authors' heading in the source)
    sit under one parent (artifact), if the VLM judges them to be one
    group, a new Group node must be created and the existing
    artifact->member PARENT_OF edges must be reparented to
    artifact->Group->member."""
    ashish = _pdf_text("ashish", "Ashish Vaswani\nGoogle Brain\navaswani@google.com", _bbox(191, 295, 353, 337))
    noam = _pdf_text("noam", "Noam Shazeer\nGoogle Brain\nnoam@google.com", _bbox(377, 295, 505, 337))
    niki = _pdf_text("niki", "Niki Parmar\nGoogle Research\nnikip@google.com", _bbox(529, 295, 666, 337))
    abstract = _pdf_text("abstract_heading", "Abstract", _bbox(464, 487, 536, 502))
    artifact = Node(id="artifact", type=NodeType.ARTIFACT, name="p1", properties={})
    doc = ArticDocument(
        source_path="missing.pdf", format="pdf",
        nodes=[artifact, ashish, noam, niki, abstract],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="artifact", target_id=node.id)
            for node in (ashish, noam, niki, abstract)
        ],
    )
    result = _SiblingRegionBatchResult(decisions=[
        _SiblingRegionDecision(
            region_index=0,
            groups=[
                _SyntheticGroupCandidate(
                    member_indices=[0, 1, 2], group_type="authors", confidence=0.95,
                    basis=["spatial", "structural", "semantic"], rationale="3 repeated name+affiliation+email patterns",
                )
            ],
        )
    ])

    created = propose_synthetic_groups(doc, client=_FakeClient(result), model="test-model", max_workers=1)

    assert len(created) == 1
    group_id = created[0]
    group_node = next(n for n in doc.nodes if n.id == group_id)
    assert group_node.type == NodeType.GROUP
    assert group_node.properties["synthetic"] is True
    assert group_node.properties["group_type"] == "authors"
    assert group_node.properties["proposed_by"] == "llm:test-model"
    assert "text" not in group_node.properties, "a Group never has source text put into it"

    parent_of = {(e.source_id, e.target_id) for e in doc.edges if e.type == EdgeType.PARENT_OF}
    assert ("artifact", group_id) in parent_of
    assert (group_id, "ashish") in parent_of
    assert (group_id, "noam") in parent_of
    assert (group_id, "niki") in parent_of
    assert ("artifact", "ashish") not in parent_of, "a reparented member must no longer be a direct child of artifact"
    assert ("artifact", "abstract_heading") in parent_of, "a sibling not in the group must be left as-is"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_propose_synthetic_groups_skips_when_vlm_finds_no_group() -> None:
    """If the VLM answers with groups=[] (not enough shared cues), nothing
    should be created and the original structure left as-is — the
    principle that no group beats a false one."""
    a = _pdf_text("a", "Section 1", _bbox(0, 0, 100, 20))
    b = _pdf_text("b", "Unrelated paragraph", _bbox(0, 40, 100, 60))
    doc = ArticDocument(
        source_path="missing.pdf", format="pdf", nodes=[a, b],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="artifact", target_id="a"),
            Edge(type=EdgeType.PARENT_OF, source_id="artifact", target_id="b"),
        ],
    )
    result = _SiblingRegionBatchResult(decisions=[_SiblingRegionDecision(region_index=0, groups=[])])

    created = propose_synthetic_groups(doc, client=_FakeClient(result), model="test-model", max_workers=1)

    assert created == []
    assert [n.type for n in doc.nodes] == [NodeType.TEXT, NodeType.TEXT]
    assert len(doc.edges) == 2


def test_resolve_ambiguous_captions_ignores_unambiguous_edges(tmp_path: Path) -> None:
    """A CAPTION_OF with only one target is already certain, so it's left as-is with no VLM call."""
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    caption = _text("t1", "그림 1. 사진")
    image = _image_node("img1", "이미지 1", str(png))
    doc = ArticDocument(
        source_path="x", format="pdf", nodes=[caption, image],
        edges=[Edge(type=EdgeType.CAPTION_OF, source_id="t1", target_id="img1")],
    )
    client = _SequencedClient([])

    resolved = resolve_ambiguous_captions(doc, client=client)

    assert resolved == []
    assert client.responses.calls == []
    assert len(doc.edges) == 1, "an unambiguous edge must not be touched"


def test_resolve_ambiguous_captions_removes_unconfirmed_edge(tmp_path: Path) -> None:
    """An ambiguous case where one caption is attached to both neighboring
    images — if the VLM confirms only candidate 1 (index 1), the edge to
    candidate 0 must be removed and only the edge to candidate 1 must
    survive."""
    png1, png2 = tmp_path / "a.png", tmp_path / "b.png"
    png1.write_bytes(b"\x89PNG\r\n\x1a\n")
    png2.write_bytes(b"\x89PNG\r\n\x1a\n")
    caption = _text("t1", "그림 1. 회로기판 사진")
    img_a = _image_node("img_a", "이미지 A", str(png1))
    img_b = _image_node("img_b", "이미지 B", str(png2))
    doc = ArticDocument(
        source_path="x", format="pdf", nodes=[caption, img_a, img_b],
        edges=[
            Edge(type=EdgeType.CAPTION_OF, source_id="t1", target_id="img_a"),
            Edge(type=EdgeType.CAPTION_OF, source_id="t1", target_id="img_b"),
        ],
    )
    fake_result = _CaptionDisambiguation(confirmed_indices=[1], rationale="candidate 1 is actually the circuit board")
    client = _SequencedClient([fake_result])

    resolved = resolve_ambiguous_captions(doc, client=client)

    assert resolved == ["t1"]
    assert len(client.responses.calls) == 1
    remaining = [(e.source_id, e.target_id) for e in doc.edges]
    assert remaining == [("t1", "img_b")], "only the unconfirmed side's (img_a) edge must be removed"


def test_resolve_ambiguous_captions_keeps_ambiguity_on_missing_pixel(tmp_path: Path) -> None:
    """If even one candidate has no pixel (no image_path/no file), judgment
    isn't possible — must not call the VLM and must safely leave it
    ambiguous (neither removing both nor arbitrarily keeping just one)."""
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    caption = _text("t1", "그림 1. 사진")
    img_a = _image_node("img_a", "이미지 A", str(png))
    img_b = Node(id="img_b", type=NodeType.IMAGE, name="이미지 B", properties={})  # no image_path
    doc = ArticDocument(
        source_path="x", format="pdf", nodes=[caption, img_a, img_b],
        edges=[
            Edge(type=EdgeType.CAPTION_OF, source_id="t1", target_id="img_a"),
            Edge(type=EdgeType.CAPTION_OF, source_id="t1", target_id="img_b"),
        ],
    )
    client = _SequencedClient([])

    resolved = resolve_ambiguous_captions(doc, client=client)

    assert resolved == []
    assert client.responses.calls == []
    assert len(doc.edges) == 2, "with a pixel-less candidate mixed in, must not judge and must leave it ambiguous"


def test_resolve_ambiguous_captions_keeps_ambiguity_on_api_failure(tmp_path: Path) -> None:
    """If the API call fails (a rate limit, etc.), that group must be safely left ambiguous."""
    png1, png2 = tmp_path / "a.png", tmp_path / "b.png"
    png1.write_bytes(b"\x89PNG\r\n\x1a\n")
    png2.write_bytes(b"\x89PNG\r\n\x1a\n")
    caption = _text("t1", "그림 1. 사진")
    img_a = _image_node("img_a", "이미지 A", str(png1))
    img_b = _image_node("img_b", "이미지 B", str(png2))
    doc = ArticDocument(
        source_path="x", format="pdf", nodes=[caption, img_a, img_b],
        edges=[
            Edge(type=EdgeType.CAPTION_OF, source_id="t1", target_id="img_a"),
            Edge(type=EdgeType.CAPTION_OF, source_id="t1", target_id="img_b"),
        ],
    )
    client = _SequencedClient([RuntimeError("simulated API error")])

    resolved = resolve_ambiguous_captions(doc, client=client)

    assert resolved == []
    assert len(doc.edges) == 2, "on API failure, the original ambiguous state must be preserved as-is"


def test_propose_for_anchor_falls_back_when_pixel_file_missing(tmp_path: Path) -> None:
    anchor = Node(
        id="tbl1", type=NodeType.TABLE, name="표",
        properties={"grid": [["a"]], "capture_path": str(tmp_path / "does_not_exist.png")},
    )
    candidate = _text("t1", "표 1")
    fake_result = _EdgeProposalResult(proposals=[])
    client = _FakeClient(fake_result)

    _propose_for_anchor(client, "gpt-5.6-terra", anchor, [candidate])

    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, str), "if capture_path doesn't point to an actual file, it must fall back to text"


def test_propose_edges_attaches_pdf_layout_crop_alongside_isolated_image(tmp_path: Path) -> None:
    """When it's an actual PDF document (with the original still present),
    the anchor's isolated image (what its content is) alone isn't enough —
    a layout crop showing how the anchor+candidates are actually laid out
    on the page must be attached too — text/isolated crop alone can't
    reveal spatial information like "is the caption directly below it"
    (a design decided in the 2026-09-05 discussion)."""
    pdf_path = build_pdf_with_figure_caption(tmp_path / "fig.pdf")
    doc = pdf_extractor.extract(pdf_path)

    fake_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=0, edge_type="CAPTION_OF", rationale="caption directly below the photo")]
    )
    client = _FakeClient(fake_result)

    proposals = propose_edges(doc, client=client, window=3)

    assert len(client.responses.calls) == 1
    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, list)
    image_blocks = [c for c in content if c["type"] == "input_image"]
    assert len(image_blocks) == 2, "both the image's own close-up (isolated crop) and the page layout crop must be attached"
    assert len(proposals) == 1


def test_propose_edges_no_layout_crop_for_non_pdf_document() -> None:
    """A non-PDF document (`format != 'pdf'`) never has `page_index`/`bbox`
    to begin with, so the old behavior (isolated crop only, or text-only)
    must be kept unchanged with no layout crop — the path where
    `_open_pdf_document` immediately returns None."""
    t1 = _text("t1", "표 1. 측정 결과")
    tbl1 = Node(id="tbl1", type=NodeType.TABLE, name="표", properties={"grid": [["a"]]})
    doc = ArticDocument(source_path="x", format="docx", nodes=[t1, tbl1], edges=[])

    fake_result = _EdgeProposalResult(proposals=[])
    client = _FakeClient(fake_result)

    propose_edges(doc, client=client, window=3)

    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, str), "with no PDF, there's no layout crop either, so it must stay text-only"


def test_merge_fragmented_text_attaches_pdf_layout_crop(tmp_path: Path) -> None:
    """When there's an actual PDF original (`document.source_path`), for
    each same-line cluster the image cropped from that page area must also
    be sent to the API — so the VLM can directly see the real layout
    (baseline mismatch, spacing) that text alone can't reveal (a design
    decided in the 2026-09-05 discussion)."""
    pdf_path = build_pdf_with_figure_caption(tmp_path / "sample.pdf")  # any PDF that just opens is fine
    where_q = _pdf_text("where_Q", "where headi = Attention(QW Q", _bbox(100.0, 100.0, 300.0, 120.0))
    frag_k = _pdf_text("frag_K", "i , KW K", _bbox(300.0, 101.0, 400.0, 121.0))
    doc = ArticDocument(source_path=str(pdf_path), format="pdf", nodes=[where_q, frag_k], edges=[])

    fake_result = _FragmentMergeBatchResult(
        decisions=[_FragmentMergeDecision(cluster_index=0, should_merge=True, merged_text="merged")]
    )
    client = _FakeClient(fake_result)

    merged = merge_fragmented_text(doc, client=client, model="test-model")

    assert merged == ["where_Q"]
    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, list), "with an actual PDF present, the content must be a list with an image attached"
    types = [c["type"] for c in content]
    assert "input_image" in types, "the cluster's actual page crop must be attached"


def test_merge_fragmented_text_no_crop_when_source_pdf_missing() -> None:
    """If `source_path` doesn't point to an actual file (as in the existing
    fake-doc tests), it must silently fall back to text-only as before,
    with no crop — no path fails completely (guarantees that the other
    tests' `source_path="x"` pattern keeps working)."""
    where_q = _pdf_text("where_Q", "frag a", _bbox(72.0, 100.0, 150.0, 112.0))
    frag_k = _pdf_text("frag_K", "frag b", _bbox(160.0, 100.0, 230.0, 112.0))
    doc = ArticDocument(source_path="x", format="pdf", nodes=[where_q, frag_k], edges=[])
    fake_result = _FragmentMergeBatchResult(
        decisions=[_FragmentMergeDecision(cluster_index=0, should_merge=False, merged_text="")]
    )
    client = _FakeClient(fake_result)

    merge_fragmented_text(doc, client=client)

    content = client.responses.calls[0]["input"][0]["content"]
    types = [c["type"] for c in content]
    assert "input_image" not in types, "with no original PDF, only text blocks should be sent, no crop"


def test_propose_edges_attaches_xlsx_layout_crop_alongside_isolated_image(tmp_path: Path) -> None:
    """The same principle as PDF applies to XLSX too — when the original
    workbook is still present, the table's own isolated capture
    (`capture_path`, what the table's content is) alone isn't enough — a
    layout crop showing how the table+caption are actually laid out on the
    sheet must be attached too (a design decided in the 2026-09-05
    discussion — XLSX after PDF)."""
    xlsx_path = build_xlsx_with_table_caption(tmp_path / "table.xlsx")
    doc = xlsx_extractor.extract(xlsx_path, capture_dir=tmp_path / "captures")

    fake_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=0, edge_type="CAPTION_OF", rationale="caption directly above the table")]
    )
    client = _FakeClient(fake_result)

    proposals = propose_edges(doc, client=client, window=3)

    assert len(client.responses.calls) == 1
    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, list)
    image_blocks = [c for c in content if c["type"] == "input_image"]
    assert len(image_blocks) == 2, "both the table's own close-up (isolated crop) and the sheet layout crop must be attached"
    assert len(proposals) == 1


def test_propose_edges_no_layout_crop_when_source_xlsx_missing() -> None:
    """If `source_path` doesn't point to an actual file (even claiming xlsx
    format), it must silently fall back to the old text/isolated-crop path
    with no crop — no path fails completely."""
    t1 = _text("t1", "표 1. 측정 결과")
    tbl1 = Node(id="tbl1", type=NodeType.TABLE, name="표", properties={"grid": [["a"]]})
    doc = ArticDocument(source_path="does-not-exist.xlsx", format="xlsx", nodes=[t1, tbl1], edges=[])

    fake_result = _EdgeProposalResult(proposals=[])
    client = _FakeClient(fake_result)

    propose_edges(doc, client=client, window=3)

    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, str), "with no original workbook, there's no layout crop either, so it must stay text-only"


def test_pptx_context_windows_choose_spatially_nearest_text() -> None:
    """A PPTX relation candidate must use the same-slide bbox distance, not
    node array order. A far-away Text stored first must not win over a
    nearby caption."""
    artifact = Node(id="slide", type=NodeType.ARTIFACT, name="Slide", properties={"slide_index": 0})
    far = _pdf_text("far", "멀리 있는 설명", _bbox(800, 800, 950, 850))
    near = _pdf_text("near", "바로 위 캡션", _bbox(100, 80, 300, 120))
    anchor = Node(
        id="image", type=NodeType.IMAGE, name="Image",
        properties={"slide_index": 0, "bbox": _bbox(100, 130, 300, 400)},
    )
    for node in (far, near):
        node.properties.pop("page_index")
        node.properties["slide_index"] = 0
    doc = ArticDocument(
        source_path="x", format="pptx", nodes=[artifact, far, anchor, near],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="slide", target_id=node.id)
            for node in (far, anchor, near)
        ],
    )

    windows = _build_document_context_windows(
        doc, window=1, anchor_types=(NodeType.IMAGE,),
        boundary_types=(NodeType.TABLE, NodeType.IMAGE),
    )

    assert len(windows) == 1
    assert [node.id for node in windows[0][1]] == ["near", "far"]


def test_propose_edges_attaches_pptx_slide_pixels(tmp_path: Path) -> None:
    """Relations receive an attached real slide plus the individual image."""
    pptx_path = build_pptx(tmp_path / "layout.pptx")
    doc = pptx_extractor.extract(pptx_path, capture_dir=tmp_path / "captures")
    from PIL import Image
    from articling.relations.slide_images import attach_pptx_slide_images
    paths = {}
    for node in doc.nodes:
        if node.type == NodeType.ARTIFACT:
            path = tmp_path / f"slide-{node.properties['slide_index']}.png"
            Image.new("RGB", (800, round(800 * node.properties['slide_height'] / node.properties['slide_width'])), "white").save(path)
            paths[node.properties['slide_index']] = path
    attach_pptx_slide_images(doc, paths)
    client = _FakeClient(_EdgeProposalResult(proposals=[]))

    propose_edges(doc, client=client)

    assert client.responses.calls
    contents = [call["input"][0]["content"] for call in client.responses.calls]
    assert all(isinstance(content, list) for content in contents)
    assert all(any(block["type"] == "input_image" for block in content) for content in contents)
    assert any(
        sum(block["type"] == "input_image" for block in content) == 2
        for content in contents
    ), "an Image anchor must receive both the individual image and the slide layout map"


def test_promote_heading_parents_attaches_xlsx_layout_crop(tmp_path: Path) -> None:
    """XLSX hierarchy judgment must also receive the same real sheet crop as relation judgment does."""
    xlsx_path = build_xlsx_with_table_caption(tmp_path / "heading.xlsx")
    doc = xlsx_extractor.extract(xlsx_path, capture_dir=tmp_path / "captures")
    client = _FakeClient(_HeadingParentChoice(parent_index=0, rationale="title directly above the table"))

    promoted = promote_heading_parents(doc, client=client, max_workers=1)

    assert promoted
    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, list)
    assert any(block["type"] == "input_image" for block in content)


def test_pptx_plot_labels_do_not_exclude_caption_or_cross_slide() -> None:
    """Dense in-plot annotations must not consume all caption candidate slots."""
    slide = Node(id='slide0', type=NodeType.ARTIFACT, name='Slide')
    other = Node(id='slide1', type=NodeType.ARTIFACT, name='Other slide')
    image = Node(id='plot', type=NodeType.IMAGE, name='Plot', properties={'bbox': _bbox(100, 100, 500, 600)})
    labels = [_pdf_text(f'label{i}', f'phase {i}', _bbox(150, 150+i*20, 200, 170+i*20)) for i in range(5)]
    caption = _pdf_text('before', '변경 전', _bbox(250, 620, 350, 660))
    unrelated = _pdf_text('other-caption', '변경 후', _bbox(100, 100, 200, 150))
    nodes = [image, *labels, caption]
    doc = ArticDocument(source_path='x.pptx', format='pptx', nodes=[slide, other, *nodes, unrelated], edges=[
        *[Edge(type=EdgeType.PARENT_OF, source_id=slide.id, target_id=n.id) for n in nodes],
        Edge(type=EdgeType.PARENT_OF, source_id=other.id, target_id=unrelated.id),
    ])
    windows = _build_document_context_windows(doc, window=3, anchor_types=(NodeType.IMAGE,), boundary_types=(NodeType.IMAGE, NodeType.TABLE))
    ids = [n.id for n in windows[0][1]]
    assert 'before' in ids and 'other-caption' not in ids
    assert len(ids) == 6
