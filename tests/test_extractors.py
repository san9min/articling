from __future__ import annotations

import io
import sys
import types
import zipfile
from pathlib import Path

import openpyxl
import pymupdf

sys.path.insert(0, str(Path(__file__).parent))


def test_pdf_outline_matches_headings_not_mentions_or_diagram_labels(tmp_path: Path) -> None:
    from articling.relations.propose import _apply_heading_reparents, _build_document_context_windows

    path = tmp_path / "outline.pdf"
    with pymupdf.open() as source:
        page = source.new_page()
        for y, text in [(50, "This text mentions Attention."), (100, "Attention"),
                        (200, "1 Architecture"), (300, "1.1 Attention"),
                        (400, "References"), (440, "Unlisted material")]:
            page.insert_text((72, y), text)
        source.set_toc([[1, "Architecture", 1], [2, "Attention", 1, 300]])
        source.save(path)
    doc = pdf_extractor.extract(path, capture_dir=tmp_path / "captures")
    texts = {n.properties["text"]: n for n in doc.nodes if n.type == NodeType.TEXT}
    artifact = next(n for n in doc.nodes if n.type == NodeType.ARTIFACT)
    parents = {e.target_id: e.source_id for e in doc.edges if e.type == EdgeType.PARENT_OF}
    assert parents[texts["1.1 Attention"].id] == texts["1 Architecture"].id
    assert "outline_level" not in texts["Attention"].properties
    assert "outline_level" not in texts["This text mentions Attention."].properties
    assert parents[texts["References"].id] == artifact.id
    assert parents[texts["Unlisted material"].id] == artifact.id
    assert _apply_heading_reparents(doc, "test", [(texts["1.1 Attention"], texts["Attention"], "wrong")]) == []
    windows = _build_document_context_windows(doc, window=3, anchor_types=(NodeType.TEXT,), boundary_types=())
    assert texts["1.1 Attention"].id not in {n.id for n, _ in windows}
    assert check_invariants(doc.nodes, doc.edges) == []

from fixtures.make_fixtures import (  # noqa: E402
    build_docx,
    build_docx_with_table_cell_image,
    build_pdf,
    build_pdf_out_of_stream_order,
    build_pdf_with_figure_caption,
    build_pdf_with_figure_reference,
    build_pptx,
    build_pptx_with_box_over_image,
    build_pptx_with_chart,
    build_pptx_with_ole,
    build_pptx_with_shaded_table,
    build_pptx_with_smartart,
    build_xlsx,
    build_xlsx_with_native_table,
    build_xlsx_with_overlapping_standalone_images,
    build_xlsx_with_table_caption_and_reference,
)
from lxml import etree  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from articling.extractors import docx as docx_extractor  # noqa: E402
from articling.extractors import pdf as pdf_extractor  # noqa: E402
from articling.extractors import pptx as pptx_extractor  # noqa: E402
from articling.extractors import xlsx as xlsx_extractor  # noqa: E402
from articling.extractors.docx import _paragraph_has_image  # noqa: E402
from articling.extractors.pdf import _reorder_same_line_blocks  # noqa: E402
from articling.scaffold import check_invariants  # noqa: E402
from articling.schema import EdgeType, NodeType  # noqa: E402

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _types(doc):
    return {n.type for n in doc.nodes}


def _fake_paragraph(inner_xml: str):
    """`_paragraph_has_image` only looks at `p._p` (an lxml element), so a
    fake object with just that attribute is enough — no real python-docx
    Document needed."""
    return types.SimpleNamespace(_p=etree.fromstring(inner_xml))


def test_paragraph_has_image_ignores_vector_shapes_without_blip() -> None:
    """Regression test — judging by w:drawing alone would misclassify an
    arrow/textbox (wsp, no a:blip) as an Image too. Should be True only
    when a:blip is present."""
    vector_shape_only = (
        f'<w:p xmlns:w="{_W_NS}"><w:r><w:drawing>'
        f'<wps:wsp xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"/>'
        f"</w:drawing></w:r></w:p>"
    )
    assert _paragraph_has_image(_fake_paragraph(vector_shape_only)) is False

    real_picture = (
        f'<w:p xmlns:w="{_W_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}"><w:r><w:drawing>'
        f'<a:blip r:embed="rId1"/>'
        f"</w:drawing></w:r></w:p>"
    )
    assert _paragraph_has_image(_fake_paragraph(real_picture)) is True


def test_docx_extract(tmp_path: Path) -> None:
    path = build_docx(tmp_path / "sample.docx")
    doc = docx_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert _types(doc) >= {NodeType.FILE, NodeType.ARTIFACT, NodeType.TEXT, NodeType.TABLE, NodeType.IMAGE}
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert len(tables) == 1
    assert tables[0].properties["grid"] == [["항목", "값"], ["토크", "3.4 kgf"]]
    assert check_invariants(doc.nodes, doc.edges) == []

    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1
    image_path = Path(images[0].properties["image_path"])
    assert image_path.exists(), "the inline image original must actually be saved"


def test_docx_absorbs_table_cell_image_as_metadata(tmp_path: Path) -> None:
    """A table-cell image must not become its own Image node — "Option A:
    absorb" (the same rule xlsx.py applies to images inside a table range)
    keeps it as `embedded_images`/`embedded_image_count` metadata on the
    Table instead, so it can't be misplaced in document order (an older bug
    this fixture used to regress against: a nested node pushed to the end of
    `content_nodes` would wrongly hand `build_context_windows` the
    document's last paragraph as its context)."""
    path = build_docx_with_table_cell_image(tmp_path / "cell_image.docx")
    doc = docx_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert check_invariants(doc.nodes, doc.edges) == []
    assert not [n for n in doc.nodes if n.type == NodeType.IMAGE], "a table-cell image must not get its own node"

    table = next(n for n in doc.nodes if n.type == NodeType.TABLE)
    assert table.properties["embedded_image_count"] == 1
    [entry] = table.properties["embedded_images"]
    assert entry["table_row"] == 0 and entry["table_col"] == 0
    assert Path(entry["image_path"]).exists(), "the cell image's original bytes must still be saved"


def test_docx_uses_native_heading_outline_for_parent_edges(tmp_path: Path) -> None:
    """Word heading levels provide a deterministic hierarchy, so body text
    and nested headings should not depend on a small VLM context window."""
    from docx import Document

    path = tmp_path / "outline.docx"
    source = Document()
    source.add_heading("Section", level=1)
    source.add_paragraph("Section body")
    source.add_heading("Subsection", level=2)
    source.add_paragraph("Subsection body")
    source.save(path)

    doc = docx_extractor.extract(path, capture_dir=tmp_path / "captures")
    by_text = {
        n.properties.get("text"): n for n in doc.nodes if n.type == NodeType.TEXT
    }
    parents = {
        e.target_id: e.source_id
        for e in doc.edges
        if e.type == EdgeType.PARENT_OF
    }
    assert by_text["Section"].properties["outline_level"] == 0
    assert by_text["Subsection"].properties["outline_level"] == 1
    assert parents[by_text["Section body"].id] == by_text["Section"].id
    assert parents[by_text["Subsection"].id] == by_text["Section"].id
    assert parents[by_text["Subsection body"].id] == by_text["Subsection"].id
    assert check_invariants(doc.nodes, doc.edges) == []


