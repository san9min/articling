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
    _build_document_context_windows, _enumerated_sibling_groups, _find_same_line_text_clusters,
    _find_semantic_text_regions, _open_heading_marker_families, _OPEN_HEADING_MAX_LEN,
    _propose_for_anchor, _propose_text_heading_parents, _widen_row_sibling_windows,
    _widen_with_confirmed_headings, _widen_with_open_headings,
    apply_vlm_enrichment, build_context_windows, merge_fragmented_text,
    merge_semantic_text_groups, nest_numbered_headings,
    propose_edges, propose_synthetic_groups, resolve_ambiguous_captions,
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


def _reparented_ids(doc: ArticDocument) -> list[str]:
    """`propose_edges`'s HEADING_PARENT judgment doesn't return the ids it
    reparented (see its docstring) — every anchor it actually reparents
    carries `properties["reparented_from"]` on its new PARENT_OF edge, which
    is how a caller (and these tests) recovers the same list
    `promote_heading_parents` used to return directly."""
    return [e.target_id for e in doc.edges if e.type == EdgeType.PARENT_OF and "reparented_from" in e.properties]


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

    edges, heading_choice = _propose_for_anchor(client, "gpt-5.6-terra", anchor, [candidate])

    assert len(client.responses.calls) == 1
    content = client.responses.calls[0]["input"][0]["content"]
    assert isinstance(content, list), "with a pixel path, the content must be multimodal (a list)"
    types = [c["type"] for c in content]
    assert "input_image" in types

    assert len(edges) == 1
    assert edges[0].source_id == "t1"
    assert edges[0].target_id == "tbl1"
    assert heading_choice is None


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


class _SequentialResponses:
    """Returns a different fixed result per call, in call order — for a
    test where a later call's request depends on an earlier call's already-
    applied effect (e.g. `propose_edges`'s pass-2 retry, which only builds
    its windows after pass 1's reparents are applied to `document`), unlike
    `_FakeClient` (same result every call) or `_ShapeAwareResponses`
    (dispatches by request schema, not call order)."""

    def __init__(self, results: list):
        self._results = results
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._results[len(self.calls) - 1])


class _SequentialClient:
    def __init__(self, results: list):
        self.responses = _SequentialResponses(results)


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


def test_propose_edges_reparents_heading_when_model_picks_one() -> None:
    """If the model picks a heading among the candidates, the PARENT_OF
    that was Artifact->Image must be reparented to heading Text->Image
    (deepening the tree) — judged in the same call as CAPTION_OF/REFERENCES
    now, via the HEADING_PARENT role."""
    heading = _text("h1", "3. 측정 결과")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    other = _text("t2", "본문 설명")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[Node(id="art1", type=NodeType.ARTIFACT, name="art1"), heading, img1, other],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id) for n in (heading, img1, other)],
    )
    fake_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=0, edge_type="HEADING_PARENT", rationale="측정 결과 섹션 제목")]
    )
    client = _FakeClient(fake_result)

    proposals = propose_edges(doc, client=client, window=3)

    assert proposals == [], "HEADING_PARENT is applied directly, never returned as a proposal"
    parent_of = [e for e in doc.edges if e.type == EdgeType.PARENT_OF and e.target_id == "img1"]
    assert len(parent_of) == 1, "the old structural edge must be removed, leaving only one new edge"
    assert parent_of[0].source_id == "h1" and parent_of[0].target_id == "img1"
    assert parent_of[0].properties["reparented_from"] == "art1"
    assert check_invariants(doc.nodes, doc.edges) == [], "the fan-in<=1 invariant must still hold after reparenting"


def test_propose_edges_leaves_structure_when_model_finds_no_heading() -> None:
    heading = _text("h1", "그냥 아무 문단")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[heading, img1],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")],
    )
    fake_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=0, edge_type="NONE", rationale="헤딩 아님")]
    )
    client = _FakeClient(fake_result)

    proposals = propose_edges(doc, client=client, window=3)

    assert proposals == []
    assert doc.edges == [Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")]


def test_propose_edges_leaves_heading_structure_on_api_failure() -> None:
    """Even if the API call fails (a rate limit, etc.), that anchor's
    original structure (Artifact as parent) must be left as-is, following
    the same partial-failure principle as the other batch code."""
    heading = _text("h1", "3. 측정 결과")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    original_edge = Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="img1")
    doc = ArticDocument(source_path="x", format="docx", nodes=[heading, img1], edges=[original_edge])
    client = _SequencedClient([RuntimeError("simulated API error")])

    proposals = propose_edges(doc, client=client, window=3)

    assert proposals == []
    assert doc.edges == [original_edge]


def test_propose_edges_default_ignores_text_anchors_for_heading() -> None:
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

    proposals = propose_edges(doc, client=client, window=3)

    assert proposals == []
    assert len(client.responses.calls) == 0, "with no Table/Image, Text must not become an anchor"
    assert doc.edges == [Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="body")]


