# Relations reference (LLM/VLM, optional)

`pip install "articling[relations]"`, `OPENAI_API_KEY` required for anything
in this file. Everything here is *additive metadata or edge proposals* on top
of an already-built `ArticDocument` — none of it changes what a deterministic
`extract()` call produces.

## Proposing CAPTION_OF / REFERENCES / HEADING_PARENT edges

```python
from articling.relations.propose import propose_edges

proposals = propose_edges(doc)   # CAPTION_OF/REFERENCES only — doc.edges is NOT touched for these
doc.edges.extend(p for p in proposals if p.type.value == "CAPTION_OF")  # human picks what to keep
```

For each Table/Image anchor, `propose_edges` judges three roles a nearby
Text candidate can have — CAPTION_OF, REFERENCES, and HEADING_PARENT — in
**one VLM call**, since all three are answered by looking at the same
anchor and the same nearby candidates. CAPTION_OF/REFERENCES and
HEADING_PARENT are applied very differently, though (see "HEADING_PARENT:
reparenting" below) — this function's return value is CAPTION_OF/REFERENCES
proposals only; HEADING_PARENT picks are applied directly.

Deterministic per-format heuristics (`scaffold.caption_prefix_edges`,
`scaffold.reference_label_edges`, wired into `extractors/docx.py`/`pptx.py`/
`pdf.py`/`xlsx.py`) already catch two explicit-marker cases for free, with
no VLM call: a "표 "/"그림 " caption immediately before or after a
Table/Image (CAPTION_OF — and because the extractor can't tell which
neighbor a caption belongs to, it may attach the same proposal to both,
deliberately leaving the ambiguity for review), and any other text
elsewhere in the document that cites that same caption's label by name,
e.g. "Figure 1" (REFERENCES — the same mechanism a paper's inline "[1]" has
to its bibliography entry, no proximity required). `propose_edges`'s
CAPTION_OF/REFERENCES judgment is a *corrective* pass for what's left once
those explicit markers are already resolved: a caption with no label
prefix, or a reference that points at an anchor's specific content (a
quoted value/identifier, or an explicit "as shown above") with no numbered
label at all. A shared topic or a nearby sentence is still not enough for
REFERENCES — see `_INSTRUCTIONS` in `propose.py` for the exact bar.
HEADING_PARENT has no such deterministic pre-filter for a Table/Image
anchor (a numbered Text subheading is the one case `nest_numbered_headings`
already catches without a VLM — see below) — it's judged on its own merits,
but still defaults to NONE when unsure.

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

## HEADING_PARENT: reparenting (PARENT_OF, opt-in exception)

```python
from articling.relations.propose import propose_edges

propose_edges(doc)   # judges CAPTION_OF/REFERENCES/HEADING_PARENT for Table/Image; HEADING_PARENT picks are applied to doc in place
```

