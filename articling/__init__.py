"""articling — a package that reads DOCX/PPTX/XLSX/PDF directly with
format-native libraries (no LibreOffice/VLM needed) and builds a typed graph
(`ArticDocument`) of File/Artifact/Text/Table/Image nodes and
PARENT_OF/NEXT/CAPTION_OF/REFERENCES edges.

    DOCX -> python-docx -> ArticDocument
    PPTX -> python-pptx -> ArticDocument
    XLSX -> openpyxl    -> ArticDocument
    PDF  -> pymupdf     -> ArticDocument (no Table — see extractors/pdf.py)

Quickstart:
    >>> from articling import extract
    >>> doc = extract("report.xlsx")
    >>> doc.nodes[0].type
    <NodeType.FILE: 'File'>

    >>> from articling.export.neo4j import to_cypher_script
    >>> Path("report.cypher").write_text(to_cypher_script(doc))
"""
from __future__ import annotations

import importlib
from pathlib import Path

from .schema import ArticDocument, Edge, EdgeType, Node, NodeType

__all__ = ["ArticDocument", "Edge", "EdgeType", "Node", "NodeType", "extract"]
__version__ = "0.1.0"

# Single source of truth for suffix -> extractor module — used for both
# dispatch and the "supported" list in the error message below, so adding a
# format means editing this one mapping instead of an if/elif chain and its
# error message separately. Each module is imported lazily (only once its
# suffix actually matches) so `import articling` doesn't pull in every
# format's dependencies up front.
_EXTRACTOR_MODULES = {
    ".docx": "articling.extractors.docx",
    ".pptx": "articling.extractors.pptx",
    ".xlsx": "articling.extractors.xlsx",
    ".pdf": "articling.extractors.pdf",
}


def extract(path: str | Path) -> ArticDocument:
    """Pick the right extractor by file extension and run it. (Only
    `.docx`/`.pptx`/`.xlsx`/`.pdf` are supported — legacy binary formats like
    `.doc`/`.ppt`/`.xls` aren't yet.)

    If you need format-specific options (e.g. xlsx/pdf's `capture_dir`),
    call that extractor directly, e.g. `articling.extractors.xlsx.extract`.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    module_name = _EXTRACTOR_MODULES.get(suffix)
    if module_name is None:
        raise ValueError(f"Unsupported file extension: {suffix!r} (supported: {sorted(_EXTRACTOR_MODULES)})")
    return importlib.import_module(module_name).extract(path)
