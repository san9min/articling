---
name: articling-conventions
description: >
  Conventions for contributing to Articling — the DOCX/PPTX/XLSX/PDF ->
  ArticDocument graph extraction package. Use this skill whenever you add or
  modify an extractor, touch schema.py/scaffold.py, or work on the
  CAPTION_OF/REFERENCES relation-proposal or export layers.
license: MIT
compatibility: Requires Python 3.10+
metadata:
  author: articling contributors
  version: "1.0"
---

# Articling conventions

Articling turns one document into one `ArticDocument`: a typed graph of
`Node`s (`File`/`Artifact`/`Text`/`Table`/`Image`) and `Edge`s
(`PARENT_OF`/`NEXT`/`CAPTION_OF`/`REFERENCES`), defined in
[`articling/schema.py`](../../../articling/schema.py). Everything in this repo
either builds that graph (extractors), enriches it (relations, capture), or
serializes it (export). Read `schema.py`'s module docstring first — it is the
single source of truth for what each node/edge type means.

## The extractor contract

Every format lives in its own module under `articling/extractors/` and
exposes exactly one entry point:

```python
def extract(path: Path, ...) -> ArticDocument: ...
```

- Build the deterministic layer (`File`, `Artifact`, `PARENT_OF`, `NEXT`) with
  `scaffold.py` helpers (`file_node`, `artifact_chain`, `parent_edges`) —
  don't hand-roll it per extractor.
- `NEXT` only ever connects `Artifact` nodes to each other (slide/sheet/page
  order). Content nodes inside one `Artifact` are never linked by `NEXT`.
- Node `id`s are filename-derived and must stay globally unique so that
  `ArticDocument.merge()` can concatenate multiple documents' node/edge lists
  without collision.
- After writing or changing an extractor, run
  `scaffold.check_invariants(doc.nodes, doc.edges)` (or `articling.cli
  --check`) — it catches dangling edges, `PARENT_OF` fan-in > 1, and `NEXT`
  cycles/branching.

## Two trust tiers for CAPTION_OF / REFERENCES

- **Deterministic heuristics** are safe to append straight to `doc.edges` —
  they're cheap and explainable, even when ambiguous (an extractor may
  attach the same heuristic edge to both a preceding and following
  candidate on purpose, leaving disambiguation to review or to
  `resolve_ambiguous_captions`). `scaffold.caption_prefix_edges` (a
  "표 "/"그림 " prefix immediately before a Table/Image → CAPTION_OF) and
  `scaffold.reference_label_edges` (some other Text citing that same
  caption's label by name, e.g. "Figure 1" → REFERENCES on the same anchor
  — the same mechanism a paper's inline "[1]" has to its bibliography
  entry) are this tier.
- **LLM-proposed edges** (`relations/propose.py::propose_edges`) are a
  *corrective* pass over what's left after the deterministic tier — candidates
  only, return them, never mutate `document.edges` in place. Callers decide
  what to keep. Keep REFERENCES's bar high here: a shared topic or a nearby
  sentence is not a reference, only a checkable pointer (a specific quoted
  value/identifier, or an explicit "as shown above" with no label) is — the
  label-citation case is already the deterministic tier's job, don't
  re-propose it.
- `resolve_ambiguous_captions` is the one function that *does* mutate
  `document` in place, and that's deliberate: it only removes an already-
  proposed edge whose anchor isn't grounded by the VLM, never asserts a new
  one. Preserve that asymmetry — "delete a maybe-wrong guess" is a much safer
  default than "invent a new claim" to run without human review.

## Failure handling in batch / LLM-backed code

Code that calls an LLM/VLM per-unit (relation proposal, VLM captioning, PDF
table detection) must not let one failing unit abort the whole run — catch the
per-unit exception and `continue`, leaving that node/candidate untouched, so a
partial result is still usable (see `relations/propose.py`,
`relations/caption_images.py`, `relations/table_structure.py`).

## Dependency boundaries

`pydantic`, `python-docx`, `python-pptx`, `openpyxl`, `Pillow`, `pymupdf` are
required. `openai` (`relations/`) and `neo4j` (`export/neo4j.py` push path)
are optional extras — importing `articling` must succeed without them; only
calling the function that needs them should fail (and only then, with a clear
error).

## Format-specific fixes are load-bearing, not incidental

`extractors/xlsx.py`'s border-detected table ranges, `capture/number_format.py`'s
percent/conditional-format handling, and `extractors/docx.py`'s merged-cell
image dedup exist because real Korean engineering documents break the naive
library-default reading. Don't simplify these away without checking
Experiments.md's "Format-specific logic" section and the corresponding test
in `tests/` for the case they were added to handle.