`PARENT_OF` is otherwise the one edge type in this schema with no LLM
involvement at all (`File → Artifact → content`, deterministic, exactly two
levels). HEADING_PARENT is an explicit, opt-in exception to that: for each
Table/Image, if a nearby Text candidate is actually the heading/section title
that content belongs under, the deterministic `Artifact → content` edge is
**removed** and replaced with `heading Text → content` — deepening the tree
by one level. Captions ("Table 1.", "Figure 2.") are explicitly excluded by
the prompt — those aren't section headings. This role is judged in the same
call as CAPTION_OF/REFERENCES (see above) but applied completely
differently — it's the riskiest of the roles `propose_edges` judges, and
one of the riskiest things this project does at all: unlike CAPTION_OF/
REFERENCES (additive only) or `resolve_ambiguous_captions` (removal only),
it deletes a deterministic structural edge and asserts a new one based on
an LLM judgment — the worst failure mode is a wrong reparent, not just a
missed or over-broad one. If no real heading is found, the existing parent
can't be located, or the API call fails, the original structure (Artifact
as parent) is left untouched — same partial-failure principle as the rest
of `propose_edges`. `scaffold.check_invariants`' "at most one `PARENT_OF`
parent per node" still holds after reparenting (the old edge is always
removed before the new one is added). A reparented anchor's new PARENT_OF
edge carries `properties["reparented_from"]` (the old parent's id) — since
HEADING_PARENT isn't part of this function's return value (unlike CAPTION_OF/
REFERENCES), that's how a caller finds every reparent it made:

```python
reparented_ids = [e.target_id for e in doc.edges if e.type.value == "PARENT_OF" and "reparented_from" in e.properties]
```

**`include_text_anchors=True`** extends HEADING_PARENT judgment from
Table/Image to Text itself — a paragraph or subheading can be reparented
under another Text (a real document outline: heading under heading,
paragraph under its section title), not just content under a heading:

```python
propose_edges(doc, include_text_anchors=True)
```

For PPTX, Text anchors receive every other Text on the same slide, ordered
by spatial proximity. The `window` limit does not truncate these candidates:
on a two-column PBA review slide, a page number and preceding section blocks
otherwise pushed the slide title out of the last two sections' windows.
Candidates never cross slide boundaries, and the VLM still judges ownership
independently for each block. XLSX Text anchors retain their bounded
reading-order windows and enumerated-sibling collapsing.

DOCX is different: when Word exposes outline levels, the extractor builds
the heading tree deterministically from those levels before any VLM pass.
Cached table-of-contents entries are marked as navigation text and excluded
from section ownership; unique internal bookmark links become deterministic
REFERENCES edges. VLM proposals do not overwrite these native structural
edges.
Automatic Word lists also retain their native `numId` and `ilvl` on Text
nodes. Enumerated-sibling collapsing can therefore recognize list members
even when the displayed number is not present in `paragraph.text`.
`numId=0` removes numbering and is not a list identity. Collapsing requires
the same Artifact and current parent, and stops at explicit outline/TOC
boundaries. Shared numbering is not enough to bridge a new section.
PDF follows the same native-first rule for document outlines: matched PDF
bookmarks are attached to existing page Text nodes and establish structural
`PARENT_OF` edges between headings. Matching requires the complete title
(optionally preceded by a section number); repeated titles are disambiguated
with the destination position. Unmatched bookmarks contribute only to outline
counts. Body blocks and unlisted sections retain their original parents;
outline metadata alone does not prove their ownership. VLM and numbered-title
heuristics cannot overwrite confirmed `pdf_outline` parents. Internal page
links are not yet extracted as graph references.

The heading instructions choose the nearest enclosing semantic level: a
title governing a whole content region can own its top-level sections,
and a block containing a heading and its body is judged as one section.
Physical containers are distinct from semantic headings; formatting or
markers alone cannot establish ownership. A PBA review regression
showed that offering the title alone was insufficient: the model rejected
it as a "slide title rather than a section heading." Clarifying this
distinction restored all four section-to-title links in a real API check.
Page numbers and running headers remain excluded. Failed Text-heading calls
log warnings; enable DEBUG for `articling.relations.propose` to inspect
single-anchor parent choices and their rationales, including null choices.

Default is Table/Image only, precisely because Text can never be a child
there — only a parent — so a cycle is structurally impossible. Once Text can
be reparented too, Text is on both sides (parent and child), so a cycle is
now reachable (A proposed as B's heading while B is proposed as A's) — the
function checks for that before applying each reparent (walking the current
`PARENT_OF` chain from the candidate heading to see if the anchor is already
its ancestor) and skips any reparent that would close a loop, leaving the
original parent in place. This also means many more anchors (every Text
node, not just Table/Image) — and unlike the Table/Image case, CAPTION_OF/
REFERENCES never apply to a Text anchor, so this runs as its own separate,
batched pass rather than joining the one-call-per-anchor path above — see
"Batching and concurrency" below for how that's kept fast.

For XLSX and PPTX, HEADING_PARENT judgment also receives the relevant sheet
crop or supplied/reconstructed full-slide image (for both the Table/Image
call and the Text-anchor pass). This lets it use alignment and visual
section boundaries in addition to heading-like wording. If layout rendering
is unavailable, it falls back to the existing text-only path.

### Batching and concurrency (the `include_text_anchors=True` Text-anchor pass)

```python
propose_edges(doc, include_text_anchors=True, batch_size=25, max_workers=8)  # both are the defaults
```

Confirmed empirically (`docs/vlm-integration-research.md` §14): calling the
model once per anchor took 753s on a 250+-anchor document — mostly fixed
per-request round-trip latency, not judgment time. Text anchors without a
pixel (the vast majority) are grouped into batches of `batch_size` and
judged in one structured-output call per batch (a `list[Decision]` schema,
each entry tagged with which anchor it's for); batches run concurrently up
to `max_workers` via a thread pool (the OpenAI client is thread-safe). Text
anchors with a layout crop are still called individually — mixing several
images into one batched call risks the model losing track of which image
belongs to which anchor — but those calls run concurrently too. Table/Image
anchors always go through individual calls regardless (that's the
one-call-per-anchor path judging CAPTION_OF/REFERENCES/HEADING_PARENT
together, described above), so this batching only ever applies to the
Text-anchor pass. Judgments are still independent per anchor (no loss of
safety), and the reparenting itself (both the Table/Image and Text-anchor
picks) is applied in one deterministic sequential pass afterward, in
original document order (the cycle check depends on order). Failures now
happen per batch instead of per anchor, same partial-failure principle.
Measured result: 753s → 98s (~7.7×) on the same document, with identical
merge/reparent outcomes.

### Enumerated-sibling collapsing

A flat "1) .../2) .../3) ..." run is a special case handled *before* any of
the above batching/individual-call routing: `build_context_windows`'s
`window` bounds each anchor's own candidate list to a handful of nearby
Text nodes — enough for a typical anchor, but a section with more preceding
body lines than the window holds can push its own heading out of a later
run member's candidate list entirely, and no layout image rescues this (the
model can only pick an index from the candidates it was actually given).
Confirmed on a real xlsx document: in a flat "1) .../2) .../3) ..." list,
"1)"'s own window reached the shared heading fine, but "3)"'s own window
was exactly consumed by "1)", "2)", and a sub-bullet before ever reaching
that same heading a few lines further up.

Rather than asking the same underlying question once per member and hoping
they agree (or patching a straggler up after the fact), the run's own
numbering is used *before* any VLM call: the numbering already proves every
member shares one parent, so the whole run is collapsed into a **single**
HEADING_PARENT question, asked using the run's first (and so
heading-closest) member's own candidate window — never a later, worse-
positioned member's — with the answer applied to every member identically.
"2)" and "3)" never become separate anchors at all in this case (they're
still perfectly valid *candidate* text for other anchors' own windows, just
not questions of their own), so there's nothing to disagree with and no
per-member window to run out for them. A run whose first member has no
candidate window of its own (rare) isn't collapsed — each member then falls
back to being judged independently, as if there were no run at all.