def test_docx_preserves_automatic_list_identity(tmp_path: Path) -> None:
    """Automatic numbering lives in w:numPr, not paragraph text."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    path = tmp_path / "numbered.docx"
    source = Document()
    paragraphs = [source.add_paragraph(text) for text in ("first", "second", "third")]
    for paragraph in paragraphs:
        ppr = paragraph._p.get_or_add_pPr()
        num_pr = OxmlElement("w:numPr")
        ilvl = OxmlElement("w:ilvl"); ilvl.set(qn("w:val"), "0")
        num_id = OxmlElement("w:numId"); num_id.set(qn("w:val"), "7")
        num_pr.extend((ilvl, num_id)); ppr.append(num_pr)
    source.save(path)
    doc = docx_extractor.extract(path, capture_dir=tmp_path / "captures")
    texts = [n for n in doc.nodes if n.type == NodeType.TEXT]
    assert [(n.properties["list_num_id"], n.properties["list_level"]) for n in texts] == [(7, 0)] * 3


def test_pptx_extract(tmp_path: Path) -> None:
    path = build_pptx(tmp_path / "sample.pptx")
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert _types(doc) >= {NodeType.FILE, NodeType.ARTIFACT, NodeType.TEXT, NodeType.TABLE, NodeType.IMAGE}
    assert doc.nodes[1].type == NodeType.ARTIFACT
    assert check_invariants(doc.nodes, doc.edges) == []

    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1
    image_path = Path(images[0].properties["image_path"])
    assert image_path.exists(), "the picture shape original must actually be saved"
    for node in doc.content_nodes():
        assert node.properties["slide_index"] == 0
        assert set(node.properties["bbox"]) == {"x_min", "y_min", "x_max", "y_max"}
        assert node.properties["width"] > 0 and node.properties["height"] > 0


def test_pptx_ole_object_not_dropped(tmp_path: Path) -> None:
    path = build_pptx_with_ole(tmp_path / "ole.pptx")
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / "captures")

    ole_nodes = [n for n in doc.nodes if n.properties.get("embed_kind") == "ole_object"]
    assert len(ole_nodes) == 1, "an embedded OLE object shape must not be silently dropped"
    node = ole_nodes[0]
    assert node.type == NodeType.IMAGE
    assert node.properties["ole_prog_id"] == "Excel.Sheet.12"
    image_path = Path(node.properties["image_path"])
    assert image_path.exists(), "the OLE object's fallback preview picture must be saved"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_pptx_box_drawn_on_picture_gets_composited(tmp_path: Path) -> None:
    path = build_pptx_with_box_over_image(tmp_path / "boxed.pptx")
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert check_invariants(doc.nodes, doc.edges) == []
    assert not [n for n in doc.nodes if n.type == NodeType.TEXT], "a textless box shape must not become its own node"

    [image] = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert image.properties["annotation_shape_count"] == 1
    image_path = Path(image.properties["image_path"])
    assert image_path.exists()

    with PILImage.open(image_path) as composited:
        w, h = composited.size
        # The red box sits at (2in, 1.5in) within the picture's (1in, 1in)-(5in, 4in) box —
        # i.e. 25%/16.7% into it — well clear of its edges either way.
        assert composited.getpixel((round(w * 0.3), round(h * 0.25))) == (255, 0, 0), \
            "the box's fill must be baked into the picture's saved pixels"
        assert composited.getpixel((round(w * 0.05), round(h * 0.05))) == (0, 120, 200), \
            "pixels outside the box must still be the original photo, not lost in re-rendering"


def test_pptx_chart_not_dropped(tmp_path: Path) -> None:
    path = build_pptx_with_chart(tmp_path / "chart.pptx")
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert check_invariants(doc.nodes, doc.edges) == []
    [chart] = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert chart.properties["chart_type"] == "COLUMN_CLUSTERED"
    assert chart.properties["chart_title"] == "분기별 실적"
    assert chart.name == "분기별 실적"
    grid = chart.properties["grid"]
    assert grid[0] == ["", "매출", "비용"]
    assert grid[1] == ["1분기", "19.2", "10.1"]
    assert grid[3] == ["3분기", "16.7", "9.9"]


def test_pptx_table_gets_visual_capture(tmp_path: Path) -> None:
    path = build_pptx_with_shaded_table(tmp_path / "shaded_table.pptx")
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert check_invariants(doc.nodes, doc.edges) == []
    [table] = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(table.properties["capture_path"])
    assert capture_path.exists()

    with PILImage.open(capture_path) as composited:
        w, h = composited.size
        assert composited.getpixel((round(w * 0.3), round(h * 0.15))) == (255, 0, 0), \
            "the header row's fill color must be baked into the table's captured pixels"
        assert composited.getpixel((round(w * 0.3), round(h * 0.85))) == (255, 255, 255), \
            "a data row with no explicit fill must stay white, not inherit the header's color"


def test_pptx_chart_table_has_no_visual_capture(tmp_path: Path) -> None:
    """A chart-derived Table already carries exact data as its `grid`, and
    the renderer has no chart-drawing support to capture from anyway — it
    must not get a (nonexistent-content) `capture_path`."""
    path = build_pptx_with_chart(tmp_path / "chart.pptx")
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / "captures")

    [chart] = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert "capture_path" not in chart.properties


def test_pptx_smartart_text_not_dropped(tmp_path: Path) -> None:
    path = build_pptx_with_smartart(tmp_path / "smartart.pptx")
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert check_invariants(doc.nodes, doc.edges) == []
    [text_node] = [n for n in doc.nodes if n.type == NodeType.TEXT]
    assert text_node.properties["shape_kind"] == "smartart"
    assert text_node.properties["text"] == "1단계: 준비\n2단계: 조립"
    assert "ROOT_SHOULD_BE_SKIPPED" not in text_node.properties["text"], \
        "the diagram's structural doc/parTrans points must not leak into the recovered text"


def test_xlsx_extract(tmp_path: Path) -> None:
    path = build_xlsx(tmp_path / "sample.xlsx")
    doc = xlsx_extractor.extract(path, capture_dir=tmp_path / "captures")

    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    texts = [n for n in doc.nodes if n.type == NodeType.TEXT]

    assert len(tables) == 1, "border-based table detection must recognize B2:C3 as a table"
    assert tables[0].properties["capture_embedded_image_count"] == 1, "an image inside the table must be absorbed into the capture"
    assert len(images) == 1, "only the image outside the table range should become a separate Image node (Option A)"
    assert any("재측정" in t.properties["text"] for t in texts), "the note physically separate from the table must become its own Text"
    assert check_invariants(doc.nodes, doc.edges) == []

    capture_path = Path(tables[0].properties["capture_path"])
    assert capture_path.exists(), "the table's visual-capture PNG must actually be saved"

    image_path = Path(images[0].properties["image_path"])
    assert image_path.exists(), "the standalone image outside the table range must actually have its original saved"


def test_xlsx_native_table_extract(tmp_path: Path) -> None:
    """Regression test — `ws.tables.items()` (a `TableList`) yields
    `(name, ref_string)` pairs, not `(name, Table)` like a plain dict would;
    `_table_ranges` used to call `.ref` on that string and crash on any real
    workbook using a formal Excel Table (e.g. Microsoft's own Financial
    Sample.xlsx demo workbook)."""
    path = build_xlsx_with_native_table(tmp_path / "sample.xlsx")
    doc = xlsx_extractor.extract(path, capture_dir=tmp_path / "captures")

    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert len(tables) == 1
    assert tables[0].name.startswith("SpecTable")
    assert tables[0].properties["range"] == "R1C1:R3C2"
    assert check_invariants(doc.nodes, doc.edges) == []


def _inject_into_drawing_xml(src_path: Path, dst_path: Path, extra_anchor_xml: str) -> None:
    """openpyxl has no API for writing shapes (`xdr:sp`/`xdr:cxnSp`) — the
    only way to reproduce a situation with a shape a person drew directly
    is to inject an anchor element as a string straight into the saved
    xlsx's drawing XML."""
    with zipfile.ZipFile(src_path) as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}
    drawing_name = next(n for n in entries if n.startswith("xl/drawings/drawing") and n.endswith(".xml"))
    entries[drawing_name] = entries[drawing_name].replace(b"</wsDr>", extra_anchor_xml.encode() + b"</wsDr>")
    with zipfile.ZipFile(dst_path, "w") as zout:
        for name, data in entries.items():
            zout.writestr(name, data)


def _inject_xdr_rect_shape(
    src_path: Path, dst_path: Path, from_rc: tuple[int, int], to_rc: tuple[int, int], hex_color: str = "FF0000"
) -> None:
    """`from_rc`/`to_rc` are 0-based (row, col). An outline-only rectangle `xdr:sp` with no fill."""
    shape_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f"<xdr:from><xdr:col>{from_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{from_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{to_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{to_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="999" name="TestBox"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "<a:noFill/>"
        f'<a:ln w="25400"><a:solidFill><a:srgbClr val="{hex_color}"/></a:solidFill></a:ln>'
        "</xdr:spPr>"
        "</xdr:sp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(src_path, dst_path, shape_xml)


