# Relations reference (LLM/VLM, optional)

`pip install "articling[relations]"`, `OPENAI_API_KEY` required for anything
in this file. Everything here is *additive metadata or edge proposals* on top
of an already-built `ArticDocument` — none of it changes what a deterministic
`extract()` call produces.

## Proposing CAPTION_OF / REFERENCES edges

```python
from articling.relations.propose import propose_edges

proposals = propose_edges(doc)   # doc.edges is NOT touched
doc.edges.extend(p for p in proposals if p.type.value == "CAPTION_OF")  # human picks what to keep
```

Deterministic per-format heuristics (in `extractors/docx.py`/`pptx.py`/
`pdf.py`) already catch explicit "표 "/"그림 " captions immediately before or
after a Table/Image, and — because the extractor can't tell which neighbor a
caption belongs to — may attach the same proposal to both, deliberately
leaving the ambiguity for review. `propose_edges` is for cases the
deterministic heuristic can't reach (e.g. prose that references a table by
number several paragraphs later).

For PDF, XLSX, and PPTX, candidates are accompanied by layout evidence when
available. PDF uses a source-page crop, XLSX renders the relevant cell range,
and PPTX supplies native source-shape reconstruction plus a labeled copy.
Externally attached slide images take precedence. Reconstruction is explicitly
approximate and does not invoke an Office process. XLSX/PPTX candidate Text nodes are ranked by spatial
distance within the same sheet/slide instead of list position alone.

## Resolving ambiguous captions with a VLM

```python
from articling.relations.propose import resolve_ambiguous_captions

resolve_ambiguous_captions(doc)   # mutates doc in place
```

Different trust level from `propose_edges`: this looks at existing ambiguous
`CAPTION_OF` proposals (same caption pointing at two candidates) and asks a
VLM, using the anchor's captured pixels when available (`Table.capture_path`
/ `Image.image_path`), which one is actually grounded — then *removes* the
unconfirmed edge. It never asserts a new claim, so the worst failure mode is
deleting a correct guess, not inventing a wrong one — which is why it's safe
to apply directly instead of returning candidates. If pixels are missing or
the API call fails, it leaves the ambiguity as-is.

## Promoting a heading to be the parent (PARENT_OF, opt-in exception)

```python
from articling.relations.propose import promote_heading_parents

promoted = promote_heading_parents(doc)   # mutates doc in place, returns reparented node ids
```

`PARENT_OF` is otherwise the one edge type in this schema with no LLM
involvement at all (`File → Artifact → content`, deterministic, exactly two
levels). This function is an explicit, opt-in exception to that: for each
Table/Image, if a nearby Text candidate is actually the heading/section title
that content belongs under, the deterministic `Artifact → content` edge is
**removed** and replaced with `heading Text → content` — deepening the tree
by one level. Captions ("Table 1.", "Figure 2.") are explicitly excluded by
the prompt — those aren't section headings.

This is a different trust level again, and the riskiest of the three: unlike
`propose_edges` (additive only) or `resolve_ambiguous_captions` (removal
only), this deletes a deterministic structural edge and asserts a new one
based on an LLM judgment — the worst failure mode is a wrong reparent, not
just a missed or over-broad one. If no real heading is found, the existing
parent can't be located, or the API call fails, the original structure
(Artifact as parent) is left untouched — same partial-failure principle as
`propose_edges`. `scaffold.check_invariants`' "at most one `PARENT_OF` parent
per node" still holds after promotion (the old edge is always removed before
the new one is added).

**`include_text_anchors=True`** extends this from Table/Image to Text itself —
a paragraph or subheading can be reparented under another Text (a real
document outline: heading under heading, paragraph under its section title),
not just content under a heading:

```python
promoted = promote_heading_parents(doc, include_text_anchors=True)
```