def test_propose_edges_reparents_text_under_text_when_included() -> None:
    """With `include_text_anchors=True`, a body paragraph Text can also be
    reparented under a heading Text — the "Text-Text PARENT_OF" case a user
    requested."""
    heading = _text("h1", "1. 개요")
    body = _text("body", "이 섹션은 배경을 설명한다")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[Node(id="art1", type=NodeType.ARTIFACT, name="art1"), heading, body],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id) for n in (heading, body)],
    )
    # anchor_index 0=heading (candidates=[body]), 1=body (candidates=[heading])
    # Only the body chooses a heading; the heading remains under Artifact.
    fake_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=1, parent_index=0, rationale="개요 섹션 소속")]
    )
    client = _FakeClient(fake_result)

    propose_edges(doc, client=client, window=3, include_text_anchors=True)

    assert _reparented_ids(doc) == ["body"]
    parent_of = [e for e in doc.edges if e.type == EdgeType.PARENT_OF and e.target_id == "body"]
    assert len(parent_of) == 1
    assert parent_of[0].source_id == "h1" and parent_of[0].target_id == "body"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_propose_edges_skips_reparent_that_would_create_cycle() -> None:
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

    propose_edges(doc, client=client, window=3, include_text_anchors=True)

    assert _reparented_ids(doc) == [], "a reparent that creates a cycle must not be applied"
    assert doc.edges == edges, "the original structure must be preserved as-is"
    assert check_invariants(doc.nodes, doc.edges) == []
    assert len(client.responses.calls) == 1, "the 2 pixel-less anchors must be processed as one batch (1 call)"


