"""External slide evidence must preserve pixels and drive graph mutations."""
from io import BytesIO
from pathlib import Path
import base64
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.make_fixtures import build_pptx
from test_propose import _FakeClient
from articling.extractors.pptx import extract
from articling.relations.slide_images import attach_pptx_slide_images
from articling.relations.propose import (
    _build_layout_crop_renderer, _SiblingRegionBatchResult,
    _SiblingRegionDecision, _SyntheticGroupCandidate, propose_synthetic_groups,
)
from articling.schema import NodeType, EdgeType
from articling.scaffold import check_invariants


def setup_deck(tmp_path):
    doc = extract(build_pptx(tmp_path / 'deck.pptx'), capture_dir=tmp_path / 'captures')
    paths = {}
    for n in doc.nodes:
        if n.type == NodeType.ARTIFACT:
            p = tmp_path / f"slide-{n.properties['slide_index']}.png"
            w = 800
            h = round(w * n.properties['slide_height'] / n.properties['slide_width'])
            Image.new('RGB', (w, h), '#123456').save(p)
            paths[n.properties['slide_index']] = p
    return doc, paths


def test_external_pixels_reach_group_decision_and_reparent(tmp_path):
    doc, paths = setup_deck(tmp_path)
    attach_pptx_slide_images(doc, paths)
    client = _FakeClient(_SiblingRegionBatchResult(decisions=[
        _SiblingRegionDecision(region_index=0, groups=[_SyntheticGroupCandidate(
            member_indices=[0, 1], group_type='panel', confidence=0.95,
            basis=['spatial', 'visual'], rationale='Shared panel',
        )])
    ]))
    created = propose_synthetic_groups(doc, client=client, max_workers=1)
    assert len(created) == 1
    assert sum(e.source_id == created[0] and e.type == EdgeType.PARENT_OF for e in doc.edges) == 2
    assert check_invariants(doc.nodes, doc.edges) == []
    content = client.responses.calls[0]['input'][0]['content']
    block = next(b for b in content if b['type'] == 'input_image')
    with Image.open(BytesIO(base64.b64decode(block['image_url'].split(',')[1]))) as evidence:
        assert evidence.getpixel((400, 100)) == (18, 52, 86)
    assert any('visual_id=' in b.get('text', '') for b in content)
    propose_synthetic_groups(doc, client=client, max_workers=1)
    assert len({n.id for n in doc.nodes}) == len(doc.nodes)
    assert check_invariants(doc.nodes, doc.edges) == []


def test_mapping_validation_is_atomic(tmp_path):
    doc, paths = setup_deck(tmp_path)
    with pytest.raises(ValueError, match='indices'):
        attach_pptx_slide_images(doc, {})
    last = max(paths)
    Image.new('RGB', (100, 100)).save(paths[last])
    with pytest.raises(ValueError, match='aspect ratio'):
        attach_pptx_slide_images(doc, paths)
    assert not any('slide_image_path' in n.properties for n in doc.nodes)


def test_missing_attached_image_falls_back_without_aborting(tmp_path):
    doc, paths = setup_deck(tmp_path)
    attach_pptx_slide_images(doc, paths)
    anchor = next(n for n in doc.content_nodes() if 'bbox' in n.properties)
    paths[anchor.properties['slide_index']].unlink()
    render, close = _build_layout_crop_renderer(doc)
    try:
        with pytest.warns(UserWarning, match='Cannot read slide'):
            assert render(anchor, []) is not None
    finally:
        close()


def test_enrichment_routes_external_evidence_to_all_three_stages(tmp_path):
    from types import SimpleNamespace
    from articling.relations.propose import apply_vlm_enrichment, _HeadingParentChoice, _EdgeProposalResult

    doc, paths = setup_deck(tmp_path)
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        schema = kwargs['text_format']
        if schema is _HeadingParentChoice:
            result = schema(candidate_index=None, rationale='No heading')
        elif schema is _EdgeProposalResult:
            result = schema(proposals=[])
        elif schema is _SiblingRegionBatchResult:
            result = schema(decisions=[])
        else:
            raise AssertionError(f'Unexpected stage: {schema}')
        return SimpleNamespace(output_parsed=result)

    apply_vlm_enrichment(doc, client=SimpleNamespace(responses=SimpleNamespace(parse=parse)), slide_images=paths)
    assert {c['text_format'] for c in calls} == {_HeadingParentChoice, _EdgeProposalResult, _SiblingRegionBatchResult}
    for call in calls:
        blocks = call['input'][0]['content']
        images = [b for b in blocks if b['type'] == 'input_image']
        assert images
        evidence = Image.open(BytesIO(base64.b64decode(images[-1]['image_url'].split(',')[1])))
        assert evidence.getpixel((400, 100)) == (18, 52, 86)
    assert check_invariants(doc.nodes, doc.edges) == []


def test_vector_ole_preview_is_not_sent_as_an_api_image(tmp_path):
    from articling.relations.propose import _anchor_pixel_path
    from articling.schema import Node
    path = tmp_path / 'preview.wmf'
    path.write_bytes(b'vector preview')
    node = Node(id='ole', type=NodeType.IMAGE, name='OLE', properties={'image_path': str(path)})
    assert _anchor_pixel_path(node) is None



def test_native_reconstruction_preserves_clean_pixels_and_caches_slide(tmp_path, monkeypatch):
    from articling.capture.pptx_capture import PptxSlideRenderer
    doc, _ = setup_deck(tmp_path)
    anchor = next(n for n in doc.content_nodes() if 'bbox' in n.properties)
    expected = Image.open(BytesIO(PptxSlideRenderer(doc.source_path).render(0).png))
    calls = []
    original = PptxSlideRenderer._draw_shape
    def draw(self, *args):
        calls.append(1)
        return original(self, *args)
    monkeypatch.setattr(PptxSlideRenderer, '_draw_shape', draw)
    render, close = _build_layout_crop_renderer(doc)
    try:
        evidence = Image.open(BytesIO(render(anchor, list(doc.content_nodes()))))
        assert evidence.crop((0, 24, expected.width, expected.height + 24)).tobytes() == expected.tobytes()
        first_count = len(calls)
        assert first_count > 0
        render(anchor, [])
        assert len(calls) == first_count
    finally:
        close()


def test_missing_source_and_slide_images_fall_back_to_no_layout(tmp_path):
    doc, _ = setup_deck(tmp_path)
    Path(doc.source_path).unlink()
    anchor = next(n for n in doc.content_nodes() if 'bbox' in n.properties)
    render, close = _build_layout_crop_renderer(doc)
    try:
        with pytest.warns(UserWarning, match='Cannot reconstruct PPTX'):
            assert render(anchor, []) is None
        assert render(anchor, []) is None
    finally:
        close()
