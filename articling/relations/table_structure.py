"""Actually reads a PDF table candidate crop — grid (row/column text)
extraction, with two independent backends.

`extractors/pdf_tables.py`'s geometric candidate detection
(`detect_table_candidates`) is **cheap but has a lot of false positives** —
confirmed (`docs/vlm-integration-research.md` §10) cases like a slide with
several images placed side by side getting its whole array of border boxes
caught as a "table" — pure vector graphics alone can't distinguish a
"table-shaped layout" from a "real table." This module handles that final
check (and grid reading).

**The two backends can be called independently, but they differ in
trustworthiness** (see each function's docstring below for the empirical
basis):

- `read_table_grid_openai`: asks a general-purpose VLM directly for an
  `is_table` judgment as structured output — confirmed to actually follow
  the instruction "answer that it's not a table if it isn't." **Self-gated,
  so a standalone call is safe.**
- `read_table_grid_granite_docling`: has a small model dedicated to table
  structure (`ibm-granite/granite-docling-258M`) read it, using the prompt
  "Convert table to OTSL." **Has no self-gating** — even given a non-table
  image, it invents a plausible-looking fake table (confirmed: given an
  image of a circle + a border box, it invented a "Size/Height/Width"
  header and "1.0/1.0/1.0" values). The alternative general-purpose prompt
  tried ("Convert this page to docling.") had the opposite problem of
  scattering even a real table into separate text (confirmed: `doc.tables`
  came back empty) — so it didn't dodge this issue either. In other words,
  this model can't reliably judge "table or not" on its own from a single
  crop. So **only use it on a crop the caller is already confident is a
  table**, or have it go through `read_table_grid_openai`'s judgment first
  via `verify_with_openai=True`.

`read_table_grid` wraps both of the above behind one interface, letting you
choose either "OpenAI gates first -> only what passes gets read by
Granite-Docling" (read locally/free, but safely) via `verify_with_openai`,
or "call it directly with no gate" (faster, but the caller takes on the
hallucination risk above) — both kept available as options per a deliberate
design decision (2026-09-04, per user direction).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .._image_util import encode_image_data_url
from ..extractors.pdf import _normalized_bbox, _resort_pdf_content_nodes
from ..extractors.pdf_tables import TableCandidate, detect_table_candidates
from ..scaffold import save_image_bytes
from ..schema import ArticDocument, Edge, EdgeType, Node, NodeType

DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"
GRANITE_DOCLING_REPO_ID = "ibm-granite/granite-docling-258M"
# The prompt settled on by empirical testing (2026-09-04) — the model
# card's short instruction dedicated to "Table". "Convert this page to
# docling." (the general-purpose page prompt) scatters a table in a crop
# into separate text (doc.tables=0), so it can't be used for table reading.
GRANITE_DOCLING_TABLE_PROMPT = "Convert table to OTSL."

_INSTRUCTIONS_OPENAI = """\
Inspect one image and determine whether it is a genuine table containing data
organized into rows and columns. Photos, diagrams, decorative bordered boxes,
icon panels, and grids of separate images are not tables. When uncertain,
return false; a conservative rejection is better than inventing table data.