def _inject_xdr_pic_twocell(
    src_path: Path,
    dst_path: Path,
    from_rc: tuple[int, int],
    to_rc: tuple[int, int],
    image_bytes: bytes,
) -> None:
    """Injects a `xdr:twoCellAnchor`/`xdr:pic` (genuinely resized to fit
    `from_rc`..`to_rc`, default `editAs="twoCell"`) — with its own media
    part and relationship, the same mechanics as `_inject_xdr_pic`
    (elsewhere in this file) but a two-cell anchor instead of a one-cell
    one, so the picture's real extent can reach arbitrarily far past a
    table's own declared border (exercising
    `_embedded_image_overflow_bounds`)."""
    with zipfile.ZipFile(src_path) as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}

    media_name = "xl/media/image_overflow_test.png"
    entries[media_name] = image_bytes

    rels_name = "xl/drawings/_rels/drawing1.xml.rels"
    rels_xml = entries[rels_name].decode("utf-8")
    r_id = "rIdOverflowTest"
    new_rel = (
        '<Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        f'Target="../media/image_overflow_test.png" Id="{r_id}"/>'
    )
    entries[rels_name] = rels_xml.replace("</Relationships>", new_rel + "</Relationships>").encode("utf-8")

    pic_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<xdr:from><xdr:col>{from_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{from_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{to_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{to_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        "<xdr:pic>"
        '<xdr:nvPicPr><xdr:cNvPr id="901" name="OverflowTestPic"/><xdr:cNvPicPr/></xdr:nvPicPr>'
        f'<xdr:blipFill><a:blip r:embed="{r_id}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
        '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
        "</xdr:pic>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    drawing_name = next(n for n in entries if n.startswith("xl/drawings/drawing") and n.endswith(".xml"))
    entries[drawing_name] = entries[drawing_name].replace(b"</wsDr>", pic_xml.encode("utf-8") + b"</wsDr>")

    with zipfile.ZipFile(dst_path, "w") as zout:
        for name, data in entries.items():
            zout.writestr(name, data)


def test_xlsx_capture_table_extends_canvas_for_overflowing_twocell_image(tmp_path: Path) -> None:
    """Regression test — an embedded `TwoCellAnchor` image anchored inside a
    table but genuinely reaching several columns past the table's own
    border used to get clipped at the canvas edge: `capture_table_image`'s
    canvas was only ever widened by a fixed `padding` (1 cell), not by
    however far the image's own `to` corner actually reached. Injects a
    wide asymmetric (left-red/right-blue) image anchored from inside the
    table (C2, inside `build_xlsx`'s B2:C3) out to column G — 4 columns
    past the table's own border — and confirms blue (the image's far/right
    side) survives in the capture, proving the canvas widened enough not to
    clip it (see `_embedded_image_overflow_bounds`)."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    overflowed = tmp_path / "sample_with_overflowing_image.xlsx"
    _inject_xdr_pic_twocell(base, overflowed, from_rc=(1, 2), to_rc=(2, 6), image_bytes=_asymmetric_png_bytes())

    doc = xlsx_extractor.extract(overflowed, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert len(tables) == 1
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")
    assert _has_pixel(img, lambda px: px[2] > 150 and px[0] < 100), (
        "the wide image's blue right half must survive uncropped in the table capture"
    )


def test_xlsx_capture_table_extends_canvas_for_overflowing_fixed_size_image(tmp_path: Path) -> None:
    """Regression test — a fixed-size (`editAs="oneCell"`) embedded image
    (confirmed as the anchor type real annotation photos in these documents
    actually use — see the screwdriver-photo flip bug found on the same
    kind of file) has no `to` corner at all; its real width comes only from
    its own EMU `ext`. `build_xlsx`'s sheet has `ws.max_column == 3` (no
    cell value reaches further right), so *without* walking a fixed-size
    anchor's `ext` forward column by column (`_advance_col_by_width`), the
    canvas would be capped at column 3 by `sheet_max_col` regardless of
    `padding` — genuinely clipping (not just squishing) a wide photo via
    PIL's `paste`, unlike the `TwoCellAnchor` case in
    `test_xlsx_capture_table_extends_canvas_for_overflowing_twocell_image`
    (which instead resizes to whatever `to`-based width it computes). Injects
    a wide asymmetric (left-red/right-blue) `oneCell` image anchored at C2
    (inside `build_xlsx`'s B2:C3) with an `ext` several columns wide, and
    confirms blue survives uncropped."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    overflowed = tmp_path / "sample_with_overflowing_fixed_image.xlsx"
    _inject_xdr_pic(
        base, overflowed, from_rc=(1, 2), ext_emu=(3_000_000, 200_000), image_bytes=_asymmetric_png_bytes()
    )

    doc = xlsx_extractor.extract(overflowed, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert len(tables) == 1
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")
    assert _has_pixel(img, lambda px: px[2] > 150 and px[0] < 100), (
        "the wide fixed-size image's blue right half must survive uncropped in the table capture"
    )


def test_xlsx_capture_includes_manually_drawn_shape(tmp_path: Path) -> None:
    """Regression test — openpyxl never parses `xdr:sp` (a shape: e.g. a
    highlight rectangle a person drew directly on top of a photo) at all
    (never caught in `ws._images`; openpyxl itself warns "Shapes and
    drawings will be lost"). Verifies that `capture_table_image` reopens the
    original xlsx and finds the shape directly in the drawing XML to draw
    it."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_shape.xlsx"
    # a red outline rectangle (no fill) covering B2:C3 (the table range, 0-based row1-3/col1-3).
    _inject_xdr_rect_shape(base, shaped, from_rc=(1, 1), to_rc=(3, 3), hex_color="FF0000")

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    assert len(tables) == 1
    capture_path = Path(tables[0].properties["capture_path"])
    assert capture_path.exists()

    img = PILImage.open(capture_path).convert("RGB")
    pixels = img.load()
    width, height = img.size
    found_red = any(
        pixels[x, y][0] > 200 and pixels[x, y][1] < 80 and pixels[x, y][2] < 80
        for y in range(height)
        for x in range(width)
    )
    assert found_red, "the red outline of the person-drawn rectangle shape (xdr:sp) must be drawn in the capture PNG"


def _has_pixel(img: PILImage.Image, predicate) -> bool:
    pixels = img.load()
    width, height = img.size
    return any(predicate(pixels[x, y]) for y in range(height) for x in range(width))


def test_xlsx_capture_rotates_shapes(tmp_path: Path) -> None:
    """Regression test — verifies that a shape is drawn rotated per
    `spPr/xfrm`'s `rot` (in 1/60000ths of a degree). Injects a green filled
    rectangle rotated 45 degrees into B2:C3, and confirms "was it actually
    rotated" by checking that the background color (white) is still intact
    at a spot that wouldn't be if it hadn't rotated (one of the original
    bbox's four corners, a spot the rotated rectangle has turned away
    from)."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_rotated_shape.xlsx"
    shape_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        "<xdr:to><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="1" name="RotBox"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        '<a:xfrm rot="2700000"><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="00FF00"/></a:solidFill>'
        "</xdr:spPr>"
        "</xdr:sp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(base, shaped, shape_xml)

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    assert _has_pixel(img, lambda px: px[1] > 150 and px[0] < 120 and px[2] < 120), (
        "the 45-degree-rotated green rectangle must be drawn"
    )
    # if it were an unrotated (axis-aligned) rectangle, the bbox corner
    # itself would be filled green — after rotation it's diamond-shaped, so
    # the bbox's top-left corner must be left empty (background/white).
    top_left = img.getpixel((0, 0))
    assert top_left == (255, 255, 255), "if it rotated, the original bbox corner must be left as the background color"


def test_xlsx_capture_draws_straight_connector_with_arrowhead(tmp_path: Path) -> None:
    """Regression test — verifies that an `xdr:cxnSp` (a straight connector,
    an arrow annotation) is drawn honoring the line color/width and even the
    arrowhead (`headEnd`)."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_arrow.xlsx"
    arrow_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        "<xdr:to><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:cxnSp macro="">'
        '<xdr:nvCxnSpPr><xdr:cNvPr id="2" name="Arrow"/><xdr:cNvCxnSpPr/></xdr:nvCxnSpPr>'
        "<xdr:spPr>"
        '<a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
        '<a:ln w="19050"><a:solidFill><a:srgbClr val="FF00FF"/></a:solidFill>'
        '<a:tailEnd type="none"/><a:headEnd type="triangle"/>'
        "</a:ln>"
        "</xdr:spPr>"
        "</xdr:cxnSp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(base, shaped, arrow_xml)

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    assert _has_pixel(img, lambda px: px[0] > 180 and px[2] > 180 and px[1] < 100), (
        "the straight connector (arrow)'s magenta line must be drawn in the capture PNG"
    )


def _inject_xdr_connector(
    src_path: Path, dst_path: Path, from_rc: tuple[int, int], to_rc: tuple[int, int], prst: str, hex_color: str
) -> None:
    """`from_rc`/`to_rc` are 0-based (row, col). A `xdr:cxnSp` with the given
    `prstGeom` preset (e.g. `"bentConnector3"`/`"curvedConnector3"`) and no
    arrowheads."""
    conn_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f"<xdr:from><xdr:col>{from_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{from_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{to_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{to_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:cxnSp macro="">'
        '<xdr:nvCxnSpPr><xdr:cNvPr id="3" name="Conn"/><xdr:cNvCxnSpPr/></xdr:nvCxnSpPr>'
        "<xdr:spPr>"
        f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'
        f'<a:ln w="25400"><a:solidFill><a:srgbClr val="{hex_color}"/></a:solidFill></a:ln>'
        "</xdr:spPr>"
        "</xdr:cxnSp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(src_path, dst_path, conn_xml)


def test_xlsx_capture_draws_bent_connector(tmp_path: Path) -> None:
    """Regression test — `bentConnector*` used to be skipped outright (see
    `_bent_connector_points`); verifies it's now drawn as a midpoint elbow,
    i.e. the connector's color shows up away from the diagonal between its
    two bbox corners (proof it actually bent, not just a straight line the
    old skip-check happened to let through)."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_bent_connector.xlsx"
    _inject_xdr_connector(base, shaped, from_rc=(1, 1), to_rc=(5, 5), prst="bentConnector3", hex_color="00FFFF")

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    is_cyan = lambda px: px[1] > 180 and px[2] > 180 and px[0] < 100  # noqa: E731
    assert _has_pixel(img, is_cyan), "the bent connector's cyan line must be drawn in the capture PNG"

    pixels = img.load()
    width, height = img.size
    cyan_pixels = [(x, y) for y in range(height) for x in range(width) if is_cyan(pixels[x, y])]
    top_edge_y = min(y for _x, y in cyan_pixels)
    xs_at_top = [x for x, y in cyan_pixels if y == top_edge_y]
    # A plain diagonal line only grazes any single y row across a span of
    # about one line-width (~3px here); the elbow's horizontal segment spans
    # a much wider run of x at its (near-)constant y — proof a real bend was
    # drawn, not a straight diagonal the old skip-check happened to let
    # through unrecognized.
    assert max(xs_at_top) - min(xs_at_top) > 15, (
        "the near-horizontal segment of a bent connector's elbow must span many pixels of x at one y, "
        "not just the width of a plain diagonal line"
    )


def test_xlsx_capture_draws_curved_connector(tmp_path: Path) -> None:
    """Regression test — `curvedConnector*` used to be skipped outright (see
    `_curved_connector_points`); verifies it's now drawn as a flattened
    Bezier S-curve."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_curved_connector.xlsx"
    _inject_xdr_connector(base, shaped, from_rc=(1, 1), to_rc=(5, 5), prst="curvedConnector3", hex_color="00FFFF")

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    assert _has_pixel(img, lambda px: px[1] > 180 and px[2] > 180 and px[0] < 100), (
        "the curved connector's cyan curve must be drawn in the capture PNG"
    )


def test_xlsx_capture_draws_polygon_preset_shape(tmp_path: Path) -> None:
    """Regression test — a non-rect/ellipse `prstGeom` (e.g. `"diamond"`)
    used to always render as a plain rectangle (see
    `_PRESET_UNIT_POLYGONS`); verifies a filled diamond leaves its own bbox
    corners as background (unlike a rectangle, which would fill them)."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_diamond.xlsx"
    shape_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        "<xdr:to><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="20" name="Diamond"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        '<a:prstGeom prst="diamond"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="00FF00"/></a:solidFill>'
        "</xdr:spPr>"
        "</xdr:sp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(base, shaped, shape_xml)

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    is_green = lambda px: px[1] > 150 and px[0] < 100 and px[2] < 100  # noqa: E731
    assert _has_pixel(img, is_green), "the diamond's green fill must be drawn in the capture PNG"

    pixels = img.load()
    width, height = img.size
    green_pixels = [(x, y) for y in range(height) for x in range(width) if is_green(pixels[x, y])]
    green_xs = [x for x, _y in green_pixels]
    green_ys = [y for _x, y in green_pixels]
    x1, y1, x2, y2 = min(green_xs), min(green_ys), max(green_xs), max(green_ys)
    bbox_area = (x2 - x1 + 1) * (y2 - y1 + 1)
    # A diamond covers exactly half its bounding box's area (in continuous
    # geometry); a rectangle fallback would cover the whole thing. Using a
    # generous 80% threshold (rather than exactly 50%) absorbs rasterization
    # rounding at small pixel sizes while still clearly separating the two.
    assert len(green_pixels) < bbox_area * 0.8, (
        "a diamond must cover meaningfully less than its full bounding box — a rectangle fallback would fill all of it"
    )


def test_xlsx_capture_expands_grouped_shapes(tmp_path: Path) -> None:
    """Regression test — an `xdr:grpSp` that a person "grouped" by selecting
    several shapes used to have its inner shapes not drawn at all, because
    `_load_sheet_shapes` only looked at the top-level anchor's direct
    children (ignoring anything that isn't `sp`/`cxnSp`). Injects two
    side-by-side red/blue rectangles inside a group, and — since they sit in
    the left half/right half respectively of the group's internal
    coordinate system (`chOff`/`chExt`) — verifies that red is also drawn
    to the left of blue in the capture PNG (confirming
    `_expand_group_shapes`'s linear mapping actually preserves relative
    position)."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_group.xlsx"
    group_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        "<xdr:to><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:grpSp>'
        '<xdr:nvGrpSpPr><xdr:cNvPr id="10" name="Group"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr>'
        '<xdr:grpSpPr>'
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="2000000" cy="1000000"/></a:xfrm>'
        "</xdr:grpSpPr>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="11" name="LeftHalf"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="FF0000"/></a:solidFill>'
        "</xdr:spPr>"
        "</xdr:sp>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="12" name="RightHalf"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        '<a:xfrm><a:off x="1000000" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="0000FF"/></a:solidFill>'
        "</xdr:spPr>"
        "</xdr:sp>"
        "</xdr:grpSp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(base, shaped, group_xml)

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    def _avg_x(predicate) -> float:
        pixels = img.load()
        width, height = img.size
        xs = [x for y in range(height) for x in range(width) if predicate(pixels[x, y])]
        assert xs, "not a single pixel of that color was drawn"
        return sum(xs) / len(xs)

    red_x = _avg_x(lambda px: px[0] > 200 and px[1] < 80 and px[2] < 80)
    blue_x = _avg_x(lambda px: px[2] > 200 and px[0] < 80 and px[1] < 80)
    assert red_x < blue_x, (
        "red (in the left half of the group's internal coordinate system) must also be to the left of blue (right half) in the capture PNG"
    )


def test_xlsx_capture_rotates_grouped_shapes(tmp_path: Path) -> None:
    """Regression test — a rotated `xdr:grpSp` (`grpSpPr/xfrm`'s `rot`) used
    to be skipped entirely (see `_expand_group_shapes`'s old early return).
    Injects a group rotated 45 degrees around a single child shape that
    fills the group's whole internal coordinate system (so its center
    coincides with the group's own center, isolating "does the child's own
    rotation pick up the group's rotation" from "was the child's position
    also correctly rotated," which `test_xlsx_capture_expands_grouped_shapes`
    already covers for the unrotated case) — verifies the shape is actually
    drawn rotated, the same corner-stays-background check as
    `test_xlsx_capture_rotates_shapes`."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_rotated_group.xlsx"
    group_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        "<xdr:to><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:grpSp>'
        '<xdr:nvGrpSpPr><xdr:cNvPr id="30" name="RotGroup"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr>'
        '<xdr:grpSpPr>'
        '<a:xfrm rot="2700000"><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="2000000" cy="2000000"/></a:xfrm>'
        "</xdr:grpSpPr>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="31" name="Full"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="2000000" cy="2000000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="00FF00"/></a:solidFill>'
        "</xdr:spPr>"
        "</xdr:sp>"
        "</xdr:grpSp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(base, shaped, group_xml)

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    assert _has_pixel(img, lambda px: px[1] > 150 and px[0] < 120 and px[2] < 120), (
        "the rotated group's green shape must still be drawn"
    )
    top_left = img.getpixel((0, 0))
    assert top_left == (255, 255, 255), (
        "an unrotated square would fill the padded canvas's very corner; a 45-degree rotation must leave it background"
    )


def _inject_xdr_text_shape(
    src_path: Path, dst_path: Path, from_rc: tuple[int, int], to_rc: tuple[int, int], text: str, hex_color: str
) -> None:
    """`from_rc`/`to_rc` are 0-based (row, col). An `xdr:sp` with no
    fill/outline, only a text label (Excel's "text box" shape) — an
    explicit color is given so it can be distinguished from
    `_shape_text_lines`'s fallback (black) when it can't read a color."""
    shape_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f"<xdr:from><xdr:col>{from_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{from_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f"<xdr:to><xdr:col>{to_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{to_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="800" name="Label"/><xdr:cNvSpPr txBox="1"/></xdr:nvSpPr>'
        '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></xdr:spPr>'
        "<xdr:txBody><a:bodyPr/><a:p><a:r>"
        f'<a:rPr sz="1600"><a:solidFill><a:srgbClr val="{hex_color}"/></a:solidFill></a:rPr>'
        f"<a:t>{text}</a:t>"
        "</a:r></a:p></xdr:txBody>"
        "</xdr:sp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(src_path, dst_path, shape_xml)


def test_xlsx_capture_draws_shape_label_text(tmp_path: Path) -> None:
    """Regression test — even for an `xdr:sp` that's a "text box" shape with
    no fill/outline, only text (`xdr:txBody`) (a very common annotation
    label form in Excel), that text must be drawn in the capture PNG in its
    specified color. It used to be that when fill and outline were both
    None, the whole shape was skipped and the label text disappeared."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_text_label.xlsx"
    _inject_xdr_text_shape(base, shaped, from_rc=(1, 1), to_rc=(3, 3), text="Label", hex_color="FFFF00")

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    capture_path = Path(tables[0].properties["capture_path"])
    img = PILImage.open(capture_path).convert("RGB")

    assert _has_pixel(img, lambda px: px[0] > 200 and px[1] > 200 and px[2] < 100), (
        "the yellow text of a fill/outline-less text-box shape must be drawn in the capture PNG"
    )


def _inject_pic_xfrm(src_path: Path, dst_path: Path, off_emu: tuple[int, int], ext_emu: tuple[int, int]) -> None:
    """Rewrites the *second* `<pic>` in the drawing XML (`build_xlsx`'s
    standalone "Image 2" at F10 — the in-table "Image 1" is first) to carry
    an explicit `pic/spPr/xfrm` off+ext, and also resizes that anchor's own
    top-level `<ext>` (sibling of `<from>`) to the same `ext_emu` — that
    sibling `<ext>`, not `pic/spPr/xfrm/ext`, is what `image_anchor_rect`
    actually uses for a fixed-size (`oneCell`) anchor's rendered pixel size,
    so leaving it at `build_xlsx`'s tiny original (381000x285750 EMU) would
    make the photo itself render far smaller than `ext_emu` implies.
    openpyxl itself never writes `pic/spPr/xfrm` at all (confirmed:
    `build_xlsx`'s own drawing XML has none), so a test exercising
    `_pic_anchor_own_off_ext_emu`'s calibration path has to inject it
    directly — the same reason `_inject_xdr_rect_shape` exists for shapes."""
    with zipfile.ZipFile(src_path) as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}
    drawing_name = next(n for n in entries if n.startswith("xl/drawings/drawing") and n.endswith(".xml"))
    xml = entries[drawing_name].decode("utf-8")
    marker = "<spPr><a:prstGeom"
    first = xml.index(marker)
    second = xml.index(marker, first + 1)

    # The anchor-level <ext .../> is the closest one preceding this <pic>'s
    # own <spPr> — resize it in place.
    anchor_ext_start = xml.rindex("<ext ", 0, second)
    anchor_ext_end = xml.index("/>", anchor_ext_start) + len("/>")
    new_anchor_ext = f'<ext cx="{ext_emu[0]}" cy="{ext_emu[1]}"/>'
    xml = xml[:anchor_ext_start] + new_anchor_ext + xml[anchor_ext_end:]
    # `second` shifted by however much that replacement changed the length.
    second += len(new_anchor_ext) - (anchor_ext_end - anchor_ext_start)

    xfrm = (
        '<a:xfrm xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<a:off x="{off_emu[0]}" y="{off_emu[1]}"/><a:ext cx="{ext_emu[0]}" cy="{ext_emu[1]}"/></a:xfrm>'
    )
    insert_at = second + len("<spPr>")
    xml = xml[:insert_at] + xfrm + xml[insert_at:]
    entries[drawing_name] = xml.encode("utf-8")
    with zipfile.ZipFile(dst_path, "w") as zout:
        for name, data in entries.items():
            zout.writestr(name, data)


def test_xlsx_standalone_image_annotation_refines_position_via_own_offset(tmp_path: Path) -> None:
    """Regression test — `_xml_anchor_rect` positions a shape from
    `col_off[fc] + emu_to_px(fco)`, summing this module's *approximate*
    per-column pixel width from the canvas origin out to that shape's own
    column; the more columns in between (or the wider those columns are
    declared), the more that can drift from Excel's real column widths
    (confirmed on a real document: a red highlight box annotating two
    screws, anchored 3 columns past its photo, rendered a whole photo-width
    off, onto blank background instead of the screws). Widens column G
    (between the photo, anchored at F10 per `build_xlsx`, and a shape
    anchored just past it in column H) enough that the naive column-based
    estimate would place the shape past the photo's own midpoint, then
    gives both their own explicit `spPr/xfrm` off/ext (the shape's 15% into
    the photo's own footprint — near its *left* edge) and confirms the
    shape lands there instead — proof `annotate_standalone_image` refines a
    shape's position via the EMU delta between the two anchors' own offsets
    (`_calibrate_rect_from_offsets`) rather than trusting the column-based
    estimate outright. The shape still has to pass the ordinary column-based
    overlap check first (unchanged by this refinement — see
    `test_xlsx_standalone_image_annotation_rejects_a_distant_shapes_coincidental_offset`
    for why that gate has to run *before* calibration, not after)."""
    base = tmp_path / "sample_wide_cols.xlsx"
    wb = openpyxl.load_workbook(build_xlsx(tmp_path / "sample.xlsx"))
    ws = wb["Sheet1"]
    ws.column_dimensions["G"].width = 30  # wider than its real (unknown) rendered width, to induce naive drift
    wb.save(str(base))

    with_pic_xfrm = tmp_path / "sample_with_pic_xfrm.xlsx"
    image_off = (5_000_000, 3_000_000)
    image_ext = (4_000_000, 2_000_000)
    _inject_pic_xfrm(base, with_pic_xfrm, image_off, image_ext)

    shaped = tmp_path / "sample_with_nearby_shape.xlsx"
    shape_off = (image_off[0] + round(image_ext[0] * 0.15), image_off[1] + round(image_ext[1] * 0.15))
    shape_ext = (round(image_ext[0] * 0.2), round(image_ext[1] * 0.2))
    nearby_shape_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<xdr:from><xdr:col>7</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>9</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        "<xdr:to><xdr:col>8</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>10</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="500" name="NearbyBox"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        f'<a:xfrm><a:off x="{shape_off[0]}" y="{shape_off[1]}"/><a:ext cx="{shape_ext[0]}" cy="{shape_ext[1]}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="00FF00"/></a:solidFill>'
        "</xdr:spPr>"
        "</xdr:sp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(with_pic_xfrm, shaped, nearby_shape_xml)

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1
    assert images[0].properties.get("annotation_shape_count") == 1

    image_path = Path(images[0].properties["image_path"])
    img = PILImage.open(image_path).convert("RGB")
    width = img.width
    is_green = lambda px: px[1] > 150 and px[0] < 100 and px[2] < 100  # noqa: E731
    green_xs = [x for y in range(img.height) for x in range(width) if is_green(img.getpixel((x, y)))]
    assert green_xs, "the shape must be drawn"
    # Calibrated (via off/ext), it sits 15% into the photo's width — near
    # the left edge. The naive column-based estimate (column G widened to
    # 30 chars) would instead push it out past the photo's own midpoint.
    assert max(green_xs) < width * 0.4, (
        "the shape must land near the photo's left edge (per its own off), "
        "not out past the midpoint the column-based estimate would compute"
    )


def test_xlsx_standalone_image_annotation_rejects_a_distant_shapes_coincidental_offset(tmp_path: Path) -> None:
    """Regression test — a shape's own cached `spPr/xfrm off` isn't reliably
    an absolute position spanning the *whole* sheet (confirmed on a real
    document: a caption text box 18 rows and 35 columns away from an
    unrelated photo had an `off` only ~150-260px from that photo's own
    `off` — evidently stale/local, not live — which made it wrongly
    "overlap" when a first version of this fix computed the overlap
    decision *after* calibrating a shape's position instead of before).
    Gives a shape anchored far away (row 30, column 40) an `off` deliberately
    close to the photo's own `off`, and confirms it is *not* composited —
    proof the ordinary column-based `rects_overlap` check still gates
    calibration, so a coincidentally-close cached offset can't paper over an
    actually-distant anchor."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    with_pic_xfrm = tmp_path / "sample_with_pic_xfrm.xlsx"
    image_off = (5_000_000, 3_000_000)
    image_ext = (381_000, 285_750)  # matches build_xlsx's own standalone image ext, see its drawing XML
    _inject_pic_xfrm(base, with_pic_xfrm, image_off, image_ext)

    shaped = tmp_path / "sample_with_distant_shape.xlsx"
    # Physically far away (row 30, col 40) but an `off` only slightly past
    # the photo's own — exactly the coincidence found on the real document.
    distant_off = (image_off[0] + 50_000, image_off[1] + 50_000)
    distant_ext = (round(image_ext[0] * 0.3), round(image_ext[1] * 0.3))
    distant_shape_xml = (
        '<xdr:twoCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<xdr:from><xdr:col>40</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>30</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        "<xdr:to><xdr:col>41</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>31</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
        '<xdr:sp macro="" textlink="">'
        '<xdr:nvSpPr><xdr:cNvPr id="501" name="DistantBox"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        "<xdr:spPr>"
        f'<a:xfrm><a:off x="{distant_off[0]}" y="{distant_off[1]}"/><a:ext cx="{distant_ext[0]}" cy="{distant_ext[1]}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        '<a:solidFill><a:srgbClr val="00FF00"/></a:solidFill>'
        "</xdr:spPr>"
        "</xdr:sp>"
        "<xdr:clientData/>"
        "</xdr:twoCellAnchor>"
    )
    _inject_into_drawing_xml(with_pic_xfrm, shaped, distant_shape_xml)

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1
    assert not images[0].properties.get("annotation_shape_count"), (
        "a shape anchored 30 rows/35 columns away must not be composited onto this photo, "
        "even though its cached off happens to sit close to the photo's own"
    )


def _asymmetric_png_bytes() -> bytes:
    """A 40x20 PNG, red on the left half and blue on the right — asymmetric
    so a horizontal mirror (`flipH`) is visible in a pixel test, unlike this
    file's other fixture images (all a single solid color, which look
    identical after mirroring)."""
    img = PILImage.new("RGB", (40, 20), (220, 40, 40))
    for x in range(20, 40):
        for y in range(20):
            img.putpixel((x, y), (40, 40, 220))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _inject_xdr_pic(
    src_path: Path,
    dst_path: Path,
    from_rc: tuple[int, int],
    ext_emu: tuple[int, int],
    image_bytes: bytes,
    flip_h: bool = False,
) -> None:
    """Injects a brand-new `xdr:oneCellAnchor`/`xdr:pic` — with its own
    media part and relationship, not reusing an existing one — into the
    drawing XML. `build_xlsx`'s own images are added through openpyxl's
    `add_image`, which never writes `pic/spPr/xfrm` at all (confirmed
    empirically: its saved drawing XML has no `xfrm` on either `<pic>`), so
    a test exercising `pic_anchor_flip`/`_apply_flip` needs a hand-built
    `<xdr:pic>`, the same reason `_inject_xdr_rect_shape` hand-builds an
    `<xdr:sp>` for shapes."""
    with zipfile.ZipFile(src_path) as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}

    media_name = "xl/media/image_flip_test.png"
    entries[media_name] = image_bytes

    rels_name = "xl/drawings/_rels/drawing1.xml.rels"
    rels_xml = entries[rels_name].decode("utf-8")
    r_id = "rIdFlipTest"
    new_rel = (
        '<Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        f'Target="../media/image_flip_test.png" Id="{r_id}"/>'
    )
    entries[rels_name] = rels_xml.replace("</Relationships>", new_rel + "</Relationships>").encode("utf-8")

    flip_attr = ' flipH="1"' if flip_h else ""
    pic_xml = (
        '<xdr:oneCellAnchor xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<xdr:from><xdr:col>{from_rc[1]}</xdr:col><xdr:colOff>0</xdr:colOff>"
        f"<xdr:row>{from_rc[0]}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f'<xdr:ext cx="{ext_emu[0]}" cy="{ext_emu[1]}"/>'
        "<xdr:pic>"
        '<xdr:nvPicPr><xdr:cNvPr id="900" name="FlipTestPic"/><xdr:cNvPicPr/></xdr:nvPicPr>'
        f'<xdr:blipFill><a:blip r:embed="{r_id}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
        f'<xdr:spPr><a:xfrm{flip_attr}><a:off x="0" y="0"/><a:ext cx="{ext_emu[0]}" cy="{ext_emu[1]}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
        "</xdr:pic>"
        "<xdr:clientData/>"
        "</xdr:oneCellAnchor>"
    )
    drawing_name = next(n for n in entries if n.startswith("xl/drawings/drawing") and n.endswith(".xml"))
    entries[drawing_name] = entries[drawing_name].replace(b"</wsDr>", pic_xml.encode("utf-8") + b"</wsDr>")

    with zipfile.ZipFile(dst_path, "w") as zout:
        for name, data in entries.items():
            zout.writestr(name, data)


def _avg_rgb(img: PILImage.Image, x0: int, x1: int) -> tuple[float, float, float]:
    pixels = [img.getpixel((x, y)) for x in range(x0, x1) for y in range(img.height)]
    n = len(pixels)
    return (sum(p[0] for p in pixels) / n, sum(p[1] for p in pixels) / n, sum(p[2] for p in pixels) / n)


def test_xlsx_standalone_image_flip_is_applied(tmp_path: Path) -> None:
    """Regression test — a standalone image's own `spPr/xfrm flipH="1"`
    used to be silently ignored everywhere this module opens and pastes
    image bytes (confirmed on a real document: a screwdriver photo placed
    as an annotation, saved with `flipH="1"` meaning "show this mirrored,"
    rendered pointing the wrong way in every capture because nothing
    checked the flag). Injects an asymmetric (left-red/right-blue) image
    with `flipH="1"`, far from any table or other image so it's captured
    on its own (exercising `annotate_standalone_image`'s flip-only path,
    where there's nothing else to composite), and confirms red/blue swap
    sides in the saved capture."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    flipped = tmp_path / "sample_with_flipped_image.xlsx"
    _inject_xdr_pic(base, flipped, from_rc=(30, 30), ext_emu=(400_000, 200_000), image_bytes=_asymmetric_png_bytes(), flip_h=True)

    doc = xlsx_extractor.extract(flipped, capture_dir=tmp_path / "captures")
    images = [n for n in doc.nodes if n.type == NodeType.IMAGE and n.properties.get("row") == 31]
    assert len(images) == 1, "the injected picture must show up as its own standalone Image node"

    image_path = Path(images[0].properties["image_path"])
    img = PILImage.open(image_path).convert("RGB")
    left = _avg_rgb(img, 0, img.width // 2)
    right = _avg_rgb(img, img.width // 2, img.width)
    assert left[2] > left[0], "flipH='1' must move the blue half to the left side of the capture"
    assert right[0] > right[2], "flipH='1' must move the red half to the right side of the capture"


def test_xlsx_standalone_image_gets_annotation_shape_composited(tmp_path: Path) -> None:
    """Regression test — a highlight shape a person placed (e.g. a
    red/yellow highlight box) on top of a standalone image outside any
    table range (an Image node that isn't an Option A absorption target)
    used to, unlike table capture, be preserved nowhere at all — only the
    raw bytes were saved and the annotation was entirely lost. Verifies
    that `annotate_standalone_image` finds a shape overlapping the image's
    own anchor (F10, see `build_xlsx`) and composites it."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_image_annotation.xlsx"
    # places a shape near the standalone image anchored at F10 (0-based row9/col5) so it overlaps.
    _inject_xdr_rect_shape(base, shaped, from_rc=(9, 5), to_rc=(11, 8), hex_color="FFFF00")

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1
    assert images[0].properties.get("annotation_shape_count") == 1, (
        "annotation_shape_count must be filled in when there's an overlapping shape"
    )

    image_path = Path(images[0].properties["image_path"])
    img = PILImage.open(image_path).convert("RGB")
    assert _has_pixel(img, lambda px: abs(px[0] - 200) < 30 and abs(px[1] - 160) < 30 and abs(px[2] - 40) < 30), (
        "the original photo's (orange-ish 200,160,40) pixels must survive in the composite"
    )
    assert _has_pixel(img, lambda px: px[0] > 200 and px[1] > 200 and px[2] < 100), (
        "the overlapping yellow highlight box's outline must be drawn in the composite PNG"
    )


def test_xlsx_standalone_image_without_nearby_shape_keeps_raw_bytes(tmp_path: Path) -> None:
    """Regression test — even with a shape present, if it doesn't overlap a
    standalone image's anchor area (e.g. a shape above the table range),
    that standalone image must be left as the raw bytes as-is (no
    unnecessary re-encoding/compositing — the path where
    `annotate_standalone_image` returns None)."""
    base = build_xlsx(tmp_path / "sample.xlsx")
    shaped = tmp_path / "sample_with_unrelated_shape.xlsx"
    # a shape that only spans B2:C3 (the table range) — doesn't overlap the standalone image at F10.
    _inject_xdr_rect_shape(base, shaped, from_rc=(1, 1), to_rc=(3, 3), hex_color="FF0000")

    doc = xlsx_extractor.extract(shaped, capture_dir=tmp_path / "captures")
    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1
    assert "annotation_shape_count" not in images[0].properties
    image_path = Path(images[0].properties["image_path"])
    img = PILImage.open(image_path).convert("RGB")
    assert img.size == (40, 30), "with no overlapping shape, the image must survive at its original size"


def test_xlsx_overlapping_standalone_images_get_absorbed(tmp_path: Path) -> None:
    """Regression test — when standalone images outside any table range
    overlap each other (e.g. a small icon photo placed separately on top of
    a large background photo, confirmed in a screw fastening-strength
    review document), they should not both become independent Image nodes
    — the smaller one (the icon) should be composited onto the larger one
    (the background photo) and absorbed. It used to be that
    `annotate_standalone_image` only checked overlap against vector shapes
    (`xdr:sp`/`xdr:cxnSp`), entirely missing another photo placed on top of
    a photo, so each became a separate node."""
    path = build_xlsx_with_overlapping_standalone_images(tmp_path / "overlap.xlsx")
    doc = xlsx_extractor.extract(path, capture_dir=tmp_path / "captures")

    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1, "two overlapping standalone images must be absorbed into one Image node"
    assert images[0].properties.get("absorbed_image_count") == 1, (
        "the number of absorbed images must be kept in absorbed_image_count"
    )

    image_path = Path(images[0].properties["image_path"])
    img = PILImage.open(image_path).convert("RGB")
    assert _has_pixel(img, lambda px: abs(px[0] - 200) < 30 and abs(px[1] - 160) < 30 and abs(px[2] - 40) < 30), (
        "the background photo's (orange-ish 200,160,40) pixels must survive in the composite"
    )
    assert _has_pixel(img, lambda px: abs(px[0] - 30) < 30 and abs(px[1] - 30) < 30 and abs(px[2] - 200) < 30), (
        "the absorbed icon photo's (blue-ish 30,30,200) pixels must survive in the composite"
    )


def test_xlsx_extract_caption_and_reference_label_heuristics(tmp_path: Path) -> None:
    """xlsx.py wires the same deterministic heuristics docx.py/pdf.py/pptx.py
    use: "표 1. …" directly above a table gets a CAPTION_OF (`_CAPTION_PREFIXES`),
    and a separate cell elsewhere on the sheet citing that same label by name
    ("표1") gets a REFERENCES edge to the same table
    (`scaffold.reference_label_edges`) — no proximity to the table required."""
    path = build_xlsx_with_table_caption_and_reference(tmp_path / "table.xlsx")
    doc = xlsx_extractor.extract(path, capture_dir=tmp_path / "captures")

    tables = [n for n in doc.nodes if n.type == NodeType.TABLE]
    caption = next(n for n in doc.nodes if n.type == NodeType.TEXT and n.properties["text"].startswith("표 1"))
    citation = next(n for n in doc.nodes if n.type == NodeType.TEXT and n.properties["text"].startswith("표1"))
    assert len(tables) == 1

    caption_edges = [e for e in doc.edges if e.type == EdgeType.CAPTION_OF and e.source_id == caption.id and e.target_id == tables[0].id]
    assert len(caption_edges) == 1, "the '표 1.' caption must resolve to a CAPTION_OF edge on the table"

    reference_edges = [e for e in doc.edges if e.type == EdgeType.REFERENCES and e.source_id == citation.id and e.target_id == tables[0].id]
    assert len(reference_edges) == 1, "citing the caption's own label ('표1') elsewhere on the sheet must resolve to a REFERENCES edge on the table"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_pdf_extract_single_page(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "sample.pdf")
    doc = pdf_extractor.extract(path, capture_dir=tmp_path / "captures")

    assert _types(doc) >= {NodeType.FILE, NodeType.ARTIFACT, NodeType.TEXT, NodeType.IMAGE}
    assert NodeType.TABLE not in _types(doc), "the PDF native path doesn't create a Table (§3-a)"
    assert check_invariants(doc.nodes, doc.edges) == []

    artifact = next(n for n in doc.nodes if n.type == NodeType.ARTIFACT)
    assert artifact.properties["page_count"] == 1
    texts = [n for n in doc.nodes if n.type == NodeType.TEXT]
    assert all(t.properties["page_index"] == 0 for t in texts)
    assert any("Heading" in t.properties["text"] for t in texts)

    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    assert len(images) == 1
    image_path = Path(images[0].properties["image_path"])
    assert image_path.exists(), "the PDF image block original must actually be saved"


def test_pdf_extract_multi_page_has_no_next_edges(tmp_path: Path) -> None:
    """Schema decision (§3-a): PDF = one Artifact (not split per page) ->
    since there's only one Artifact, no NEXT edge is ever created (same
    reason as docx.py). Page boundaries are distinguished only by content
    nodes' `page_index`."""
    path = build_pdf(tmp_path / "multi.pdf", n_pages=3)
    doc = pdf_extractor.extract(path, capture_dir=tmp_path / "captures")

    artifacts = [n for n in doc.nodes if n.type == NodeType.ARTIFACT]
    assert len(artifacts) == 1
    assert artifacts[0].properties["page_count"] == 3
    assert not any(e.type.value == "NEXT" for e in doc.edges)

    page_indices = {t.properties["page_index"] for t in doc.nodes if t.type == NodeType.TEXT}
    assert page_indices == {0, 1, 2}
    assert check_invariants(doc.nodes, doc.edges) == []


def test_pdf_extract_uses_visual_order_not_stream_order(tmp_path: Path) -> None:
    """Empirically confirmed (`docs/docling-compat-harness.md` §4a): PDF
    content-stream order can be unrelated to on-screen position (a real
    case confirmed where a document title showed up 15th in the node list
    in a document exported from PPT) — if the "node order ≈ document
    order" that `relations/propose.py`'s `build_context_windows` assumes
    breaks, a caption candidate gets pushed outside the window. Regression
    test that `page.get_text(..., sort=True)` restores the on-screen
    top-to-bottom order."""
    path = build_pdf_out_of_stream_order(tmp_path / "reordered.pdf")
    doc = pdf_extractor.extract(path, capture_dir=tmp_path / "captures")

    texts = [n for n in doc.nodes if n.type == NodeType.TEXT]
    assert [t.properties["text"] for t in texts] == [
        "Top paragraph (inserted second)",
        "Bottom paragraph (inserted first)",
    ], "the paragraph higher up on screen must come first, regardless of content-stream order"


def test_pdf_extract_caption_of_heuristic(tmp_path: Path) -> None:
    """pdf.py's deterministic `_CAPTION_PREFIXES` heuristic — verifies that
    "Figure 1. …" catches the immediately preceding image as a CAPTION_OF
    (the same pattern as docx.py's identical test)."""
    path = build_pdf_with_figure_caption(tmp_path / "captioned.pdf")
    doc = pdf_extractor.extract(path, capture_dir=tmp_path / "captures")

    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    captions = [n for n in doc.nodes if n.type == NodeType.TEXT and n.properties["text"].startswith("Figure ")]
    assert len(images) == 1
    assert len(captions) == 1

    caption_edges = [
        e for e in doc.edges
        if e.type == EdgeType.CAPTION_OF and e.source_id == captions[0].id and e.target_id == images[0].id
    ]
    assert len(caption_edges) == 1, "the Figure caption must be proposed as CAPTION_OF on the immediately preceding image"
    assert check_invariants(doc.nodes, doc.edges) == []


def test_pdf_extract_reference_label_heuristic(tmp_path: Path) -> None:
    """scaffold.py's deterministic `reference_label_edges` heuristic:
    "As discussed in Figure 1, …" on a different page from the caption/image
    still resolves to a REFERENCES edge on the image, purely by matching the
    caption's own label — no proximity/adjacency involved, unlike
    `caption_prefix_edges`."""
    path = build_pdf_with_figure_reference(tmp_path / "referenced.pdf")
    doc = pdf_extractor.extract(path, capture_dir=tmp_path / "captures")

    images = [n for n in doc.nodes if n.type == NodeType.IMAGE]
    citations = [n for n in doc.nodes if n.type == NodeType.TEXT and "discussed in Figure 1" in n.properties["text"]]
    assert len(images) == 1
    assert len(citations) == 1

    reference_edges = [
        e for e in doc.edges
        if e.type == EdgeType.REFERENCES and e.source_id == citations[0].id and e.target_id == images[0].id
    ]
    assert len(reference_edges) == 1, "citing the caption's own label must resolve to a REFERENCES edge on the anchor, regardless of page/proximity"
    assert check_invariants(doc.nodes, doc.edges) == []


def _block(label: str, bbox: tuple[float, float, float, float]) -> dict:
    """`_reorder_same_line_blocks` only reads `bbox`, so a minimal dict with
    just the empirically measured coordinates is enough to mimic a pymupdf
    block — `label` is an identifier used only by the test."""
    return {"bbox": bbox, "label": label}


def test_reorder_same_line_blocks_fixes_scrambled_math_fragment_order() -> None:
    """Confirmed (`1706.03762`'s MultiHead attention formula,
    `docs/vlm-integration-research.md` §11): a sub/superscript splits one
    line into 4 blocks, and pymupdf's `sort=True` scrambles the Q,K,V order
    within it into Q,V,K — reproduces the actually-measured bboxes as-is to
    verify that `_reorder_same_line_blocks` fixes it into ascending x
    (Q,K,V). The other line (MultiHead(...) itself) must be untouched."""
    multihead = _block("multihead", (186.94, 145.82, 410.92, 158.40))  # a separate line, must be untouched
    where_q = _block("where_Q", (224.497, 161.956, 358.449, 175.865))  # "where headi = Attention(QW Q"
    frag_v = _block("frag_V", (381.956, 162.630, 418.974, 176.833))  # "i , V W V"
    frag_close = _block("frag_close", (412.883, 164.407, 425.061, 176.833))  # "i )"
    frag_k = _block("frag_K", (350.789, 162.630, 390.035, 177.147))  # "i , KW K"

    # exactly the actual (buggy) order pymupdf sort=True produced
    scrambled = [multihead, where_q, frag_v, frag_close, frag_k]

    reordered = _reorder_same_line_blocks(scrambled)

    assert [b["label"] for b in reordered] == [
        "multihead", "where_Q", "frag_K", "frag_V", "frag_close",
    ], "same-line fragments must be in ascending x (Q, K, V, closing paren), and the MultiHead(...) line must stay put"


def test_reorder_same_line_blocks_does_not_bridge_across_a_tall_block() -> None:
    """Confirmed (`1706.03762` page 1, §11): if a tall rotated arXiv
    watermark block (341pt tall) gets grouped together with the title,
    authors, and Abstract heading just because its y overlaps all of them
    (a simple union-find's transitive connectivity), reordering by x makes
    things worse than the original order (e.g. the "Abstract" heading gets
    pushed behind the body text). `_LINE_MAX_HEIGHT` excludes this watermark
    from grouping, so only the genuinely same-line pair (Ashish/Noam, both
    short) should be a reorder target."""
    title = _block("title", (211.49, 148.83, 399.89, 166.05))
    ashish = _block("ashish", (132.91, 233.54, 203.89, 245.01))
    noam = _block("noam", (239.06, 233.54, 304.84, 245.01))
    abstract_heading = _block("abstract_heading", (283.76, 385.61, 328.24, 397.56))
    watermark = _block("watermark", (10.94, 213.92, 37.62, 555.0))  # 341pt tall — y overlaps everything

    original = [title, ashish, noam, watermark, abstract_heading]

    reordered = _reorder_same_line_blocks(original)

    assert [b["label"] for b in reordered] == [
        "title", "ashish", "noam", "watermark", "abstract_heading",
    ], "a tall watermark must not group unrelated blocks together and scramble their order"


def test_reorder_same_line_blocks_leaves_non_overlapping_blocks_untouched() -> None:
    """Blocks whose y doesn't overlap (different lines) keep their original order — the no-op path."""
    a = _block("a", (72.0, 100.0, 150.0, 112.0))
    b = _block("b", (72.0, 120.0, 150.0, 132.0))

    assert [x["label"] for x in _reorder_same_line_blocks([a, b])] == ["a", "b"]
    assert [x["label"] for x in _reorder_same_line_blocks([b, a])] == ["b", "a"]


def test_pptx_rotated_picture_visual_bbox_preserves_raw_geometry(tmp_path: Path) -> None:
    """A 270-degree supplier photo occupies a tall box, despite wide raw extents."""
    from pptx import Presentation
    from pptx.util import Inches

    image_path = tmp_path / 'wide.png'
    PILImage.new('RGB', (400, 200), 'blue').save(image_path)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(10), Inches(10)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    picture = slide.shapes.add_picture(str(image_path), Inches(2), Inches(3), Inches(4), Inches(2))
    picture.rotation = 270
    path = tmp_path / 'rotated.pptx'
    prs.save(path)
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / 'captures')
    node = next(n for n in doc.nodes if n.type == NodeType.IMAGE)
    assert node.properties['bbox'] == {'x_min': 300, 'y_min': 200, 'x_max': 500, 'y_max': 600}
    assert node.properties['left'] == Inches(2)
    assert node.properties['width'] == Inches(4)
    assert node.properties['rotation'] == 270
    assert check_invariants(doc.nodes, doc.edges) == []


def test_pptx_nested_group_coordinates_and_reading_order(tmp_path: Path) -> None:
    """Nested local coordinates must map into the moved/scaled outer group."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = prs.slide_height = Inches(10)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    outer = slide.shapes.add_group_shape()
    inner = outer.shapes.add_group_shape()
    box = inner.shapes.add_textbox(Inches(1), Inches(1), Inches(1), Inches(1))
    box.text = 'Grouped'
    # Explicit transforms model nested groups from the supplier deck. Adding
    # children recomputes extents, so set the transforms after creating them.
    from pptx.oxml.ns import qn
    for group, off, ext, child_off, child_ext in [
        (inner, (2, 2), (4, 2), (1, 1), (2, 1)),
        (outer, (6, 4), (2, 2), (2, 2), (4, 2)),
    ]:
        transform = group._element.grpSpPr.xfrm
        for tag, values, attrs in [('off', off, ('x', 'y')), ('ext', ext, ('cx', 'cy')),
                                  ('chOff', child_off, ('x', 'y')), ('chExt', child_ext, ('cx', 'cy'))]:
            element = transform.find(qn('a:' + tag))
            for attr, value in zip(attrs, values):
                element.set(attr, str(Inches(value)))
    early = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(2), Inches(1))
    early.text = 'Before group'
    path = tmp_path / 'nested.pptx'
    prs.save(path)
    doc = pptx_extractor.extract(path, capture_dir=tmp_path / 'captures')
    texts = [n for n in doc.nodes if n.type == NodeType.TEXT]
    assert [n.properties['text'] for n in texts] == ['Before group', 'Grouped']
    assert texts[1].properties['bbox'] == {'x_min': 600, 'y_min': 400, 'x_max': 700, 'y_max': 600}
    assert texts[1].properties['left'] == Inches(1)
    assert check_invariants(doc.nodes, doc.edges) == []
    # Reflection then 90-degree rotation also applies in the parent frame.
    inner._element.grpSpPr.xfrm.set('flipH', '1')
    outer.rotation = 90
    prs.save(path)
    rotated = pptx_extractor.extract(path, capture_dir=tmp_path / 'captures')
    grouped = next(n for n in rotated.nodes if n.properties.get('text') == 'Grouped')
    assert grouped.properties['bbox'] == {'x_min': 600, 'y_min': 500, 'x_max': 800, 'y_max': 600}
    assert check_invariants(rotated.nodes, rotated.edges) == []
