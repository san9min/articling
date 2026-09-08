"""Bind externally exported slides to native PPTX nodes without a renderer.

Explicit zero-based indices avoid guessing slide order from filenames. The
source deck remains the graph authority; images supply visual evidence only.
"""
from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
import warnings

from PIL import Image, ImageDraw

from ..schema import ArticDocument, Node, NodeType

if TYPE_CHECKING:
    from ..capture.pptx_capture import SlideReconstruction


def attach_pptx_slide_images(document: ArticDocument, images: Mapping[int, str | Path]) -> None:
    """Attach a complete zero-based slide -> PNG/JPEG mapping, atomically.

    Images must be exported from this exact deck, without cropping. Dimensions
    catch aspect-ratio mistakes, but cannot prove image/deck content identity.
    """
    if document.format != "pptx":
        raise ValueError("Slide images require a PPTX document")
    artifacts = {n.properties["slide_index"]: n for n in document.nodes if n.type == NodeType.ARTIFACT}
    if any(type(i) is not int for i in images) or set(images) != set(artifacts):
        raise ValueError(f"Expected exactly these zero-based slide indices: {sorted(artifacts)}")
    validated = {}
    for index, path in images.items():
        path = Path(path).resolve()
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError(f"Slide {index}: expected PNG or JPEG")
            width, height = image.size
            props = artifacts[index].properties
            expected = props["slide_width"] / props["slide_height"]
            if abs(width / height / expected - 1) > 0.01:
                raise ValueError(f"Slide {index}: image aspect ratio does not match the PPTX")
            image.verify()
        validated[index] = str(path)
    for index, path in validated.items():
        artifacts[index].properties["slide_image_path"] = path


def render_slide_evidence(
    document: ArticDocument, anchor: Node, candidates: list[Node], *,
    reconstruction: SlideReconstruction | None = None,
) -> bytes | None:
    """Show untouched pixels above a labeled copy, using prompt visual IDs."""
    index = anchor.properties.get("slide_index")
    artifact = next((n for n in document.nodes if n.type == NodeType.ARTIFACT
                     and n.properties.get("slide_index") == index), None)
    original = None
    reconstructed = False
    if reconstruction is None and artifact is not None and artifact.properties.get("slide_image_path"):
        try:
            with Image.open(artifact.properties["slide_image_path"]) as source:
                original = source.convert("RGB")
        except (OSError, ValueError) as exc:
            warnings.warn(f"Cannot read slide {index} image: {exc}", stacklevel=2)
    if original is None and reconstruction is not None:
        with Image.open(BytesIO(reconstruction.png)) as source:
            original = source.convert("RGB")
        reconstructed = True
    if original is None:
        return None
    annotated = original.copy()
    draw = ImageDraw.Draw(annotated)
    width, height = original.size
    for node in [anchor, *candidates]:
        if node.properties.get("slide_index") != index:
            continue
        box = node.properties.get("bbox")
        if box is None:
            continue
        x0, y0, x1, y1 = [round(box[k] / 1000 * size) for k, size in
                          [("x_min", width), ("y_min", height), ("x_max", width), ("y_max", height)]]
        x0, x1 = max(0, x0), min(width - 1, x1)
        y0, y1 = max(0, y0), min(height - 1, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        label = node.id.rsplit(":", 1)[-1]
        draw.rectangle((x0, y0, x1, y1), outline="#d92323", width=3)
        draw.rectangle(draw.textbbox((x0, y0), label), fill="white")
        draw.text((x0, y0), label, fill="black")
    notes = reconstruction.warnings if reconstructed else []
    footer = 24 if notes else 0
    evidence = Image.new("RGB", (width, height * 2 + 48 + footer), "white")
    evidence.paste(original, (0, 24))
    evidence.paste(annotated, (0, height + 48))
    draw = ImageDraw.Draw(evidence)
    draw.text((4, 4), "PPTX reconstruction (approximate; source text is authoritative)" if reconstructed else "Original full slide", fill="black")
    draw.text((4, height + 28), "Same slide with visual_id labels (not source content)", fill="black")
    if notes:
        draw.text((4, height * 2 + 52), f"Reconstruction limitations: {len(notes)} reported (fonts, fitted text, or unsupported shapes). Missing details are not evidence of absence.", fill="black")
    output = BytesIO()
    evidence.save(output, format="PNG")
    return output.getvalue()