If it is a genuine table, transcribe every cell into a row-major grid. Never
invent values that are not visible. Use an empty string for unreadable cells.
"""


class _TableReadResult(BaseModel):
    is_table: bool = Field(description="Whether the image is a genuine row-and-column data table. Use false when uncertain.")
    rows: list[list[str]] = Field(
        default_factory=list, description="For a genuine table only: row-major grid preserving visible cell text."
    )


def read_table_grid_openai(
    image_path: str, client=None, model: str = DEFAULT_OPENAI_MODEL
) -> Optional[list[list[str]]]:
    """Looks at the image, judges whether it's a table, and if so reads the
    grid. Returns `None` if judged not to be a table (`None`, not an empty
    list — to distinguish "it is a table but the cells are empty" from
    "it's not a table at all").

    If `client` isn't given, `openai.OpenAI()` is built with its default
    constructor (needs the `OPENAI_API_KEY` environment variable) — this is
    why `openai` is an optional dependency.
    """
    if client is None:
        from openai import OpenAI  # lazy import — openai isn't needed unless this function is called

        client = OpenAI()

    content = [
        {"type": "input_text", "text": "Determine whether this image is a table and, if so, transcribe its grid."},
        {"type": "input_image", "image_url": encode_image_data_url(image_path), "detail": "high"},
    ]
    response = client.responses.parse(
        model=model,
        instructions=_INSTRUCTIONS_OPENAI,
        input=[{"role": "user", "content": content}],
        text_format=_TableReadResult,
    )
    parsed = response.output_parsed
    if parsed is None or not parsed.is_table:
        return None
    return parsed.rows


# Reuses the loaded model/processor (a module-global lazy singleton) —
# reloading on every call takes tens of seconds (confirmed). Tests can bypass
# this global state by passing `model=`/`processor=` directly.
_granite_docling_model = None
_granite_docling_processor = None


def _load_granite_docling():
    global _granite_docling_model, _granite_docling_processor
    if _granite_docling_model is None:
        import torch  # lazy import — torch/transformers isn't needed unless this backend is used
        from transformers import AutoModelForImageTextToText, AutoProcessor

        _granite_docling_processor = AutoProcessor.from_pretrained(GRANITE_DOCLING_REPO_ID)
        _granite_docling_model = AutoModelForImageTextToText.from_pretrained(
            GRANITE_DOCLING_REPO_ID, dtype=torch.float32
        )
    return _granite_docling_model, _granite_docling_processor


def read_table_grid_granite_docling(
    image_path: str,
    model=None,
    processor=None,
    max_new_tokens: int = 4096,
) -> list[list[str]]:
    """Reads a table crop as OTSL with `ibm-granite/granite-docling-258M`
    and returns it as a grid. **Has no self-gating** — see the empirical
    warning in the module docstring. Given a non-table image, it can return
    a plausible-looking fake table (raises no exception). Only use it on a
    crop you're already confident is a table, or gate it first via
    `read_table_grid`'s `verify_with_openai=True`.

    Slow on CPU (confirmed: tens of seconds to a few minutes per table —
    heavily dependent on token count/CPU performance). Passing
    `model`/`processor` directly bypasses the module-global cache, letting
    tests inject fake objects.
    """
    from PIL import Image

    if model is None or processor is None:
        model, processor = _load_granite_docling()

    image = Image.open(image_path).convert("RGB")
    messages = [
        {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": GRANITE_DOCLING_TABLE_PROMPT}]}
    ]
    prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
    inputs = processor(text=prompt, images=[image], return_tensors="pt")
    generated_ids = model.generate(**inputs, max_new_tokens=max_new_tokens)
    prompt_len = inputs.input_ids.shape[1]
    doctags = processor.batch_decode(generated_ids[:, prompt_len:], skip_special_tokens=False)[0].lstrip()

    from docling_core.types.doc import DoclingDocument  # lazy import — an optional dependency specific to this backend
    from docling_core.types.doc.document import DocTagsDocument

    doctags_doc = DocTagsDocument.from_doctags_and_image_pairs([doctags], [image])
    doc = DoclingDocument.load_from_doctags(doctags_doc, document_name="table_crop")
    if not doc.tables:
        return []
    return doc.tables[0].export_to_dataframe(doc).values.tolist()


def read_table_grid(
    image_path: str,
    backend: Literal["openai", "granite_docling"] = "openai",
    *,
    verify_with_openai: bool = False,
    openai_client=None,
    openai_model: str = DEFAULT_OPENAI_MODEL,
    granite_model=None,
    granite_processor=None,
) -> Optional[list[list[str]]]:
    """Both backends behind one interface. `backend="openai"` is always
    self-gated. `backend="granite_docling"` reads directly with no gate by
    default (for a crop the caller is already confident about) — passing
    `verify_with_openai=True` first confirms it's a table with
    `read_table_grid_openai`, and only what's confirmed gets read by
    Granite-Docling (meaning one extra OpenAI call)."""
    if backend == "openai":
        return read_table_grid_openai(image_path, client=openai_client, model=openai_model)

    if backend == "granite_docling":
        if verify_with_openai:
            gate = read_table_grid_openai(image_path, client=openai_client, model=openai_model)
            if gate is None:
                return None
        return read_table_grid_granite_docling(image_path, model=granite_model, processor=granite_processor)

    raise ValueError(f"Unknown backend: {backend!r} (must be openai or granite_docling)")


def fill_table_grids(
    document: ArticDocument,
    backend: Literal["openai", "granite_docling"] = "openai",
    **kwargs,
) -> list[str]:
    """Picks out only the `Table` nodes in `document` that have a
    `capture_path` but an empty `grid` or a `grid_source` of
    `"text_fallback"`, and fills them in with `read_table_grid`. A grid
    that's already trustworthy (e.g. a value read directly from the
    original, as with XLSX) is left untouched — an XLSX Table with no
    `grid_source` never matches this condition in the first place.

    Treated at the same trust level as `caption_images.py`'s
    `vlm_description` — this only fills in a **property** on an
    already-final Table node, so the graph structure (edges) doesn't
    change. If the reading comes back `is_table=False` (OpenAI backend) or
    the call fails, that node is silently skipped (doesn't stop the whole
    run).

    Returns: the list of Table node ids whose grid was actually filled in."""
    updated: list[str] = []
    for node in document.nodes:
        if node.type != NodeType.TABLE:
            continue
        capture_path = node.properties.get("capture_path")
        if not capture_path or not Path(capture_path).exists():
            continue
        if node.properties.get("grid") and node.properties.get("grid_source") != "text_fallback":
            continue
        try:
            grid = read_table_grid(capture_path, backend=backend, **kwargs)
        except Exception:  # noqa: BLE001 — a model/API call failure, skip just this node
            continue
        if grid is None:
            continue
        node.properties["grid"] = grid
        node.properties["grid_source"] = f"{backend}"
        updated.append(node.id)
    return updated


def _page_text_blocks(page) -> list[tuple[tuple[float, float, float, float], str]]:
    """Extracts only the page's text bboxes, the same way `extractors/pdf.py`
    does (sorted text blocks) — used for a table candidate's "does it
    actually have text" gate."""
    raw = page.get_text("dict", sort=True)
    out = []
    for block in raw["blocks"]:
        if block["type"] != 0:
            continue
        text = "\n".join(
            "".join(span["text"] for span in line["spans"]) for line in block["lines"]
        ).strip()
        if text:
            out.append((block["bbox"], text))
    return out


