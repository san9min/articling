# Python SDK reference

## Basic conversion

```python
from articling import extract

doc = extract("report.xlsx")   # -> ArticDocument, dispatched by suffix
print(len(doc.nodes), len(doc.edges))
```

`extract()` only dispatches on `.docx`/`.pptx`/`.xlsx`/`.pdf`. For
format-specific options (e.g. XLSX/PDF `capture_dir` for rendered table/image
crops), call the extractor module directly:

```python
from articling.extractors.xlsx import extract as extract_xlsx

doc = extract_xlsx("report.xlsx", capture_dir="captures/")
```

## `ArticDocument` shape

```python
doc.source_path   # str
doc.format        # str, e.g. "xlsx"
doc.nodes         # list[Node]: id, type (NodeType), name, properties: dict
doc.edges         # list[Edge]: type (EdgeType), source_id, target_id, properties: dict

doc.content_nodes()   # nodes excluding File/Artifact (i.e. Text/Table/Image/Group)
doc.nodes_by_id()     # dict[str, Node]
doc.merge(other_doc)  # concatenate two documents' nodes/edges (ids are file-scoped, so no collision)
```

`Node.properties` is type-dependent free-form data: `Text` carries
`text`/`style`; `Table` carries `grid` (nested, JSON-string-serialized on
Neo4j export) and `capture_path` (rendered PNG, if available); `Image`
carries `image_path` and, for XLSX, position fields like `row`/`col`.
PPTX content nodes carry `slide_index`, native `top`/`left`/`width`/`height`,
and a normalized 0-1000 `bbox` used by layout-aware relation and hierarchy
enrichment. A PPTX `Image` node for an embedded/linked OLE object (e.g. an
Excel worksheet dropped onto a slide) additionally carries
`embed_kind="ole_object"` and `ole_prog_id` (e.g. `"Excel.Sheet.12"`) —
`image_path` there is the object's fallback preview picture, which is
usually just a generic file-type icon (`showAsIcon="1"`), not a data
preview, so don't expect real cell content from it. A PPTX `Image` node also
carries `annotation_shape_count` when a textless shape (a highlight box, an
unglued arrow) was drawn directly on top of the picture — that shape never
gets its own node, but is baked into `image_path`'s pixels instead of being
lost, and the count says how many were composited in. A native PPTX table
also carries `capture_path` (cell shading/merges/borders rendered to PNG),
the same property XLSX tables carry. A native chart becomes a `Table` node
too — `grid` holds its categories/series data (a header row of series
names, then one row per category), plus `chart_type` (the `XL_CHART_TYPE`
member name) and `chart_title` when the chart has a title — but never a
`capture_path`, since it already carries exact data and there's no
chart-drawing renderer to capture from. SmartArt becomes a `Text` node
carrying `shape_kind="smartart"` — its `text` is every label typed into the
diagram (one per line), recovered from the
diagram's data model since neither python-pptx nor this package can render
SmartArt's layout.

## Exporting

```python
from articling.export.json_export import to_json, write_json
from articling.export.neo4j import to_cypher_script, push_to_neo4j

to_json(doc)                       # -> str
write_json(doc, Path("out.json"))  # -> Path

to_cypher_script(doc)                                        # -> str, no connection needed
push_to_neo4j(doc, uri="bolt://localhost:7687",
               auth=("neo4j", "password"))                   # requires pip install "articling[neo4j]"
```

## Validating the graph

```python
from articling.scaffold import check_invariants

problems = check_invariants(doc.nodes, doc.edges)  # [] == fine
```

Checks: no dangling edges, `PARENT_OF` fan-in <= 1 per node (except `File`),
`NEXT` forms a single non-branching, acyclic chain per artifact sequence.

## Relation proposal and VLM captioning

See [relations.md](relations.md) for `propose_edges` (judges
CAPTION_OF/REFERENCES and, with `include_text_anchors`, HEADING_PARENT
reparenting — for a Table/Image anchor, both are judged in one VLM call; a
flat "1)/2)/3)..." enumerated-sibling run is also collapsed into one
HEADING_PARENT question, asked using the run's first member's own candidate
window, rather than one independent question per member),
`resolve_ambiguous_captions`,
`merge_semantic_text_groups` (PDF-only, merges 2D layout-based semantic Text units),
`merge_fragmented_text` (PDF-only, merges same-line `Text` fragments),
`nest_numbered_headings` (no model needed — nests "3.1" under "3" by text
pattern), `propose_synthetic_groups` (creates synthetic `Group` nodes for
sibling content with no heading in the source, e.g. an author roster),
`apply_vlm_enrichment` (runs four of these together — not
`propose_synthetic_groups`, which stays a separate opt-in call), and
`caption_content_nodes` — all optional, most `OPENAI_API_KEY`-gated
(`pip install "articling[relations]"` — `nest_numbered_headings` is the one
exception, needing neither). `propose_edges`'s CAPTION_OF/REFERENCES half
leaves `doc` untouched (returns candidates for the caller to `.extend()`);
its HEADING_PARENT half mutates `doc` in place directly, same as the rest
of the mutating functions below — `propose_edges`/`propose_synthetic_groups`/
`apply_vlm_enrichment` are the riskiest since they delete and reassert (or,
for `propose_synthetic_groups`, insert) structural `PARENT_OF` edges, not
just add or remove proposals.

PDF Text nodes preserve `pdf_text_lines` (original line/span text, bbox,
baseline origin, direction and typography in PDF points) and
`pdf_block_numbers`. Extraction can join tightly adjacent horizontal fragments
before graph creation, marked `native_merged_by="same_line_continuation"`.
This uses no API; node bbox coordinates still use the normalized 0–1000 scale.

`propose_edges(..., trace_dir="/tmp/articling-traces")` and
`apply_vlm_enrichment(..., trace_dir="/tmp/articling-traces")` optionally save
local diagnostic evidence in a unique run directory. `calls/*/call.json`
retains model/instructions/input/schema and parsed output, including nulls
and API error types. Image previews sit beside each call. For relation and
heading calls, `anchors` maps anchor indices and ordered candidate indices
to node IDs. `heading-outcomes/` explains applied or rejected parent changes;
`heading-groups.json` lists collapsed sibling runs with the representative first.
`relation-proposals.json` separates returned candidates from applied edges.
`before.json`, `relations-before.json`, `relations-after.json`, and `after.json`
use the ordinary `ArticDocument` schema. Bundle tracing also captures merge
and synthetic-group API requests. Trace files include document content;
client credentials and exception messages are not serialized. No extra API
calls are made, and tracing is disabled by default.
