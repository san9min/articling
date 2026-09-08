"""ArticDocument -> JSON. Since `ArticDocument` is already a Pydantic model,
`document.model_dump_json(indent=2)` is enough for most uses, but this adds a
short helper that writes straight to a file — kept for a consistent usage
pattern with the CLI and the other export module (neo4j.py)."""
from __future__ import annotations

from pathlib import Path

from ..schema import ArticDocument


def to_json(document: ArticDocument) -> str:
    return document.model_dump_json(indent=2)


def write_json(document: ArticDocument, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(to_json(document), encoding="utf-8")
    return out_path