def _bbox_center_inside(bbox: tuple[float, float, float, float], region: tuple[float, float, float, float], tol: float = 2.0) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return (region[0] - tol <= cx <= region[2] + tol) and (region[1] - tol <= cy <= region[3] + tol)


_MIN_TEXT_BLOCKS_FOR_CANDIDATE = 2  # confirmed (§10): without this, a diagram/image layout also gets mistaken for a table


def enrich_pdf_tables(
    document: ArticDocument,
    backend: Literal["openai", "granite_docling"] = "openai",
    capture_dir: Optional[Path] = None,
    **backend_kwargs,
) -> list[str]:
    """Reopens `document.source_path` (the PDF), finds geometric table
    candidates (`extractors/pdf_tables.detect_table_candidates`), reads only
    the candidates that actually have text (a false-positive filter, see the
    constant comment above for the empirical basis) with `read_table_grid`,
    and turns only the confirmed ones into a `Table` node.

    **This is a separate opt-in step `extract()` doesn't run automatically**
    — the PDF native path (`extractors/pdf.py`) still never creates a Table
    (see Experiments.md "PDF Support"). A Table node only appears in a PDF once this
    function is called explicitly.

    Existing Text/Image nodes in an area confirmed as a table are removed
    (the same principle as XLSX's "Option A: absorb" — content inside a
    table never becomes a separate top-level node), and a single Table node
    with `capture_path`+`grid` filled in takes their place. A candidate the
    model judges "not a table" is silently skipped, leaving the original
    Text/Image nodes as-is (doesn't stop the whole run — the same principle
    as the other VLM modules).

    Returns: the list of newly created Table node ids."""
    import pymupdf

    artifact = next(n for n in document.nodes if n.type == NodeType.ARTIFACT)
    pdf_path = Path(document.source_path)
    capture_root = capture_dir if capture_dir is not None else pdf_path.parent / "captures"
    created: list[str] = []

    pdf = pymupdf.open(str(pdf_path))
    try:
        for page_index in range(pdf.page_count):
            page = pdf[page_index]
            w, h = page.rect.width, page.rect.height
            candidates = detect_table_candidates(page)
            if not candidates:
                continue

            text_blocks = _page_text_blocks(page)

            page_nodes = [
                n for n in document.nodes
                if n.type in (NodeType.TEXT, NodeType.IMAGE) and n.properties.get("page_index") == page_index
            ]

            for i, cand in enumerate(candidates):
                n_texts = sum(1 for bbox, _ in text_blocks if _bbox_center_inside(bbox, cand.bbox))
                if n_texts < _MIN_TEXT_BLOCKS_FOR_CANDIDATE:
                    continue

                cand_bbox_norm = _normalized_bbox(cand.bbox, w, h)
                absorbed = [
                    n for n in page_nodes
                    if _bbox_center_inside(
                        (
                            n.properties["bbox"]["x_min"], n.properties["bbox"]["y_min"],
                            n.properties["bbox"]["x_max"], n.properties["bbox"]["y_max"],
                        ),
                        (cand_bbox_norm["x_min"], cand_bbox_norm["y_min"], cand_bbox_norm["x_max"], cand_bbox_norm["y_max"]),
                        tol=0.0,
                    )
                ]

                pixmap = page.get_pixmap(clip=pymupdf.Rect(*cand.bbox), dpi=200)
                stem = f"{pdf_path.stem}__table_p{page_index}_{i}"
                saved = save_image_bytes(capture_root, stem, pixmap.tobytes("png"), "png")

                try:
                    grid = read_table_grid(str(saved), backend=backend, **backend_kwargs)
                except Exception:  # noqa: BLE001 — a model/API call failure, skip just this candidate
                    continue
                if grid is None:
                    continue  # confirmed "not a table" — leave the existing node as-is

                table_node = Node(
                    id=f"content:{pdf_path.name}:table_p{page_index}_{i}",
                    type=NodeType.TABLE,
                    name=f"Table (p.{page_index + 1})",
                    properties={
                        "page_index": page_index,
                        "bbox": cand_bbox_norm,
                        "grid": grid,
                        "grid_source": backend,
                        "capture_path": str(saved),
                        "detection_mode": cand.mode,
                    },
                )

                absorbed_ids = {n.id for n in absorbed}
                document.nodes = [n for n in document.nodes if n.id not in absorbed_ids]
                document.edges = [
                    e for e in document.edges
                    if e.source_id not in absorbed_ids and e.target_id not in absorbed_ids
                ]
                document.nodes.append(table_node)
                document.edges.append(Edge(type=EdgeType.PARENT_OF, source_id=artifact.id, target_id=table_node.id))
                created.append(table_node.id)
    finally:
        pdf.close()

    if created:
        _resort_pdf_content_nodes(document)

    return created
