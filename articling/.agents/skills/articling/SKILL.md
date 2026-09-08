---
name: articling
description: >
  Use Articling to turn a document — DOCX, PPTX, XLSX, or PDF — into a typed
  knowledge graph (File/Artifact/Text/Table/Image/Group nodes; PARENT_OF/NEXT/
  CAPTION_OF/REFERENCES edges) that maps directly onto Neo4j. Use this skill
  whenever you need to graph a document's structure rather than just read its
  text: "turn this report into a graph", "load this spreadsheet into Neo4j",
  "what tables/images does this doc have and how do they relate". Covers the
  `articling` CLI, the Python SDK (`extract()` + `ArticDocument`), the
  Cypher/Neo4j export layer, and the optional LLM/VLM relation-proposal and
  captioning helpers.
license: MIT
compatibility: Requires Python 3.10+
metadata:
  author: articling contributors
  version: "1.0"
allowed-tools: Bash(articling:*) Bash(python3:*) Bash(python:*) Bash(pip:*)
---

# Articling

Articling converts a document — DOCX, PPTX, XLSX, or PDF — into a single
typed representation, the **`ArticDocument`**: a graph of `Node`s (`File`,
`Artifact`, `Text`, `Table`, `Image`, `Group`) connected by `Edge`s (`PARENT_OF`,
`NEXT`, `CAPTION_OF`, `REFERENCES`). `Group` is a synthetic node (`synthetic=True`,
no `text`) for sibling content that forms one conceptual unit with no explicit
heading in the source (an author roster, several KPI cards) — see
`relations.propose.propose_synthetic_groups` below. Unlike a tree-shaped document model, this
graph natively expresses caption and cross-reference relationships and maps
1:1 onto a Neo4j property graph. Reach for Articling whenever the goal is
*structure* — what tables/images exist, how they relate, how artifacts
(slides, sheets, pages) fit together — not just plain text extraction. `NEXT`
currently orders PPTX slides only; other formats' Artifacts are linked to
`File` via `PARENT_OF` without an inter-Artifact order.

PPTX VLM enrichment reconstructs slide evidence from native source shapes with
`python-pptx` and Pillow; no Office app or external converter runs. It preserves
paint order, group transforms, picture cropping/alpha/rotation, text formatting,
and explicit table cell formatting. It labels the result approximate and reports
font substitution, fitted text, and unsupported shapes. For a local PNG use
`PptxSlideRenderer` from `articling.capture.pptx_capture`: `render(0)` returns
`SlideReconstruction(png=..., warnings=...)`; call `close()` after use.

Optionally provide full-slide PNG/JPEGs with
`--slide-images slides.json --vlm-enrichment` or SDK
`apply_vlm_enrichment(doc, slide_images={0: "Slide1.png", ...})`. Indices must
cover every slide. Supplied pixels take precedence and also enable synthetic
sibling grouping in the convenience bundle. Unreadable images fall back to native
reconstruction; if the source is missing too, available text/individual images
remain. Bboxes include nested transforms while raw parent-space EMU stays unchanged.
See [references/relations.md](references/relations.md) for validation and trust tiers.

## The fastest thing that works: the CLI

```bash
python -m articling.cli report.xlsx --format json -o report.json
python -m articling.cli report.pptx --format cypher -o report.cypher
```

Add `--vlm-enrichment` to also run 2D semantic Text grouping + fragment merging + heading promotion +
numbered-subsection nesting + the caption/reference proposer
(`relations.propose.apply_vlm_enrichment`, `OPENAI_API_KEY` required,
`pip install "articling[relations]"`), and
`--check` to validate graph invariants (dangling edges, `PARENT_OF` fan-in,
`NEXT` linearity). See [references/cli.md](references/cli.md) for every flag.

## Choosing how to use Articling