Default is Table/Image only, precisely because Text can never be a child
there — only a parent — so a cycle is structurally impossible. Once Text can
be reparented too, Text is on both sides (parent and child), so a cycle is
now reachable (A proposed as B's heading while B is proposed as A's) — the
function checks for that before applying each reparent (walking the current
`PARENT_OF` chain from the candidate heading to see if the anchor is already
its ancestor) and skips any reparent that would close a loop, leaving the
original parent in place. This also means many more anchors (every Text
node, not just Table/Image) — see "Batching and concurrency" below for how
that's kept fast.

For XLSX and PPTX, heading promotion also receives the relevant sheet crop
or supplied/reconstructed full-slide image. This lets it use alignment and visual section boundaries
in addition to heading-like wording. If layout rendering is unavailable, it
falls back to the existing text-only path.

### Batching and concurrency

```python
promoted = promote_heading_parents(doc, include_text_anchors=True, batch_size=25, max_workers=8)  # both are the defaults
```

Confirmed empirically (`docs/vlm-integration-research.md` §14): calling the
model once per anchor took 753s on a 250+-anchor document — mostly fixed
per-request round-trip latency, not judgment time. Anchors without a pixel
(the vast majority once `include_text_anchors=True` is on) are grouped into
batches of `batch_size` and judged in one structured-output call per batch
(a `list[Decision]` schema, each entry tagged with which anchor it's for);
batches run concurrently up to `max_workers` via a thread pool (the OpenAI
client is thread-safe). Anchors with a pixel (Table's `capture_path` /
Image's `image_path`) are still called individually — mixing several images
into one batched call risks the model losing track of which image belongs
to which anchor — but those calls run concurrently too. Judgments are still
independent per anchor (no loss of safety), and the reparenting itself is
still applied in one deterministic sequential pass afterward, in original
document order (the cycle check depends on order). Failures now happen per
batch instead of per anchor, same partial-failure principle. Measured
result: 753s → 98s (~7.7×) on the same document, with identical merge/
reparent outcomes.

## Merging same-line PDF text fragments (PDF only)

```python
from articling.relations.propose import merge_fragmented_text

merged = merge_fragmented_text(doc)   # mutates doc in place, returns ids of the surviving (updated) nodes
```

`extractors.pdf._reorder_same_line_blocks` already puts same-line `Text`
blocks back in left-to-right order (a `page.get_text("dict")` quirk splits
inline math with sub/superscripts across several blocks and can scramble
their order — confirmed on a real paper, see
`docs/vlm-integration-research.md` §11.2) — but it never merges them,
because geometry alone can't tell "one expression split across blocks" from
"two unrelated things that happen to share a line" (also confirmed
empirically: two authors' names on the same line, closer together than some
genuinely-fragmented math). This function asks a model instead: for each
candidate cluster of 2+ `Text` nodes on the same line (only PDF nodes
qualify — they need `bbox`+`page_index`, so this is a no-op on other
formats), it asks whether the fragments are really one expression and, if
so, what the correctly-joined text is. The first (leftmost) node in the
cluster is kept and updated in place (`properties["text"]`, `name`, and a
unioned `bbox`; `properties["merged_from"]`/`["merged_by"]` record what
happened) — its existing edges (e.g. `PARENT_OF`) carry over unchanged, so
no new edges are created. The rest of the cluster's nodes, and any edges
pointing at them, are removed. If the model says "don't merge", the API
call fails, or the answer is empty, that cluster is left exactly as-is
(partial-failure principle, same as `propose_edges`).

Same batching/concurrency as `promote_heading_parents` (see "Batching and
concurrency" above): `batch_size` clusters per call (default 25),
`max_workers` batches concurrently (default 8) —
`merge_fragmented_text(doc, batch_size=25, max_workers=8)`. Order of the
returned ids isn't guaranteed across batches run concurrently; pass
`max_workers=1` if you need it deterministic.

## Merging semantic Text units from the 2D PDF layout (PDF only)

```python
from articling.relations.propose import merge_semantic_text_groups

merged = merge_semantic_text_groups(doc)
```

This builds conservative 2D-neighborhood regions from PDF Text bboxes,
renders each source region with numbered boxes, and asks the VLM to partition
nodes that must be read together as one semantic unit. Geometry only selects
candidates; the model decides grouping, while Articling joins original text
in spatial order without model rewriting. It is the first stage of
`apply_vlm_enrichment`.

## Nesting numbered subsections under their section (no model needed)

```python
from articling.relations.propose import nest_numbered_headings

nested = nest_numbered_headings(doc)   # mutates doc in place, returns reparented heading node ids — no client, no API key
```

`promote_heading_parents` judges each content node's parent independently —
it has no notion that a heading numbered `"3.1"` belongs under the heading
numbered `"3"`. Confirmed empirically: on a real paper, only 2 of 15 numbered
subsection headings ended up under their section after
`promote_heading_parents(include_text_anchors=True)` alone. This function
finds `Text` nodes whose text starts with a numeric heading pattern (e.g.
`"3.1\nEncoder and Decoder Stacks"`) and reparents each one under the
sibling heading matching its immediate parent number — `"3.2.1"` goes under
`"3.2"`, not `"3"`. Since the parent is derived purely from the number
string, this can never create a cycle (unlike `promote_heading_parents`, no
`_is_ancestor` check is needed). A numeric-looking prefix isn't enough by
itself — table data rows like `"1 512 512 5.29 …"` start with a digit too,
so a heading only counts if a letter immediately follows the number (that
row's next character is another digit, so it's correctly ignored). A
subsection whose parent-numbered heading isn't in the document is left
where it is — no parent is invented.

## Reifying sibling groups with no heading in the source (`propose_synthetic_groups`)

```python
from articling.relations.propose import propose_synthetic_groups

created = propose_synthetic_groups(doc)   # mutates doc in place, returns new Group node ids
```

Some sibling content forms one conceptual unit even though the source has no
explicit heading or container for it — several author name/affiliation/email
blocks with no "Authors" heading, several KPI cards with no enclosing box.
Nothing in `PARENT_OF`/`CAPTION_OF`/`REFERENCES` can express "these N siblings
are one thing," so this function creates a new **synthetic** `Group` node
(`properties["synthetic"] = True`, no `text` — a `group_type` label like
`"authors"` is the model's own label, not text copied from the source) and
reparents the members under it: the existing `parent -PARENT_OF-> member`
edges are removed and replaced with `parent -PARENT_OF-> Group -PARENT_OF->
each member`. Because `Group` is a brand-new node, this can never create a
cycle (unlike `promote_heading_parents`, no `_is_ancestor` check is needed).

Candidate regions are deterministic — the current siblings under one parent,
further split by `page_index` where the format provides it (PDF; XLSX/PPTX
already have one Artifact per sheet/slide, so page-splitting is a no-op
there). The model decides whether to group at all, requiring at least two
independent cues (spatial proximity, visual repetition, structural
repetition, shared semantic role, a clear boundary from surrounding
siblings) — the cues it used are kept in `properties["basis"]`, so
synthetic groups can be evaluated for precision/recall separately from the
rest of the graph later. When uncertain the model returns no groups for that
region; a missed group is treated as safer than a false one.

Confirmed on a real paper (`1706.03762`, `gpt-5.6-terra`): the 8 authors on
page 1 were grouped as `group_type="authors"`, confidence 0.99, all 5 cues
present — but only once candidate regions were split by `page_index` in
addition to parent. Splitting by parent alone put all ~124 top-level
siblings of the single whole-document PDF Artifact in one region — too big
for the per-page layout crop to render at all, and big enough that the model
missed the authors' pattern (see `docs/vlm-integration-research.md` §15).

**Not included in `apply_vlm_enrichment`** — like `promote_heading_parents`,
this creates and reparents structure, so it stays an explicit opt-in call.

## Doing all five at once: `apply_vlm_enrichment`

```python
from articling.relations.propose import apply_vlm_enrichment

apply_vlm_enrichment(doc)   # semantic layout merge, fragment merge, heading promotion, numbered nesting, then edge proposals
```

Convenience wrapper for call sites that just want "use the VLM or not" as one
switch — this is what the CLI's `--vlm-enrichment` flag and the demo app's
"VLM enrichment" checkbox call (`nest_numbered_headings` itself needs no
model, but it's bundled in because it directly closes the gap
`promote_heading_parents` leaves). It does not change the trust levels
above: `propose_edges` still only adds, `promote_heading_parents` still
deletes and reasserts structural `PARENT_OF` edges on a model's say-so,
`nest_numbered_headings` still does the same deterministically,
`merge_semantic_text_groups` and `merge_fragmented_text` still merge nodes
only on a model's say-so. If you only need one of the five, call it directly
instead. Its return value is
`promote_heading_parents`' reparented node ids (unchanged signature) —
merged/re-nested node ids aren't returned but are discoverable via
`properties["merged_from"]`/`properties["nested_by"]` on `document.nodes`,
same as how added `CAPTION_OF`/`REFERENCES` edges are discoverable via
`document.edges` directly.

## VLM-generated descriptions (not the same as captions)

```python
from articling.relations.caption_images import caption_content_nodes

caption_content_nodes(doc)   # OPENAI_API_KEY required, mutates doc's node properties
```

Fills `vlm_description`/`vlm_content_type`/`vlm_confidence` on `Image`/`Table`
nodes — new text the VLM *invented* by looking at pixels, for search/metadata
purposes. This is unrelated to `CAPTION_OF`/`REFERENCES`, which link *existing*
text in the document to a Table/Image. Because it's pure metadata with no
graph-structure impact, it's applied without a human-review step, unlike the
edge proposals above.

## PPTX graph enrichment with external slide images

```python
from articling import extract
from articling.relations.propose import apply_vlm_enrichment

doc = extract("deck.pptx")
apply_vlm_enrichment(doc, slide_images={0: "Slide1.png", 1: "Slide2.png"})
```

Supply every zero-based slide index from the exact source deck, exported as
uncropped PNG/JPEG by PowerPoint. No slide renderer or external converter runs.
The mapping and aspect ratios are checked before any images are attached or API
calls made. Successful attachment stores `slide_image_path` on each Artifact.
Content identity remains the caller's responsibility.

Heading promotion and caption/reference proposals receive original full-slide
pixels plus a copy annotated with `visual_id` labels matching the node summaries.
Synthetic sibling grouping runs last and changes `PARENT_OF` through Group nodes.
The return value remains promoted node IDs; inspect the document for new groups
and relations. These are model-derived changes, not verified source relations.
Existing candidate windows and region-size limits still apply; this does not
recover missing nodes or introduce new relation types. If an attached image later
becomes unreadable, a warning is emitted and native reconstruction is used.
If the source is also missing, available text and individual image inputs remain.

For individual stages, first call
`attach_pptx_slide_images(doc, {0: "Slide1.png", 1: "Slide2.png"})` from
`articling.relations.slide_images`, then call the desired function in `propose`.
OLE WMF/EMF bytes remain preserved but are omitted as isolated VLM image inputs;
the external slide image supplies their visual context.

Native reconstruction reads the source deck once per enrichment stage and caches
clean slides across anchors. `PptxSlideRenderer.render(index)` exposes PNG bytes
and warnings for local inspection. It uses source paint order, transformed shape
corners, picture crop/alpha, text runs, and merged cells; it does not paint graph
preview strings over images. Font substitution and fitted text are approximate.
WMF/EMF, complex geometry/effects, SmartArt, and table theme styles remain limited.
Treat reported omissions as uncertainty, not absence.
