"""Shared defaults for the `relations/` modules that call an LLM/VLM.

`propose.py`, `caption_images.py` and `table_structure.py` were each
declaring their own `"gpt-5.6-terra"` literal (with comments noting they'd
been manually "unified" onto the same model on 2026-09-04, see
`docs/vlm-integration-research.md` §9) — centralized here so the three
modules can't drift out of sync again. Each module still exposes its own
per-purpose alias (e.g. `propose.py`'s `HEADING_MODEL`/`MERGE_MODEL`/...,
`table_structure.py`'s `DEFAULT_OPENAI_MODEL`) so call sites keep a
descriptive parameter default; only the underlying value is shared.
"""
from __future__ import annotations

DEFAULT_MODEL = "gpt-5.6-terra"