def test_propose_edges_retries_flat_text_anchor_with_confirmed_heading_from_pass_one() -> None:
    """Pass 2 (`_retry_flat_text_anchors_with_confirmed_headings`): a
    heading with no recognizable marker at all ("Overview Section" — no
    number/circle/bracket, so `_widen_with_open_headings` can't help it
    either) sits too far from a later paragraph for pass 1's own window to
    ever offer it as a candidate. Pass 1 *does* confirm it's a real heading
    by reparenting a closer paragraph under it, though — pass 2 then offers
    that now-confirmed heading to the distant paragraph too, resolving it
    without ever threading state through the model itself (each pass is
    still an independent, concurrent judgment — see the module comment
    above `_widen_with_confirmed_headings`)."""
    section1 = _text("section1", "Overview Section")
    local_subsection = _text("local", "Local subsection")
    fillers = [_text(f"filler{i}", f"filler paragraph {i}") for i in range(5)]
    distant_anchor = _text("distant", "Far paragraph")
    content = [section1, local_subsection, *fillers, distant_anchor]
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[Node(id="art1", type=NodeType.ARTIFACT, name="art1"), *content],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id) for n in content],
    )

    # Pass 1 batch anchor order follows content order: section1=0, local=1,
    # filler0..4=2..6, distant=7. `local`'s own window=3 candidates are
    # [section1, filler0, filler1] (only section1 sits to its left) —
    # parent_index 0 picks section1. `distant`'s own candidates are
    # [filler2, filler3, filler4] (window=3 can't reach back past the 5
    # fillers) — no choice entry for it, so it's left flat, as intended.
    pass1_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=1, parent_index=0, rationale="Overview Section 소속")]
    )
    # After pass 1 is applied, "section1" is a confirmed heading (it's
    # `local`'s real parent now). Pass 2 retries every still-flat anchor
    # whose widened candidates actually gained something new: filler0/
    # filler1 already had section1 in their own window during pass 1 (close
    # enough), so they're excluded; filler2/filler3/filler4/distant weren't
    # — batch order filler2=0, filler3=1, filler4=2, distant=3. `distant`'s
    # *widened* candidates are [filler2, filler3, filler4, section1] —
    # parent_index 3 picks section1.
    pass2_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=3, parent_index=3, rationale="Overview Section 소속 (2차)")]
    )
    client = _SequentialClient([pass1_result, pass2_result])

    propose_edges(doc, client=client, window=3, include_text_anchors=True)

    assert set(_reparented_ids(doc)) == {"local", "distant"}
    parent_of_distant = next(e for e in doc.edges if e.type == EdgeType.PARENT_OF and e.target_id == "distant")
    assert parent_of_distant.source_id == "section1"
    assert len(client.responses.calls) == 2, "pass 1 and pass 2 must each be exactly one batch call"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_propose_edges_processes_table_and_text_only_anchors_separately(tmp_path: Path) -> None:
    """A Table/Image anchor always gets an individual call (it's judged
    together with CAPTION_OF/REFERENCES in `_propose_for_anchor`, pixel or
    not), while a Text anchor with no pixel is still batched with other
    text-only Text anchors — both run concurrently on the same thread pool,
    but the reparent result must be correct for both (2026-09-05,
    `docs/vlm-integration-research.md` §14)."""
    png = tmp_path / "capture.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    heading = _text("h1", "1. 개요")
    tbl1 = Node(id="tbl1", type=NodeType.TABLE, name="표", properties={"grid": [["a"]], "capture_path": str(png)})
    body = _text("body", "본문 문단")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[Node(id="art1", type=NodeType.ARTIFACT, name="art1"), heading, tbl1, body],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="h1"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="tbl1"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="body"),
        ],
    )

    # tbl1 (Table anchor): picks heading (index 0) among candidates=[heading,
    # body] — an individual call, via _EdgeProposalResult (the merged
    # CAPTION_OF/REFERENCES/HEADING_PARENT schema). heading/body (Text
    # anchors, no pixel): processed together in one batch via the older
    # _HeadingParentBatchResult schema — filling in only body's answer is
    # enough (heading's own reparent attempt is ignored since h1 has no
    # existing edge, the same pattern as the other tests above).
    client = _ShapeAwareClient({
        _EdgeProposalResult: _EdgeProposalResult(
            proposals=[_EdgeProposal(context_index=0, edge_type="HEADING_PARENT", rationale="개요 섹션 소속(표)")]
        ),
        _HeadingParentBatchResult: _HeadingParentBatchResult(
            choices=[_HeadingParentChoice(anchor_index=1, parent_index=0, rationale="개요 섹션 소속(본문)")]
        ),
    })

    propose_edges(doc, client=client, window=3, include_text_anchors=True)

    assert set(_reparented_ids(doc)) == {"tbl1", "body"}
    assert len(client.responses.calls) == 2, "table anchor (1 individual call) + pixel-less text anchors (1 batch call) = 2 calls total"
    parent_by_target = {e.target_id: e.source_id for e in doc.edges if e.type == EdgeType.PARENT_OF}
    assert parent_by_target["tbl1"] == "h1"
    assert parent_by_target["body"] == "h1"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_apply_vlm_enrichment_runs_heading_and_caption_from_one_call() -> None:
    """apply_vlm_enrichment's `propose_edges` judges HEADING_PARENT and
    CAPTION_OF/REFERENCES for the same Table/Image anchor in **one call**
    now (the merge in this session) — both the structural reparent
    (PARENT_OF) and the edge proposal (CAPTION_OF) must be reflected in doc
    from that single response, the behavior the CLI --vlm-enrichment/demo
    checkbox expects.
    (merge_fragmented_text also runs first, but this test's nodes have no
    bbox/page_index so no cluster is caught, making it a silent no-op with
    no API call — the call count of 1 below stays the same.)"""
    heading = _text("h1", "3. 측정 결과")
    img1 = Node(id="img1", type=NodeType.IMAGE, name="이미지", properties={})
    caption = _text("t2", "그림 1. 측정 결과 사진")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[Node(id="art1", type=NodeType.ARTIFACT, name="art1"), heading, img1, caption],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id) for n in (heading, img1, caption)],
    )

    # the one and only call is for the img1 anchor (candidates=[heading,
    # caption]): picks the heading (index 0) as HEADING_PARENT and caption
    # (index 1) as CAPTION_OF, both in the same _EdgeProposalResult.
    edge_result = _EdgeProposalResult(
        proposals=[
            _EdgeProposal(context_index=0, edge_type="HEADING_PARENT", rationale="측정 결과 섹션 제목"),
            _EdgeProposal(context_index=1, edge_type="CAPTION_OF", rationale="그림 1로 명시적 지칭"),
        ]
    )
    client = _FakeClient(edge_result)

    promoted = apply_vlm_enrichment(doc, client=client, include_text_anchors=False)

    assert promoted == ["img1"], "must return exactly the node ids propose_edges's HEADING_PARENT judgment reparented"
    parent_of = [e for e in doc.edges if e.type == EdgeType.PARENT_OF and e.target_id == "img1"]
    assert len(parent_of) == 1 and parent_of[0].source_id == "h1", "PARENT_OF must be reparented under the heading"
    caption_edges = [e for e in doc.edges if e.type == EdgeType.CAPTION_OF]
    assert len(caption_edges) == 1 and caption_edges[0].source_id == "t2", "the propose_edges proposal must be added to doc.edges"
    assert len(client.responses.calls) == 1, "HEADING_PARENT and CAPTION_OF/REFERENCES are judged in one call per anchor now"


