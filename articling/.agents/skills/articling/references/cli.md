# CLI reference

```bash
python -m articling.cli PATH [--format json|cypher] [-o OUTPUT] [--vlm-enrichment] [--slide-images MANIFEST.json] [--check]
```

- `PATH` — `.docx`, `.pptx`, `.xlsx`, or `.pdf`. Format is picked from the
  extension; there is no `--pipeline`-style override (unlike docling).
- `--format json` (default) — `ArticDocument` as lossless JSON
  (`export.json_export.to_json`).
- `--format cypher` — a full Cypher script (`CREATE` statements + optional
  uniqueness constraints) via `export.neo4j.to_cypher_script`.
- `-o/--output` — write to a file; omit to print to stdout.
- Right after extraction, **before** `--vlm-enrichment` is even considered,
  the CLI always runs `relations.propose.nest_numbered_headings(document)` —
  it nests a numbered subsection heading ("3.1 Title") under its section
  ("3 Title") by text-pattern matching alone (a different, hierarchical
  "N.M" pattern from the flat "1)/2)/3)..." run below). This needs no
  VLM/API key/`relations` extra, so it's unconditional and applies to every
  format, not just DOCX — the same treatment
  `extractors.pdf_figures.enrich_pdf_figures` gets in the demo app.
- `--vlm-enrichment` — after extraction, run
  `relations.propose.apply_vlm_enrichment(document)`, which (in order)
  first merges semantic PDF `Text` units from numbered 2D layout regions
  (`merge_semantic_text_groups`), then same-line fragments
  (`merge_fragmented_text`; both PDF-only and no-op on other formats), then
  for each Table/Image judges CAPTION_OF/REFERENCES **and** HEADING_PARENT
  together in one call (`propose_edges(include_text_anchors=True)`) —
  CAPTION_OF/REFERENCES proposals are appended to `document.edges`, while a
  HEADING_PARENT pick reparents Table/Image (and, since
  `include_text_anchors=True`, every paragraph Text too, via its own
  separate batched pass) under a detected heading Text in place. A flat
  "1)/2)/3)..." enumerated-sibling run is collapsed into **one**
  HEADING_PARENT question — asked using the run's first (heading-closest)
  member's own candidate window — rather than one independent question per
  member that a small candidate-window limit could push out of range for a
  later member (or that could simply disagree with an earlier one); the
  single answer is then applied to every member. Finally it re-runs
  `nest_numbered_headings` — a no-op wherever the earlier, unconditional
  call above already put a node under the right parent, but this second
  pass catches a numbered heading that HEADING_PARENT just moved to a
  different section, which the first (pre-VLM) call couldn't see yet.
  Requires `OPENAI_API_KEY` and `pip install "articling[relations]"` (the
  bundle's other three steps need it even though `nest_numbered_headings`
  itself doesn't). Heading reparenting rewrites deterministic `PARENT_OF`
  structure and fragment merging removes nodes — flag both as model-derived
  when reporting results, same as the CAPTION_OF/REFERENCES proposals (not
  confirmed edges). Need only one part of this? Call `merge_fragmented_text`,
  `propose_edges` (with or without `include_text_anchors`),
  or `nest_numbered_headings` directly from the
  SDK instead — see [relations.md](relations.md).
- `--slide-images MANIFEST.json` — requires `--vlm-enrichment` and PPTX input.
  A JSON object maps every zero-based slide index to a PNG/JPEG filename, relative
  to the manifest, e.g. `{"0": "Slide1.png", "1": "Slide2.png"}`. Full-slide
  pixels and labeled copies inform heading/relationship decisions; synthetic
  sibling grouping runs last. Images must come from the exact same deck.
- `--trace-dir DIRECTORY` — requires `--vlm-enrichment`. Save opt-in local
  request/image evidence, parsed responses (including null/API error type),
  candidate IDs, heading application/skip reasons, proposals and graph snapshots
  in a unique run directory. Contains document content, not client credentials.
- `--check` — run `scaffold.check_invariants` and print any problems
  (dangling edges, `PARENT_OF` fan-in > 1 or cycles, `NEXT` cycles/branching) to
  stderr. Does not fail the run; it's a diagnostic.

There is no CLI entry point for Neo4j push (`push_to_neo4j` requires a live
`bolt://` connection with credentials) — do that from the Python SDK, see
[neo4j.md](neo4j.md).

Package installs the console script `articling` too, equivalent to
`python -m articling.cli` (see `[project.scripts]` in `pyproject.toml`).
