"""Small helper shared by the modules that call a VLM
(`relations/propose.py`, `relations/caption_images.py`).

Encodes a local image file/bytes into the data URL format that the OpenAI
Responses API's `input_image` content block accepts.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path


def encode_image_data_url(path: str) -> str:
    p = Path(path)
    data = p.read_bytes()
    mime, _ = mimetypes.guess_type(p.name)
    mime = mime or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def encode_png_bytes_data_url(data: bytes) -> str:
    """Encode an on-the-fly render (e.g. a PDF page crop) straight to a data
    URL with no need to save it to disk first — used by `propose.py`'s
    layout crops (a single-API-call, throwaway image with no reason to
    persist as a permanent artifact, so it's never written to a file)."""
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
