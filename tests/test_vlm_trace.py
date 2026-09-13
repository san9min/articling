"""Local evidence must preserve what was sent without changing API behavior."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from articling.relations.propose import (
    _EdgeProposal, _EdgeProposalResult, _HeadingParentChoice, _HeadingParentBatchResult,
    _apply_heading_reparents, _propose_heading_parent, _propose_heading_parents_batch, apply_vlm_enrichment,
)
from articling.relations.trace import trace_session
from articling.scaffold import check_invariants
from articling.schema import ArticDocument, Node, NodeType
from test_hierarchy_safety import document, text


def test_trace_records_images_mapping_and_applied_graph_without_changing_request(tmp_path: Path):
    png = tmp_path / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    heading, caption = text("heading", "Overview"), text("caption", "Panel")
    img = Node(id="image", type=NodeType.IMAGE, name="Panel", properties={"image_path": str(png)})
    doc = document([heading, img, caption])
    doc.format = "docx"
    requests = []

    def parse(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(output_parsed=_EdgeProposalResult(proposals=[
            _EdgeProposal(context_index=0, edge_type="HEADING_PARENT", rationale="Section owner"),
            _EdgeProposal(context_index=1, edge_type="CAPTION_OF", rationale="Panel label"),
        ]))

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse), api_key="must-not-be-recorded")
    promoted = apply_vlm_enrichment(doc, client=client, include_text_anchors=False, trace_dir=tmp_path / "trace")
    assert promoted == ["image"]
    run = next((tmp_path / "trace").iterdir())
    call_path = next(run.glob("calls/*/call.json"))
    call = json.loads(call_path.read_text())
    assert call["anchors"] == [{"anchor_id": "image", "candidate_ids": ["heading", "caption"]}]
    assert call["request"]["input"] == requests[0]["input"]
    assert call["request"]["instructions"] == requests[0]["instructions"]
    assert call["parsed"]["proposals"][0]["context_index"] == 0
    assert next(call_path.parent.glob("image-*.png")).read_bytes() == png.read_bytes()
    assert json.loads((run / "heading-outcomes/0.json").read_text())["outcome"] == "applied"
    after = ArticDocument.model_validate_json((run / "after.json").read_text())
    assert after == doc
    assert check_invariants(after.nodes, after.edges) == []
    assert "must-not-be-recorded" not in "".join(p.read_text() for p in run.rglob("*.json"))
    assert requests[0]["text_format"] is _EdgeProposalResult
    assert "_anchors" not in requests[0]


def test_concurrent_trace_keeps_null_errors_and_rejected_choices(tmp_path: Path):
    heading = text("h", "Section")
    anchors = [text(value, value) for value in ("empty", "error", "ok")]
    doc = document([heading, *anchors], [text("remote", "Other slide")])

    def parse(**kwargs):
        prompt = kwargs["input"][0]["content"]
        if ": error\n" in prompt:
            raise RuntimeError("secret exception payload")
        parsed = None if ": empty\n" in prompt else _HeadingParentChoice(parent_index=0, rationale="Owner")
        return SimpleNamespace(output_parsed=parsed)

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    with trace_session(client, tmp_path, doc) as traced:
        def judge(anchor):
            try:
                return _propose_heading_parent(traced, "fake", anchor, [heading])
            except RuntimeError:
                return "error"
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(judge, anchors))
        assert results == [None, "error", (0, "Owner")]
        remote = next(n for n in doc.nodes if n.id == "remote")
        assert _apply_heading_reparents(doc, "fake", [(anchors[0], remote, "invalid")], trace=traced) == []
    run = next(tmp_path.iterdir())
    calls = [json.loads(p.read_text()) for p in run.glob("calls/*/call.json")]
    assert {call["status"] for call in calls} == {"null", "error", "parsed"}
    assert {call["anchors"][0]["anchor_id"] for call in calls} == {"empty", "error", "ok"}
    error = next(call for call in calls if call["status"] == "error")
    assert error["error_type"] == "RuntimeError"
    assert "secret exception payload" not in json.dumps(calls)
    assert json.loads((run / "heading-outcomes/0.json").read_text())["outcome"] == "artifact_boundary"


def test_trace_write_failure_does_not_discard_model_answer(tmp_path: Path, monkeypatch):
    doc = document([text("h", "Section"), text("b", "Body")])
    response = SimpleNamespace(output_parsed=_HeadingParentChoice(parent_index=0, rationale="Owner"))
    client = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: response))
    with trace_session(client, tmp_path, doc) as traced:
        def fail(*args, **kwargs):
            raise OSError("disk unavailable")
        monkeypatch.setattr(Path, "write_text", fail)
        assert _propose_heading_parent(traced, "fake", doc.nodes[2], [doc.nodes[1]]) == (0, "Owner")


def test_batch_trace_preserves_independent_candidate_index_spaces(tmp_path: Path):
    first, second, body = text("first", "First"), text("second", "Second"), text("body", "Body")
    doc = document([first, second, body])
    choices = [
        _HeadingParentChoice(anchor_index=0, parent_index=None, rationale="No owner"),
        _HeadingParentChoice(anchor_index=1, parent_index=1, rationale="First owns body"),
    ]
    client = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: SimpleNamespace(
        output_parsed=_HeadingParentBatchResult(choices=choices),
    )))
    with trace_session(client, tmp_path, doc) as traced:
        assert _propose_heading_parents_batch(traced, "fake", [(first, [second]), (body, [second, first])]) == choices
    call = json.loads(next(tmp_path.glob("run-*/calls/*/call.json")).read_text())
    assert call["anchors"] == [
        {"anchor_id": "first", "candidate_ids": ["second"]},
        {"anchor_id": "body", "candidate_ids": ["second", "first"]},
    ]
    choice = call["parsed"]["choices"][1]
    assert call["anchors"][choice["anchor_index"]]["candidate_ids"][choice["parent_index"]] == "first"
