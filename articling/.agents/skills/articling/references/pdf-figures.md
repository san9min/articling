# PDF vector figure detection reference (optional, model-free)

`extract()` only turns `type=1` (raster-embedded) blocks into `Image` nodes
for PDF. A figure drawn directly with vector graphics — a plot, a heatmap, a
diagram rendered straight to PDF by matplotlib/TikZ/etc. — has no raster
block at all, so it's silently missing from the graph. Confirmed empirically
on a real paper (`1706.03762`, "Attention Is All You Need"): the attention
heatmap figures were drawn as hundreds of small filled rectangles each, so
`extract()` kept the "Figure N. …" caption `Text` but produced zero `Image`
nodes for them.

```python
from articling.extractors.pdf import extract

doc = extract("report.pdf", enrich_figures=True)  # no API key needed
```

```python
# equivalent, two steps
from articling import extract
from articling.extractors.pdf_figures import enrich_pdf_figures

doc = extract("report.pdf")
created = enrich_pdf_figures(doc)
```

`enrich_figures` only exists on `articling.extractors.pdf.extract` — same
reason `enrich_tables`/`capture_dir` aren't on the top-level dispatcher
(format-specific options require calling the extractor directly).

## How it works — and why it needs no model

```
vector graphics (page.get_drawings()) -> cluster shapes by bbox proximity
    -> candidate = cluster with item_count >= 100 (empirical threshold)
    -> crop the page to the cluster's bbox -> Image node
```

Unlike table detection, there's no structural pattern to match (figures
aren't grid-shaped) — the only reliable signal is density: a real vector
figure in the source measurement had 624–1030 drawing items in one region,
while decorative rules and legitimate table borders on the same document
topped out at 53. That order-of-magnitude gap is why this needs **no model
gate** — `enrich_pdf_tables` needs one because geometry alone can't tell a
bordered-image grid from a real table, but "hundreds of shapes clustered
together" isn't the kind of thing plain decoration produces. Default is
still off (opt-in), same reasoning as `enrich_tables`: it's still a
heuristic judgment call, even a cheap one.

Content already inside a detected region is absorbed the same way XLSX
"Option A" absorbs cell content — existing `Text`/`Image` nodes there are
removed (they're preserved visually in the new crop instead). Caption text
(matching the deterministic `Table `/`Figure `/... prefixes) is **never**
absorbed even if its bbox overlaps the candidate — it needs to stay a
separate top-level node for `propose_edges` to later attach `CAPTION_OF` to
it. A candidate that overlaps an already-existing raster `Image` node
(IoU > 0.5) is skipped to avoid a duplicate capture.

Like `enrich_pdf_tables`, this does **not** re-run the deterministic caption
heuristic for newly created nodes — call `relations.propose.propose_edges`
(or `apply_vlm_enrichment`) afterward if you want `CAPTION_OF` for them.

## Known interaction with table detection

The same density signal that makes a figure candidate easy to spot (many
small shapes clustered together) is exactly what makes
`extractors.pdf_tables.detect_table_candidates` prone to false positives —
confirmed on the same measurement (a heatmap page produced one bogus
`"grid"` table candidate plus dozens of bogus `"ruled_rows"` candidates).
The two detectors don't coordinate: if you run both `enrich_pdf_tables` and
`enrich_pdf_figures` on a document, the same region can end up captured as
both a `Table` and an `Image`. `enrich_pdf_figures` only skips regions that
already overlap an existing raster `Image`, not existing `Table` nodes —
pick an order/precedence yourself if this matters for your document.

## Lower-level pieces

- `extractors.pdf_figures.detect_vector_figure_regions(page)` — geometry
  only, no model call, returns `FigureCandidate(bbox, item_count)`.