### Row-sibling window widening

xlsx's spatial candidate ranking (see "Two different trust levels" above)
means several Table/Image nodes anchored at the exact same row — several
photos (or a small table) side by side illustrating one point, a common
pattern — can each see a *different* HEADING_PARENT window purely because
of which column they happen to sit in. Confirmed on a real xlsx document:
4 such photos, 3 had their section heading rank among their own nearest
spatial neighbors, but the 4th — sharing its column with two unrelated
nearby lines (a numbered item two bullets below, and its own elaboration)
— had those two outrank the heading entirely, silently leaving it
unreparented while its row-siblings resolved fine. (Only confirmed for
Image so far; Table shares the fix on the same reasoning — this project
already treats Table/Image as one class of anchor everywhere else.)

Unlike the enumerated-sibling case above, CAPTION_OF/REFERENCES still needs
judging per-anchor (each photo/table can show something different), so the
anchors can't be collapsed into one question — instead, each anchor's own
candidate list is widened with the union of its same-row siblings' own
candidates (deduplicated): if the heading is close enough to be *any*
sibling's neighbor, every sibling gets a fair shot at it. CAPTION_OF/
REFERENCES eligibility is computed before this widening and never touched
by it, so a candidate borrowed only from a sibling's window still can't
become *this* anchor's own caption/reference.

### Caption/reference exclusion from HEADING_PARENT candidates

A Text already judged — deterministically
(`scaffold.caption_prefix_edges`/`reference_label_edges`, already in
`doc.edges`) or by this same `propose_edges` call's own Table/Image pass
(its returned proposals, not yet applied) — to caption or reference a
Table/Image is dropped from every *other* anchor's HEADING_PARENT candidate
list before the Text-anchor pass calls the model at all. `_HEADING_INSTRUCTIONS`
already tells the model a caption/label "names one object, not a section,"
but that was only ever a prompt-level request — a caption with no
recognizable label prefix (so undetected by the deterministic heuristics)
could still reach the candidate list and get picked. Confirmed on a real
pptx document: a short image caption sitting near an unrelated photo got
chosen as a completely unrelated Text's section heading, purely because
nothing had disqualified it as a candidate.