def test_propose_edges_excludes_own_caption_proposal_from_text_anchor_candidates() -> None:
    """End-to-end reproduction of a real pptx failure mode: a Table/Image
    anchor's own CAPTION_OF proposal (from the merged call) must feed
    directly into the separate Text-anchor HEADING_PARENT pass's exclusion
    set, within the same `propose_edges` call — no separate step needed.
    Once `caption` is proposed as `image`'s CAPTION_OF, it must never again
    appear as a *candidate* in the batched Text-anchor prompt — only in its
    own anchor line."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    image = Node(id="image", type=NodeType.IMAGE, name="이미지", properties={})
    caption = _text("caption", "스팀 유입 부")
    real_heading = _text("real_heading", "■ 재현 시험 결과")
    target = _text("target", "□ 재현 시험 내용 - 동작 조건 : ...")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[art1, image, caption, real_heading, target],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id)
            for n in (image, caption, real_heading, target)
        ],
    )

    # image's own candidates=[caption, real_heading, target]: caption
    # (index 0) gets proposed as its CAPTION_OF.
    edge_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=0, edge_type="CAPTION_OF", rationale="이미지 바로 아래의 캡션")]
    )
    # caption/real_heading/target have no pixel/layout crop for docx, so
    # they're batched into one Text-anchor call — answer "no parent" for
    # everyone, since this test only cares about what candidates they see.
    heading_result = _HeadingParentBatchResult(
        choices=[_HeadingParentChoice(anchor_index=i, parent_index=None, rationale="no parent") for i in range(3)]
    )

    class ShapeAwareResponses:
        def __init__(self):
            self.calls: list[dict] = []

        def parse(self, **kwargs):
            self.calls.append(kwargs)
            result = edge_result if kwargs["text_format"] is _EdgeProposalResult else heading_result
            return _FakeResponse(result)

    class ShapeAwareClient:
        def __init__(self):
            self.responses = ShapeAwareResponses()

    client = ShapeAwareClient()
    proposals = propose_edges(doc, client=client, window=3, include_text_anchors=True)

    assert any(p.type == EdgeType.CAPTION_OF and p.source_id == "caption" for p in proposals)

    heading_call = next(c for c in client.responses.calls if c["text_format"] is _HeadingParentBatchResult)
    batch_prompt = heading_call["input"][0]["content"]
    assert batch_prompt.count("스팀 유입 부") == 1, (
        "the caption must appear only in its own anchor line, never again as "
        "another anchor's candidate, once it's been proposed as image's CAPTION_OF"
    )


def test_apply_vlm_enrichment_collapses_enumerated_siblings_into_one_question() -> None:
    """End-to-end reproduction of the real xlsx failure mode, fixed at the
    source rather than patched after the fact: with `include_text_anchors=True`
    (the default), a flat "1) .../2) .../3) ..." run is asked about **once**
    (using "1)"'s own candidate window, the one closest to — and so most
    likely to still reach — the shared heading), and that single answer is
    applied to every member. "2)" and "3)" never become separate questions
    at all, so there's no window to run out for "3)" and nothing to
    disagree with "1)"."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    heading = _text("heading", "■ 창문형에어컨 전도 건 검토")
    item1 = _text("item1", "1) 현상 : ...")
    item2 = _text("item2", "2) 원인 : ...")
    sub = _text("sub", "- 메커니즘 : ...")
    item3 = _text("item3", "3) 창문형에어컨 측면고정 스크류 체결강도 검토")
    doc = ArticDocument(
        source_path="x", format="docx",
        nodes=[art1, heading, item1, item2, sub, item3],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="heading"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="item1"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="item2"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="sub"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="item3"),
        ],
    )

    # "item1"/"item2"/"item3" form one run; "item1" is the representative, so
    # "item2"/"item3" are pruned from the anchor list entirely before any
    # call is made — only heading(0)/item1(1)/sub(2) remain as anchors.
    # item1's own candidates are [heading, item2, sub, item3] (item2/item3
    # still appear as *candidate text*, just not as their own questions) —
    # "heading" sits at index 0.
    heading_result = _HeadingParentBatchResult(
        choices=[
            _HeadingParentChoice(anchor_index=0, parent_index=None, rationale="no parent"),
            _HeadingParentChoice(anchor_index=1, parent_index=0, rationale="under the section heading"),
            _HeadingParentChoice(anchor_index=2, parent_index=None, rationale="not a heading candidate"),
        ]
    )
    client = _FakeClient(heading_result)

    promoted = apply_vlm_enrichment(doc, client=client)

    parent_by_target = {e.target_id: e.source_id for e in doc.edges if e.type == EdgeType.PARENT_OF}
    assert parent_by_target["item1"] == "heading"
    assert parent_by_target["item2"] == "heading", "the whole run inherits item1's answer, never asked separately"
    assert parent_by_target["item3"] == "heading", "including the member whose own window would have missed the heading"
    assert {"item1", "item2", "item3"} <= set(promoted)
    assert len(client.responses.calls) == 1
    assert check_invariants(doc.nodes, doc.edges) == []


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


def test_enumerated_sibling_groups_groups_consecutive_markers_within_one_artifact() -> None:
    """"1)"/"2)"/"3)" with consecutive, increasing-by-1 numbers form one
    group regardless of what non-matching content (a sub-bullet) sits
    between them; a restart back to "1)" under a different heading starts a
    new, separate group instead of extending the first."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    item1 = _text("item1", "1) 현상 : ...")
    item2 = _text("item2", "2) 원인 : ...")
    sub = _text("sub", "- 메커니즘 : ...")
    item3 = _text("item3", "3) 결론 : ...")
    restart1 = _text("restart1", "1) 대책 : ...")
    doc = ArticDocument(
        source_path="x", format="xlsx",
        nodes=[art1, item1, item2, sub, item3, restart1],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id) for n in (item1, item2, sub, item3, restart1)],
    )

    groups = _enumerated_sibling_groups(doc)

    assert [n.id for n in groups[0]] == ["item1", "item2", "item3"]
    assert len(groups) == 1, "a lone restarted '1)' with no '2)' following it forms no group of its own"


def test_enumerated_sibling_groups_ignores_table_data_rows() -> None:
    """Not everything starting with "N)" is an enumerated item — a value
    like "2) 512 512" (a digit right after the marker) must not be mistaken
    for one, the same safeguard `nest_numbered_headings` uses for its own
    pattern."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    item1 = _text("item1", "1) 현상 : ...")
    fake_row = _text("row", "2) 512 512 5.29")  # a digit follows the marker — not an enumerated item
    doc = ArticDocument(
        source_path="x", format="xlsx",
        nodes=[art1, item1, fake_row],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="item1"),
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id="row"),
        ],
    )

    assert _enumerated_sibling_groups(doc) == [], "a single real enumerated item with no matching sibling forms no group"


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



