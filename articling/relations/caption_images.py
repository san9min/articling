"""Fills Image/Table nodes with a VLM-generated description — a completely
different mechanism from CAPTION_OF/REFERENCES proposals, don't confuse the
two:

- **This module (generation)**: a VLM **looks at the image/table capture
  pixels and invents new text**, filling it into that node's own
  `properties`. Unrelated to any text already in the document — a
  description made up from scratch.
- **`propose.py` (linking, CAPTION_OF/REFERENCES)**: judges whether a Text
  node **already in the document** is a caption describing a particular
  Table/Image, and draws an **edge between the two**. Creates no new text.

That's why the property here is named `vlm_description`, not `vlm_caption`
— using the same word as `EdgeType.CAPTION_OF` would make it unclear which
one is being talked about.

`propose.py` (CAPTION_OF/REFERENCES) is a proposal that changes the graph's
**structure**, so it needs human review, but the `vlm_description`/
`vlm_content_type` filled in here is **pure supplementary information**
attached to an already-final node — even on failure (an empty value), the
graph structure itself stays intact. So this is applied directly to the
`document`'s node properties, not returned as a proposal.

Targets: only nodes whose `properties` has a pixel path.
- An `Image` node's `image_path` (filled by extractors/docx.py·pptx.py·xlsx.py)
- A `Table` node's `capture_path` (extractors/xlsx.py's visual capture)

The default model is `gpt-5.6-terra` — `relations/propose.py` has also been
unified onto the same model since 2026-09-04 (see
`docs/vlm-integration-research.md` §9).

This module can be imported without the `openai` package (an optional
dependency), but it's required when `caption_content_nodes` is actually
called — so a user who only uses the extractors isn't forced into an
unneeded dependency (the same reason as `propose.py`).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .._image_util import encode_image_data_url
from ..schema import ArticDocument, Node, NodeType

DEFAULT_MODEL = "gpt-5.6-terra"

_INSTRUCTIONS = """\
Create a new searchable description for an image node stored in a document
graph database (Neo4j). Do not look for an existing caption in the document;
describe the supplied image itself. The image comes from an engineering or
quality-review document.

Rules:
- description: one Korean sentence, preferably no more than 40 characters.
  Describe only visible content and do not speculate. Include visible
  annotations such as arrows, numbers, or dimensions (for example, an arrow
  marking a 20 mm gap).
- content_type: exactly one of "photo", "table_capture", "diagram", "chart",
  "screenshot", or "other".
- confidence: a value from 0.0 to 1.0 representing confidence in the description.
"""


class ImageDescription(BaseModel):
    description: str = Field(description="A one-sentence Korean description of visible content.")
    content_type: str
    confidence: float


def _captionable_nodes(document: ArticDocument) -> list[tuple[Node, str]]:
    """A list of (node, pixel path) pairs. `image_path` for Image, `capture_path` for Table."""
    out: list[tuple[Node, str]] = []
    for n in document.nodes:
        if n.type == NodeType.IMAGE and n.properties.get("image_path"):
            out.append((n, n.properties["image_path"]))
        elif n.type == NodeType.TABLE and n.properties.get("capture_path"):
            out.append((n, n.properties["capture_path"]))
    return out


def caption_content_nodes(
    document: ArticDocument, client=None, model: str = DEFAULT_MODEL
) -> list[str]:
    """Fills `vlm_description`/`vlm_content_type`/`vlm_confidence`
    properties onto Image/Table nodes in `document.nodes` that have a pixel
    path. Modifies `document` **in place** (unlike propose_edges — see the
    module docstring above).

    Returns: the list of node ids that got a description filled in (for
    logging/verification). A node with no pixel file, or whose VLM call
    failed, is silently skipped (this is supplementary info with no impact
    on graph structure, so an exception doesn't stop the whole run) — to
    know the failure count, compare the returned id list against
    `len(_captionable_nodes(document))`.

    If `client` isn't given, `openai.OpenAI()` is built with its default
    constructor (needs the `OPENAI_API_KEY` environment variable).
    """
    if client is None:
        from openai import OpenAI  # lazy import — openai isn't needed unless this function is called

        client = OpenAI()

    updated: list[str] = []
    for node, pixel_path in _captionable_nodes(document):
        if not Path(pixel_path).exists():
            continue
        try:
            response = client.responses.parse(
                model=model,
                instructions=_INSTRUCTIONS,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Describe this image according to the instructions."},
                        {"type": "input_image", "image_url": encode_image_data_url(pixel_path), "detail": "high"},
                    ],
                }],
                text_format=ImageDescription,
            )
        except Exception:  # noqa: BLE001 — e.g. an API error/rate limit, skip just this node
            continue
        parsed = response.output_parsed
        if parsed is None:
            continue
        node.properties["vlm_description"] = parsed.description
        node.properties["vlm_content_type"] = parsed.content_type
        node.properties["vlm_confidence"] = parsed.confidence
        updated.append(node.id)
    return updated