Reusing this project's own CAPTION_OF/REFERENCES judgment — a narrower,
more reliable question ("does this Text specifically caption this
Table/Image") than the open-ended "is this Text a legitimate section
heading" — is more robust here than adding a new geometric
caption-likeness heuristic (e.g. bbox overlap with a nearby Image): it
needs no per-format tuning, and it mechanically enforces a rule the prompt
already declares rather than inventing a new one. A Text that's excluded
as a *candidate* can still be an anchor itself (it belongs under some
heading too) — it just stops being offered as someone else's.

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

Same batching/concurrency as `propose_edges`'s Text-anchor HEADING_PARENT
pass (see "Batching and concurrency" above): `batch_size` clusters per call (default 25),
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

## Native PDF fragment repair (no model needed)

PDF extraction now performs a narrow physical-fragment repair before any
optional VLM pass: consecutive horizontal single-line blocks can join when
baseline, typography and a small font-relative gap agree, with no intervening
drawing/image. Numeric-only blocks, rotated labels and multi-line groups are
left separate. This does not replace the semantic merge or fragment VLM pass.
`native_merged_by="same_line_continuation"` distinguishes this native repair;
`pdf_text_lines` preserves original line/span text and geometry in PDF points,
and `pdf_block_numbers` records source blocks. The node bbox stays normalized.

## Nesting numbered subsections under their section (no model needed)

```python
from articling.relations.propose import nest_numbered_headings

nested = nest_numbered_headings(doc)   # mutates doc in place, returns reparented heading node ids — no client, no API key
```

`propose_edges`'s HEADING_PARENT judgment considers each content node's
parent independently — it has no notion that a heading numbered `"3.1"`
belongs under the heading numbered `"3"`. Confirmed empirically: on a real
paper, only 2 of 15 numbered subsection headings ended up under their
section after `propose_edges(include_text_anchors=True)` alone. This
function finds `Text` nodes whose text starts with a numeric heading
pattern (e.g. `"3.1\nEncoder and Decoder Stacks"`) and reparents each one
under the sibling heading matching its immediate parent number — `"3.2.1"`
goes under `"3.2"`, not `"3"`. Only preceding headings in the same Artifact
and still-open numbered section are eligible. A repeated number starts a new
section instance; TOC entries are excluded. Native hierarchy and a cycle
check take precedence over the number pattern. A numeric-looking prefix isn't enough by
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
cycle (unlike HEADING_PARENT reparenting, no `_is_ancestor` check is needed).

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

**Not included in `apply_vlm_enrichment`** — like HEADING_PARENT
reparenting, this creates and reparents structure, so it stays an explicit
opt-in call.

## Doing it all at once: `apply_vlm_enrichment`

```python
from articling.relations.propose import apply_vlm_enrichment

apply_vlm_enrichment(doc)   # semantic layout merge, fragment merge, propose_edges (caption/reference + heading), numbered nesting
```

Convenience wrapper for call sites that just want "use the VLM or not" as one
switch — this is what the CLI's `--vlm-enrichment` flag and the demo app's
"VLM enrichment" checkbox call (`nest_numbered_headings` itself needs no
model, but it's bundled in because it directly closes the hierarchical
"N.M" subheading gap `propose_edges`'s HEADING_PARENT judgment leaves — the
flat "1)/2)/3)..." enumerated-sibling gap is instead closed inside
`propose_edges` itself, by collapsing the whole run into one question, see
above). It does not change the trust levels above: `propose_edges`'s
CAPTION_OF/REFERENCES half still only adds, its HEADING_PARENT half still
deletes and reasserts structural `PARENT_OF` edges on a model's say-so,
`nest_numbered_headings` still does the same deterministically,
`merge_semantic_text_groups` and `merge_fragmented_text`
still merge nodes only on a model's say-so. If you only need part of this,
call `propose_edges` (with or without `include_text_anchors`) or the other
functions directly instead. Its return
value is the node ids `propose_edges`'s HEADING_PARENT judgment reparented
(captured right after that call, before `nest_numbered_headings` adds its
own — unchanged signature) — merged/re-nested node ids aren't returned but
are discoverable via `properties["merged_from"]`/`properties["nested_by"]`
on `document.nodes`, same as how added `CAPTION_OF`/`REFERENCES` edges are
discoverable via `document.edges` directly.

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

`propose_edges`'s HEADING_PARENT judgment and its CAPTION_OF/REFERENCES
judgment (judged together for a Table/Image anchor, and `propose_edges`'s
separate Text-anchor pass when `include_text_anchors=True`) all receive
original full-slide pixels plus a copy annotated with `visual_id` labels
matching the node summaries.
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