def test_pptx_text_heading_window_reaches_title_across_four_sections() -> None:
    """PBA review slide: page number and four section blocks used to push
    the title outside the last two sections' three-Text reading window.
    A repeated title on another slide must not become a candidate.
    """
    slide = Node(id="slide", type=NodeType.ARTIFACT, name="Slide")
    other_slide = Node(id="other_slide", type=NodeType.ARTIFACT, name="Other")
    specs = [
        ("title", "■ 청소로봇 Main PBA 수분 유입 건", (22, 28, 371, 81)),
        ("page", "2", (14, 32, 49, 90)),
        ("benchmark", "□ 주요 경쟁사 PBA 코팅 BM", (476, 113, 893, 167)),
        ("history", "□ 개발단계 검토 이력", (22, 122, 432, 425)),
        ("coating", "□ 컨포멀코팅 도포 영역", (28, 531, 446, 645)),
        ("plans", "□ 향후 계획", (469, 603, 899, 1077)),
    ]
    texts = [_text(node_id, text) for node_id, text, _ in specs]
    for node, (_, _, box) in zip(texts, specs):
        node.properties.update(slide_index=0, bbox=_bbox(*box))
    other_title = _text("other_title", specs[0][1])
    doc = ArticDocument(
        source_path="x", format="pptx", nodes=[slide, *texts, other_slide, other_title],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="slide", target_id=n.id) for n in texts]
        + [Edge(type=EdgeType.PARENT_OF, source_id="other_slide", target_id="other_title")],
    )
    windows = _build_document_context_windows(
        doc, window=3, anchor_types=(NodeType.TEXT,), boundary_types=(),
    )
    by_anchor = {a.id: [c.id for c in candidates] for a, candidates in windows}
    for section in ("benchmark", "history", "coating", "plans"):
        assert "title" in by_anchor[section]
        assert "other_title" not in by_anchor[section]
        assert section not in by_anchor[section]
    assert by_anchor["plans"][0] != "title"  # proximity still ranks, but never truncates