| You need to… | Use | Reference |
|---|---|---|
| Read/convert a file once, from the shell | **CLI** (`python -m articling.cli …`) | [references/cli.md](references/cli.md) |
| Build/inspect the graph programmatically | **Python SDK** (`extract()` + `ArticDocument`) | [references/python-sdk.md](references/python-sdk.md) |
| Push a document's graph into Neo4j | **`export.neo4j`** (`to_cypher_script` / `push_to_neo4j`) | [references/neo4j.md](references/neo4j.md) |
| Add caption/reference edges the extractor missed, or resolve ambiguous ones | **`relations.propose`** (LLM/VLM, human-review by default) | [references/relations.md](references/relations.md) |
| Use XLSX/PPTX spatial layout to improve relation and heading-parent decisions | **`relations.propose.propose_edges` / `promote_heading_parents`** | [references/relations.md](references/relations.md) |
| Get `Table` nodes out of a PDF (`extract()` alone never does) | **`relations.table_structure.enrich_pdf_tables`** (opt-in, model-gated) | [references/pdf-tables.md](references/pdf-tables.md) |
| Get `Image` nodes for PDF figures drawn as vector graphics, not raster (`extract()` alone never does) | **`extractors.pdf_figures.enrich_pdf_figures`** (opt-in, no model needed) | [references/pdf-figures.md](references/pdf-figures.md) |
| Merge PDF `Text` fragments that are really one expression split across blocks (math with sub/superscripts) | **`relations.propose.merge_fragmented_text`** (opt-in, model-gated) | [references/relations.md](references/relations.md) |
| Merge nearby PDF `Text` nodes that form one semantic unit in the 2D layout | **`relations.propose.merge_semantic_text_groups`** (opt-in, model-gated) | [references/relations.md](references/relations.md) |
| Nest numbered subsection headings ("3.1") under their section ("3") — `promote_heading_parents` alone mostly misses this | **`relations.propose.nest_numbered_headings`** (opt-in, no model needed) | [references/relations.md](references/relations.md) |
| Reify sibling content that forms one unit with no heading in the source (author roster, KPI cards) into a synthetic `Group` node | **`relations.propose.propose_synthetic_groups`** (opt-in, model-gated) | [references/relations.md](references/relations.md) |

Rules of thumb:

- **You want a graph / Neo4j** → CLI or SDK, ending in `export.neo4j`.
- **The caption/reference edges look wrong or missing** → `relations.propose`
  proposes candidates for human review; it never silently rewrites the graph
  (the one exception, `resolve_ambiguous_captions`, only *removes* ungrounded
  guesses, never adds new claims).

## Output conventions

- Always report node/edge counts and which node types are involved (a caller
  the file has no `Table` nodes for PDFs is not an error — see "Known
  limitations" below).
- If the user does not specify a format, ask whether they want **JSON**
  (`to_json`, lossless `ArticDocument`) or **Cypher/Neo4j**
  (`to_cypher_script` / `push_to_neo4j`).
- `CAPTION_OF`/`REFERENCES` edges are proposals, not ground truth — say so
  when reporting them, and don't present them as confirmed relationships.

## Known limitations

- `.doc`/`.ppt`/`.xls` legacy binary formats are not supported (OOXML only).
- `extract()` never produces `Table` nodes for PDF by itself — PDF has no
  native table concept. `Text`/`Image` nodes with `page_index` and the
  deterministic `CAPTION_OF` heuristic are still there. Call
  `enrich_pdf_tables()` explicitly to add `Table` nodes (opt-in, requires a
  model call to confirm each candidate is really a table — see
  [references/pdf-tables.md](references/pdf-tables.md)); even then, the
  geometric candidate stage can over-detect (e.g. a grid of bordered image
  boxes), so don't treat every found `Table` as guaranteed correct.
- `extract()` also never produces `Image` nodes for PDF figures drawn as
  vector graphics rather than embedded raster images (plots, heatmaps —
  confirmed on a real paper, see [references/pdf-figures.md](references/pdf-figures.md)).
  Call `enrich_pdf_figures()` explicitly (opt-in, no model needed). It can
  double-count content that `enrich_pdf_tables` also matches — the two
  detectors don't coordinate (see that reference's "Known interaction").
- Inline math with sub/superscripts in PDF text can fragment into several
  `Text` nodes — a `page.get_text("dict")` quirk with vertically offset
  spans. The **order** is fixed for free, always
  (`extractors.pdf._reorder_same_line_blocks` puts same-line blocks back in
  left-to-right order — confirmed on a real paper's formulas, see
  `docs/vlm-integration-research.md` §11.2). Actually **merging** the
  fragments back into one node needs a model call — geometry alone can't
  tell "one expression split across blocks" from "two unrelated things that
  happen to share a line" (confirmed empirically: two authors' names on the
  same line). Call `relations.propose.merge_fragmented_text(doc)` (also part
  of `apply_vlm_enrichment`, `--vlm-enrichment`) to do that judgment call —
  it merges when the model says it's really one expression, and leaves a
  cluster alone (still several small nodes) when unsure or the two things
  are genuinely separate. Without it, treat heavily-mathematical PDF
  paragraphs as several small `Text` nodes to read in sequence.
- LLM-proposed `CAPTION_OF`/`REFERENCES` edges require a human review pass;
  don't treat `--vlm-enrichment` output as final.
