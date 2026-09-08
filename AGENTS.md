# Articling

This file provides guidance to AI coding agents when working with code in this
repository.

## Project overview

Articling is a Python SDK and CLI that converts DOCX, PPTX, XLSX, and PDF files
into a unified `ArticDocument` **graph** representation (File/Artifact/Text/
Table/Image nodes, PARENT_OF/NEXT/CAPTION_OF/REFERENCES edges) for downstream
Neo4j / GraphRAG workflows.

## Generalization principle

Work through a continuous Specific → Working Solution → Generalization loop:

1. **Make the concrete case work first.** Start from the actual document,
   failure case, or desired outcome.
2. **Do not generalize too early.** Avoid introducing new abstractions,
   frameworks, or layers based only on hypothetical future needs.
3. **Generalize from repeated evidence.** After solving a case, separate
   input-specific logic from patterns that recur across multiple real cases.
4. **Add an abstraction when repeated custom logic appears.** If the same
   workaround, branching logic, or implementation pattern is needed more
   than once, consider moving it into an existing shared mechanism (e.g.
   `scaffold.py`, `schema.py`) or the smallest reusable abstraction.
5. **Prefer extending existing abstractions over creating new ones.** New
   abstractions should be introduced only when the current model cannot
   represent the recurring pattern cleanly.
6. **Do not sacrifice correctness for generality.** A generic solution that
   handles real cases less accurately is not an improvement.

Continuously ask: How do we make this case work correctly? What repeated
pattern, if any, should become reusable? The goal is for each solved case to
reduce the amount of custom work required for the next similar case.

## Project structure

```text
articling/                      # main Python package
articling/schema.py              # Node/Edge/ArticDocument — the core contract
articling/scaffold.py            # deterministic File/Artifact/PARENT_OF/NEXT + invariant checks
articling/extractors/            # one module per format: docx.py, pptx.py, xlsx.py, pdf.py
articling/capture/               # XLSX table/number-format rendering (openpyxl+PIL)
articling/relations/             # LLM-proposed CAPTION_OF/REFERENCES edges, VLM captioning
articling/export/                # ArticDocument -> JSON / Cypher / Neo4j
articling/.agents/skills/articling/  # usage skill shipped inside the package (see below)
tests/                            # pytest suite and fixtures
docs/                             # assets referenced from README.md (diagrams, etc.)
runs/                             # sample outputs from real documents
```

## Skills

- **Development skills** (for contributors working *on* Articling) live in
  [`.agents/skills/`](.agents/skills/) at the repo root, e.g.
  `articling-conventions`.
- **Usage skills** (for agents *using* Articling to turn documents into
  graphs) are shipped inside the package at
  [`articling/.agents/skills/articling/`](articling/.agents/skills/articling/SKILL.md).
  They are packaged into the wheel/sdist (see `[tool.setuptools.package-data]`
  in `pyproject.toml`) so they are discoverable once `articling` is installed.
  Keep them in sync with the CLI, the SDK (`extract()`), the export layer, and
  the optional-dependency extras when user-facing behavior changes.

## Key commands

```bash
pip install -e ".[dev,relations,neo4j]"   # install with test + optional deps
pytest                                     # run the test suite
python -m articling.cli report.xlsx        # smoke-test the CLI
```

There is no linter/formatter wired up yet — match the surrounding style
(type-hinted, `from __future__ import annotations`, `pathlib.Path`, module
docstrings that explain *why*, not just *what*).

## Code standards

- Keep public APIs typed and compatible with Python 3.10+.
- `ArticDocument`/`Node`/`Edge` in [`schema.py`](articling/schema.py) are the
  one stable, serialized contract — every extractor and export function is
  built around it. Do not add parallel ad-hoc dict-based document shapes.
- Each format extractor (`extractors/docx.py`, `pptx.py`, `xlsx.py`, `pdf.py`)
  exposes a single `extract(path) -> ArticDocument` and owns its own
  format-specific quirks (see Experiments.md "Format-specific logic" for the
  list of real-world fixes already encoded there). New extractors should follow the
  same shape and reuse `scaffold.py` helpers (`file_node`, `parent_edges`,
  `save_image_bytes`) for the deterministic File/Artifact/PARENT_OF/NEXT layer
  instead of reimplementing it.
- `CAPTION_OF`/`REFERENCES` edges split into two trust tiers: deterministic
  heuristics (safe to add directly to `doc.edges`) and LLM-proposed edges from
  `relations/propose.py` (`propose_edges` — return *candidates*, never mutate
  `doc.edges` in place). `resolve_ambiguous_captions` is the one exception
  that mutates in place, because it only *removes* ungrounded candidates
  rather than asserting new ones — preserve that asymmetry when extending
  this area.
- Partial failure in batch/LLM-backed code should skip the failing unit rather
  than aborting the whole run — see `relations/table_structure.py::enrich_pdf_tables`
  (a candidate the model rejects is left as-is, not an aborted run) and
  `relations/propose.py`/`caption_images.py` (a failed API call skips that
  node/group rather than raising).
- Heavy/optional dependencies (`openai`, `neo4j`) stay behind
  `[project.optional-dependencies]` extras; importing `articling` must not
  require them.
- Prefer `pathlib.Path` for path-handling code.
- Avoid `hasattr(...)`/broad `getattr(...)` attribute-probing; if genuinely
  needed for a third-party library quirk, scope it narrowly and comment why.
- Prefer structured models (Pydantic) over loose dictionaries for anything
  that crosses module boundaries or gets serialized (JSON/Cypher/Neo4j).
- Do not add trivial or self-validating tests. Tests should verify real graph
  invariants, format-specific edge cases, or regressions — not restate
  well-established library behavior.

## When making changes

1. Keep edits scoped and consistent with the surrounding module.
2. Update the README and, if user-facing behavior changes (CLI flags, SDK
   signatures, export shape), the packaged usage skill
   ([`articling/.agents/skills/articling/`](articling/.agents/skills/articling/SKILL.md)).
3. Run `scaffold.check_invariants` (or `articling.cli --check`) after changes
   to extractors — PARENT_OF fan-in, NEXT linearity, and dangling edges are
   the graph's structural contract.
4. Run targeted tests for the touched extractor/export path.

## Before finishing

Run `pytest` before considering a task complete. Regenerate anything under
`runs/` only when the change intentionally affects extraction output, and
review the diff carefully — it doubles as real-document regression data.