def test_xlsx_table_heading_window_stays_spatial_even_with_boundary_types_empty() -> None:
    """Regression (found on a real xlsx document in this session): a
    Table/Image anchor's own HEADING_PARENT window (`boundary_types=()`,
    judged together with CAPTION_OF/REFERENCES) must keep using xlsx's
    spatial-distance ranking — only `propose_edges`'s separate Text-anchor
    HEADING_PARENT pass switches to reading-order scanning for
    `boundary_types=()`. Reusing reading-order scanning for a Table/Image
    anchor too let 4 reading-order-adjacent but spatially-distant lines
    (a large column offset) crowd out the table's actual heading — spatially
    close (same column, small row gap) but positioned further back in
    reading order than the window's Text-count budget could reach — and it
    broke a real Table→heading pairing that spatial ranking already got
    right."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="Sheet1", properties={})
    heading = _text("heading", "< Section Heading >")
    heading.properties.update(row=10, col=1)
    fillers = []
    for i, row in enumerate((15, 16, 17, 18), start=1):
        f = _text(f"filler{i}", f"filler line {i}")
        f.properties.update(row=row, col=100)
        fillers.append(f)
    table = Node(
        id="tbl1", type=NodeType.TABLE, name="표",
        properties={"grid": [["a"]], "range": "R20C1:R25C5"},
    )
    doc = ArticDocument(
        source_path="x", format="xlsx",
        nodes=[art1, heading, *fillers, table],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id)
            for n in (heading, *fillers, table)
        ],
    )

    windows = _build_document_context_windows(
        doc, window=3, anchor_types=(NodeType.TABLE, NodeType.IMAGE), boundary_types=()
    )

    assert len(windows) == 1
    candidate_ids = [n.id for n in windows[0][1]]
    assert candidate_ids[0] == "heading", "the spatially nearest candidate (same column, small row gap) must rank first"
    assert "heading" in candidate_ids, (
        "reading-order scanning would have excluded it entirely — the 4 filler lines "
        "(all closer in reading order) would fill the whole window first"
    )


def test_widen_row_sibling_windows_shares_heading_across_same_row_images() -> None:
    """Regression (found on a real xlsx document in this session) —
    reproduces the confirmed row/col/range geometry exactly: 4 photos
    anchored at the exact same row, illustrating one bullet, each have a
    different spatial HEADING_PARENT window purely because of which column
    they sit in. The one sharing its column with two unrelated nearby lines
    (a numbered item two bullets below, and its own elaboration — both
    single-cell, so they don't reach other columns) had those outrank its
    actual section heading entirely — while its 3 row-siblings, sitting far
    enough right to fall outside those single-cell lines' column but still
    inside the *heading*'s and "2) 원인"'s wide merged-cell ranges, had the
    heading rank fine on their own. Once widened with the union of same-row
    siblings' candidates, every member gets a fair shot at the heading, not
    just whichever happens to dodge the distracting column."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="Sheet1", properties={})
    heading = _text("heading", "■ Section Heading")
    heading.properties.update(row=2, col=2, range="R2C2:R2C27")
    item2 = _text("item2", "2) 원인 : ...")
    item2.properties.update(row=6, col=2, range="R6C2:R6C67")
    mechanism = _text("mechanism", "- 메커니즘 : ...")
    mechanism.properties.update(row=8, col=2, range="R8C2:R8C2")
    item3 = _text("item3", "3) 창문형에어컨 측면고정 스크류 체결강도 검토")
    item3.properties.update(row=12, col=2, range="R12C2:R12C2")
    crowded_img = Node(id="img_crowded", type=NodeType.IMAGE, name="Image", properties={"row": 10, "col": 2})
    clean_imgs = [
        Node(id=f"img_clean{i}", type=NodeType.IMAGE, name="Image", properties={"row": 10, "col": col})
        for i, col in enumerate((9, 17, 38), start=1)
    ]
    doc = ArticDocument(
        source_path="x", format="xlsx",
        nodes=[art1, heading, item2, mechanism, item3, crowded_img, *clean_imgs],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id)
            for n in (heading, item2, mechanism, item3, crowded_img, *clean_imgs)
        ],
    )

    wide_windows = _build_document_context_windows(
        doc, window=3, anchor_types=(NodeType.TABLE, NodeType.IMAGE), boundary_types=()
    )
    before = {a.id: {c.id for c in cands} for a, cands in wide_windows}
    assert "heading" not in before["img_crowded"], "sanity check: the crowded image's own window must miss the heading"
    assert all("heading" in before[img.id] for img in clean_imgs), "sanity check: the clean images' own windows must already have it"

    widened = _widen_row_sibling_windows(doc, wide_windows)
    after = {a.id: {c.id for c in cands} for a, cands in widened}

    assert "heading" in after["img_crowded"], "the crowded image must inherit the heading from its row-siblings"
    assert all("heading" in after[img.id] for img in clean_imgs), "the already-fine siblings must keep seeing it too"


