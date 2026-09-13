"""Shared defaults for the `relations/` modules that call an LLM/VLM.

`propose.py`, `caption_images.py` and `table_structure.py` were each
declaring their own `"gpt-5.6-terra"` literal (with comments noting they'd
been manually "unified" onto the same model on 2026-09-04, see
`docs/vlm-integration-research.md` §9) — centralized here so the three
modules can't drift out of sync again. Each module still exposes its own
per-purpose alias (e.g. `propose.py`'s `MERGE_MODEL`/`SEMANTIC_MERGE_MODEL`/
`GROUP_MODEL`/`DISAMBIGUATION_MODEL`, `table_structure.py`'s
`DEFAULT_OPENAI_MODEL`) so call sites keep a descriptive parameter default;
only the underlying value is shared. (`propose_edges`'s own CAPTION_OF/
REFERENCES/HEADING_PARENT judgment and its Text-anchor HEADING_PARENT pass
just use `DEFAULT_MODEL` directly — since 2026-09-12 they're one function,
so there's no longer a separate `HEADING_MODEL` alias to keep in sync.)
"""
from __future__ import annotations

DEFAULT_MODEL = "gpt-5.6-terra"
