# VLM enrichment

[Back to README](../README.md)

## Enable enrichment

Install the `relations` extra and set `OPENAI_API_KEY`, then run:

```bash
pip install -e ".[relations]"
python -m articling.cli deck.pptx --vlm-enrichment --check -o graph.json
```

Enrichment uses visual layout to refine text groups and heading hierarchy and
propose caption/reference relationships. PPTX slides are reconstructed locally;
see [format details and reconstruction limits](extraction-details.md#slide-reconstruction).
For the full workflow and SDK options, see the
[relation enrichment reference](../articling/.agents/skills/articling/references/relations.md).

## Supply slide images

Existing `--slide-images slides.json` remains available with `--vlm-enrichment`:
externally supplied full-slide PNG/JPEGs take precedence over reconstruction.
The JSON maps every zero-based slide index to a path relative to the JSON file,
e.g. `{"0": "slides/Slide1.png", "1": "slides/Slide2.png"}`. Export the exact same
deck without cropping. Slide count and aspect ratio are checked; content identity
remains the caller's responsibility. An unreadable supplied image falls back to
native reconstruction. If the source is unavailable too, processing continues
with text and available individual images. Synthetic sibling grouping in the
convenience enrichment bundle remains opt-in through supplied slide images.
API access requires `articling[relations]` and `OPENAI_API_KEY`.

## Diagnostics

### Save VLM traces

For local diagnosis, add `--trace-dir /tmp/articling-traces` (SDK:
`apply_vlm_enrichment(doc, trace_dir=...)` or `propose_edges(doc, trace_dir=...)`).
Each run saves actual model inputs, image previews, parsed responses/nulls/API
error types, heading candidate ID mappings, application/skip reasons, relation
proposals, and graph snapshots. This is opt-in and contains document text and
images; client credentials and exception messages are not recorded. It makes
existing judgments inspectable offline; it does not replay API calls.

### Inspect a reconstructed slide

To inspect or save a reconstruction without API access:

```python
from pathlib import Path
from articling.capture.pptx_capture import PptxSlideRenderer

renderer = PptxSlideRenderer("deck.pptx")
try:
    slide = renderer.render(0)  # zero-based
    Path("slide-1.png").write_bytes(slide.png)
    print(slide.warnings)
finally:
    renderer.close()
```