def test_widen_row_sibling_windows_includes_table_not_just_image() -> None:
    """A Table shares row-sibling widening with an Image at the same row —
    this project treats Table/Image as one class of anchor everywhere else
    (`_DEFAULT_ANCHOR_TYPES`), and a small summary table next to a couple of
    photos at the same row is a plausible real layout, so the same fix
    shouldn't be Image-only. Same crowded/clean geometry as the Image-only
    test above, except the crowded member is now a Table."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="Sheet1", properties={})
    heading = _text("heading", "■ Section Heading")
    heading.properties.update(row=2, col=2, range="R2C2:R2C27")
    item2 = _text("item2", "2) 원인 : ...")
    item2.properties.update(row=6, col=2, range="R6C2:R6C67")
    mechanism = _text("mechanism", "- 메커니즘 : ...")
    mechanism.properties.update(row=8, col=2, range="R8C2:R8C2")
    item3 = _text("item3", "3) 창문형에어컨 측면고정 스크류 체결강도 검토")
    item3.properties.update(row=12, col=2, range="R12C2:R12C2")
    crowded_table = Node(id="tbl_crowded", type=NodeType.TABLE, name="표", properties={"grid": [["a"]], "row": 10, "col": 2})
    clean_img = Node(id="img_clean", type=NodeType.IMAGE, name="Image", properties={"row": 10, "col": 38})
    doc = ArticDocument(
        source_path="x", format="xlsx",
        nodes=[art1, heading, item2, mechanism, item3, crowded_table, clean_img],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id)
            for n in (heading, item2, mechanism, item3, crowded_table, clean_img)
        ],
    )

    wide_windows = _build_document_context_windows(
        doc, window=3, anchor_types=(NodeType.TABLE, NodeType.IMAGE), boundary_types=()
    )
    before = {a.id: {c.id for c in cands} for a, cands in wide_windows}
    assert "heading" not in before["tbl_crowded"], "sanity check: the crowded table's own window must miss the heading"

    widened = _widen_row_sibling_windows(doc, wide_windows)
    after = {a.id: {c.id for c in cands} for a, cands in widened}

    assert "heading" in after["tbl_crowded"], "the crowded table must inherit the heading from its row-sibling image"


def test_open_heading_marker_families_recognizes_safe_markers() -> None:
    """Each recognized "safe" structural-marker family (see
    `_widen_with_open_headings`'s module-level comment), plus the negative
    cases they're deliberately narrow to exclude."""
    assert _open_heading_marker_families("1. 국민안전: 어디서나, 안전한 일상을 보장받는 국민") == ("numbered_section",)
    assert _open_heading_marker_families("3 Model Architecture") == ("numbered_section",)
    assert _open_heading_marker_families("① 재난상황을 빈틈없이 관리") == ("circled_number",)
    assert _open_heading_marker_families("< 핵심 정책과제 >") == ("bracket_label",)
    # not the *entire* text wrapped — a body sentence merely containing angle
    # brackets somewhere isn't a page-local label.
    assert _open_heading_marker_families("설명(<참고> 링크 포함) 이어지는 문장") == ()
    # a real xlsx cell (this session, WindowFit review doc): several "N. "s
    # concatenated in one long cell, not a heading — the length cap rejects it.
    long_cell = (
        "1. 사용 전동 드라이버 / : Bosch BSB 12-2 Professional (1~22단) / 2. 측정 Torque 게이지 / "
        ": 블루텍 DG-TD230 / 3. 측정 결과 / → 전동드라이버 최고 Torque 22단(약 42kgf·㎝)에서도 파손 없음"
    )
    assert len(long_cell) > _OPEN_HEADING_MAX_LEN
    assert _open_heading_marker_families(long_cell) == ()
    # a table data row starting with a digit-then-digit must not qualify either.
    assert _open_heading_marker_families("1 512 512 5.29 24.9") == ()


def test_build_document_context_windows_widens_pdf_anchor_with_distant_open_heading() -> None:
    """Regression (found on a real PDF this session, a 2025 government press
    release): a subsection several paragraphs into a long section correctly
    finds its immediate local parent, but that parent's *own* true ancestor
    heading sits further back than any reasonably-sized window reaches —
    reproduced here with `window=3` and 5 filler paragraphs in between."""
    art1 = Node(id="art1", type=NodeType.ARTIFACT, name="art1", properties={})
    numbered_section = _text("h1", "1. 국민안전: 어디서나, 안전한 일상을 보장받는 국민")
    fillers = [_text(f"filler{i}", f"filler paragraph {i}") for i in range(5)]
    bracket_anchor = _text("bracket", "< 핵심 정책과제 >")
    content = [numbered_section, *fillers, bracket_anchor]
    doc = ArticDocument(
        source_path="x", format="pdf",
        nodes=[art1, *content],
        edges=[Edge(type=EdgeType.PARENT_OF, source_id="art1", target_id=n.id) for n in content],
    )

    windows = _build_document_context_windows(
        doc, window=3, anchor_types=(NodeType.TEXT,), boundary_types=()
    )
    candidates_by_anchor = {a.id: {c.id for c in cands} for a, cands in windows}

    assert "h1" in candidates_by_anchor["bracket"], (
        "the distant numbered-section heading must be offered as a candidate even though "
        "it's well outside the bounded window"
    )


def test_widen_with_open_headings_does_not_offer_bracket_label_to_a_numbered_section_anchor() -> None:
    """Regression (found via a real end-to-end run on the same PDF as the
    test above, after the widening fix landed): a *later* top-level numbered
    section ("2. Title B") was offered an *earlier* section's own
    "< Label >" (the exact same page-local label text repeats once per
    section) as a bonus HEADING_PARENT candidate, and the VLM wrongly nested
    the later section under it instead of leaving both as Artifact-level
    siblings — a top-level numbered section is the root of its own subtree,
    never a page-local label's child. `numbered_section` bonuses (for a
    genuinely nested "1.1"-style case) are still offered; only
    `circled_number`/`bracket_label` are suppressed for this anchor kind."""
    section1 = _text("s1", "1 Title A")
    bracket1 = _text("b1", "< Label >")
    fillers = [_text(f"filler{i}", f"filler paragraph {i}") for i in range(5)]
    section2 = _text("s2", "2 Title B")
    content = [section1, bracket1, *fillers, section2]

    windows = build_context_windows(content, window=3, anchor_types=(NodeType.TEXT,), boundary_types=())
    widened = _widen_with_open_headings(content, windows)
    candidates_by_anchor = {a.id: {c.id for c in cands} for a, cands in widened}

    assert "b1" not in candidates_by_anchor["s2"], (
        "a numbered-section anchor must not be offered a distant, different-section "
        "bracket-label as a HEADING_PARENT candidate"
    )


def test_widen_with_confirmed_headings_does_not_offer_bonus_to_a_numbered_section_anchor() -> None:
    """The same guard as `_widen_with_open_headings`'s, for pass 2. Regression
    (found via a real end-to-end run on the same PDF, after pass 2 landed):
    a top-level numbered section ("1. 국민안전...") was left flat by pass 1
    (correctly — it's a numbered_section anchor, so it never gets an
    out-of-family bonus there), but pass 2 had no such restriction and
    offered it a confirmed heading from *outside* its own family
    ("< 2025년 주요 추진과제 >", confirmed only because pass 1 attached an
    unrelated Image caption under it) — the VLM accepted it, nesting one
    top-level section under a page-local label while its numbered siblings
    correctly stayed root siblings, an inconsistent tree."""
    overview_label = _text("overview", "Overview Label")
    fillers = [_text(f"filler{i}", f"filler paragraph {i}") for i in range(5)]
    numbered_anchor = _text("s1", "1 Title A")
    content = [overview_label, *fillers, numbered_anchor]

    windows = build_context_windows(content, window=3, anchor_types=(NodeType.TEXT,), boundary_types=())
    widened = _widen_with_confirmed_headings(content, windows, confirmed_heading_ids={"overview"})
    candidates_by_anchor = {a.id: {c.id for c in cands} for a, cands in widened}

    assert "overview" not in candidates_by_anchor["s1"], (
        "a numbered-section anchor must not be offered a confirmed heading from "
        "outside its own family as a HEADING_PARENT candidate"
    )


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


def test_propose_edges_attaches_xlsx_layout_crop_for_heading(tmp_path: Path) -> None:
    """XLSX HEADING_PARENT judgment must also receive the same real sheet
    crop as CAPTION_OF/REFERENCES judgment — they're judged in the same
    call now, via `_EdgeProposalResult`."""
    xlsx_path = build_xlsx_with_table_caption(tmp_path / "heading.xlsx")
    doc = xlsx_extractor.extract(xlsx_path, capture_dir=tmp_path / "captures")
    fake_result = _EdgeProposalResult(
        proposals=[_EdgeProposal(context_index=0, edge_type="HEADING_PARENT", rationale="title directly above the table")]
    )
    client = _FakeClient(fake_result)

    propose_edges(doc, client=client, max_workers=1)

    assert _reparented_ids(doc)
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


def test_heading_failure_is_reported_without_discarding_other_anchors(caplog) -> None:
    """An API failure must be distinguishable from a valid no-parent choice,
    while successful siblings still receive their proposed heading.
    """
    import logging
    title = _text("title", "Main title")
    broken = _text("broken", "Broken section")
    good = _text("good", "Good section")
    slide = Node(id="slide", type=NodeType.ARTIFACT, name="Slide")
    doc = ArticDocument(source_path="x", format="pptx", nodes=[slide, title, broken, good], edges=[
        Edge(type=EdgeType.PARENT_OF, source_id="slide", target_id=n.id)
        for n in (title, broken, good)
    ])
    class Responses:
        def parse(self, **kwargs):
            prompt = kwargs["input"][0]["content"][0]["text"].split("Candidate nodes")[0]
            if "Broken section" in prompt:
                raise RuntimeError("private error details")
            return _FakeResponse(_HeadingParentChoice(
                parent_index=0 if "Good section" in prompt else None,
                rationale="Main title owns this section" if "Good section" in prompt else "No parent",
            ))
    class Client:
        responses = Responses()
    with caplog.at_level(logging.WARNING):
        reparents = _propose_text_heading_parents(
            doc, Client(), "test", 3, layout_crop_fn=lambda a, cs: b"png",
            batch_size=25, max_workers=1,
        )
    assert [(a.id, h.id) for a, h, _ in reparents] == [("good", "title")]
    assert "Heading judgment failed for broken (RuntimeError)" in caplog.text
    assert "private error details" not in caplog.text


def test_propose_text_heading_parents_excludes_known_caption_candidates() -> None:
    """A Text already judged to caption/reference a Table/Image must not
    also be offered as some *other* anchor's HEADING_PARENT candidate —
    confirmed on a real pptx document: a short image caption with no
    recognizable label prefix (so undetected by the deterministic
    heuristics) got mistakenly picked as an unrelated Text's section
    heading, purely because nothing had disqualified it as a candidate."""
    slide = Node(id="slide", type=NodeType.ARTIFACT, name="Slide")
    caption = _text("caption", "스팀 유입 부")
    real_heading = _text("real_heading", "■ 재현 시험 결과")
    target = _text("target", "□ 재현 시험 내용 - 동작 조건 : ...")
    doc = ArticDocument(
        source_path="x", format="pptx",
        nodes=[slide, caption, real_heading, target],
        edges=[
            Edge(type=EdgeType.PARENT_OF, source_id="slide", target_id=n.id)
            for n in (caption, real_heading, target)
        ],
    )

    seen_prompts: list[str] = []

    class Responses:
        def parse(self, **kwargs):
            content = kwargs["input"][0]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
            seen_prompts.append(prompt)
            return _FakeResponse(_HeadingParentChoice(parent_index=0, rationale="the only real heading left"))

    class Client:
        responses = Responses()

    reparents = _propose_text_heading_parents(
        doc, Client(), "test", 3, layout_crop_fn=lambda a, cs: b"png",
        batch_size=25, max_workers=1, excluded_candidate_ids={"caption"},
    )

    target_prompt = next(p for p in seen_prompts if "재현 시험 내용" in p.split("Candidate nodes")[0])
    assert "스팀 유입 부" not in target_prompt, "the caption must be dropped from the candidate list entirely, not just deprioritized"
    assert "재현 시험 결과" in target_prompt, "the real heading must still be offered"

    target_reparent = next((h for a, h, _ in reparents if a.id == "target"), None)
    assert target_reparent is not None and target_reparent.id == "real_heading"
