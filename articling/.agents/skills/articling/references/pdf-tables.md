# PDF table detection reference (optional, model-gated)

`extract()` never produces `Table` nodes for PDF by default. Opt in either
in one call via `articling.extractors.pdf.extract(..., enrich_tables=True)`,
or after the fact via `relations.table_structure.enrich_pdf_tables` — same
result either way:

```python
from articling.extractors.pdf import extract

doc = extract("report.pdf", enrich_tables=True)  # OPENAI_API_KEY required, pip install "articling[relations]"
```

```python
# equivalent, two steps — useful if you already have a Document and want to add tables later
from articling import extract
from articling.relations.table_structure import enrich_pdf_tables

doc = extract("report.pdf")
created = enrich_pdf_tables(doc, backend="openai")
```

`enrich_tables` only exists on `articling.extractors.pdf.extract` — the
top-level `articling.extract()` dispatcher deliberately stays free of
format-specific options (same reason `capture_dir` isn't there either).

## How it works

```
vector graphics (page.get_drawings()) -> candidate table bboxes
    -> text-density pre-filter (candidate must contain >= 2 text blocks)
    -> crop sent to a model: "is this really a table?" + read the grid
    -> confirmed only -> Table node (absorbed Text/Image nodes removed)
```

The geometric candidate stage is cheap but over-detects — a grid of bordered
image placeholders can look identical to a real data table in pure vector
geometry. The text-density filter cuts most false positives but not all
(a diagram with several icon labels can still pass). **The model step is the
real gate, not the geometry.**

## Two backends — different trust levels

- **`backend="openai"`** (default) — asks `gpt-5.6-terra` for a structured
  `is_table: bool` + grid in one call. Self-gated: tested to correctly say
  "not a table" for non-table crops. Safe to call standalone.
- **`backend="granite_docling"`** — runs `ibm-granite/granite-docling-258M`
  locally (`pip install "articling[pdf-tables-local]"`, needs torch —
  the only place in this project that does; slow on CPU, tens of seconds
  per table). **Not self-gated** — forcing its table-reading prompt on a
  non-table crop can fabricate a plausible-looking fake table instead of
  saying "not a table" (verified empirically). Only use it on crops you
  already trust are tables, or pass `verify_with_openai=True` to gate it
  with the OpenAI check first:

```python
enrich_pdf_tables(doc, backend="granite_docling", verify_with_openai=True)
```

## Lower-level pieces

- `extractors.pdf_tables.detect_table_candidates(page)` — geometry only, no
  model call, returns `TableCandidate(bbox, mode)` where `mode` is `"grid"`
  (bordered cells) or `"ruled_rows"` (horizontal-rule-only "booktabs" style,
  common in papers — no vertical lines at all).
- `relations.table_structure.read_table_grid_openai` /
  `read_table_grid_granite_docling` / `read_table_grid` — read one already-
  cropped image; return `None` when the model determines it isn't a table
  (only `read_table_grid_openai` does this reliably on its own).
- `relations.table_structure.fill_table_grids(doc, backend=...)` — for
  `Table` nodes that already exist but only have a crude text fallback grid
  (e.g. `grid_source == "text_fallback"`), re-read them with a model without
  creating new nodes.
