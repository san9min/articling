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

from pathlib import Path

from .schema import ArticDocument, Edge, EdgeType, Node, NodeType

__all__ = ["ArticDocument", "Edge", "EdgeType", "Node", "NodeType", "extract"]
__version__ = "0.1.0"

_EXTRACTORS_BY_SUFFIX = {".docx", ".pptx", ".xlsx", ".pdf"}


def extract(path: str | Path) -> ArticDocument:
    """Pick the right extractor by file extension and run it. (Only
    `.docx`/`.pptx`/`.xlsx`/`.pdf` are supported — legacy binary formats like
    `.doc`/`.ppt`/`.xls` aren't yet.)

    If you need format-specific options (e.g. xlsx/pdf's `capture_dir`),
    call that extractor directly, e.g. `articling.extractors.xlsx.extract`.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from .extractors.docx import extract as _extract
    elif suffix == ".pptx":
        from .extractors.pptx import extract as _extract
    elif suffix == ".xlsx":
        from .extractors.xlsx import extract as _extract
    elif suffix == ".pdf":
        from .extractors.pdf import extract as _extract
    else:
        raise ValueError(f"Unsupported file extension: {suffix!r} (supported: {sorted(_EXTRACTORS_BY_SUFFIX)})")
    return _extract(path)
