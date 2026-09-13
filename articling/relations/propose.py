"""LLM/VLM-based CAPTION_OF/REFERENCES/heading-PARENT_OF edge proposals — no
coordinate guessing.

Core design: since extractors/*.py already gives accurate nodes with **real
source text**, this asks only "what's the relationship between these two
already-final nodes" rather than a call that guesses a bbox from pixels —
much cheaper and more stable. A candidate Text node is still given as text
only (no bbox-guessing needed for that).

If the anchor node has a pixel path (Table's `capture_path`, Image's
`image_path`), that capture image is shown to the VLM alongside — accuracy
clearly suffers for an Image judged from its name alone in particular. If
there's no pixel path or the file is gone, it automatically falls back to
text only (no path fails completely). Design basis:
`docs/vlm-integration-research.md` §2 priority 2.

Scope limiting: asking about every possible node pair is a combinatorial
explosion plus heavy noise. For each Table/Image node, only its nearby
neighbors in NEXT order (N before/after) are given as "context candidates,"
and only relationships to those candidates are judged — on the assumption
that a caption/reference/heading appears spatially/positionally nearby.

`propose_edges` judges three independent roles a candidate Text can have
relative to one Table/Image anchor — CAPTION_OF, REFERENCES, and
HEADING_PARENT — in **one VLM call per anchor**, since they're all answered
by looking at the same anchor + the same nearby candidates; asking three
separate questions over the same material would triple the API cost for no
accuracy gain. `include_text_anchors=True` additionally judges HEADING_PARENT
for Text anchors too (a paragraph Text reparented under its own section
heading) — CAPTION_OF/REFERENCES never apply there (a Text doesn't caption
another Text in this schema), so that part runs as its own batched pass; see
"Batching and concurrency" below.

For CAPTION_OF/REFERENCES, `propose_edges` is a **corrective pass**, not the
primary source of either edge type — `scaffold.caption_prefix_edges`/
`reference_label_edges` already catch every case with an explicit marker (a
"표 "/"Table "/... prefix, or a "Figure 1"/"표1" citation elsewhere in the
text) deterministically, for free, before this ever runs. This only exists
for the harder remainder: a description or citation with no such marker at
all. That's why NONE should be the default answer, not CAPTION_OF/REFERENCES
— a case the deterministic heuristic would already have caught is evidence
to lean NONE here, not re-confirm it. HEADING_PARENT has no such
deterministic pre-filter for a Table/Image anchor (unlike a numbered Text
subheading, which `nest_numbered_headings` already catches without a VLM) —
it's judged on its own merits, but still defaults to NONE when unsure, since
a wrong reparenting is worse than leaving the structure unchanged.

edge_type:
- CAPTION_OF: the Text directly describes/names this Table/Image with no
  explicit label prefix ("표 "/"Table "/...) — that case is already handled
  deterministically — but it's still unmistakably a caption for this one
  table/figure and no other (a repeated title/generic name is not enough).
- REFERENCES: the Text points at this exact Table/Image the way a paper's
  inline "[1]" points at its bibliography entry — a specific, checkable
  pointer, not a topical relationship. Only two kinds qualify: (a) it names
  or quotes a value, identifier, or detail that appears in this anchor and
  distinguishes it from any neighboring table/figure, or (b) it explicitly
  says it's pointing at a table/figure ("as shown above", "see the photo
  below") even without a numbered label. A shared subject, a nearby
  sentence, or a table/figure that merely supports the same argument is
  NOT a reference — the label-citation case ("Figure 1", "표1") is already
  covered deterministically and must NOT be re-proposed here.
- HEADING_PARENT: the candidate is the actual section/subsection heading
  the anchor's content belongs under — see `promote_heading_parents`'s old
  docstring criteria (a caption/label is never a heading; a body sentence
  is never a heading). At most one candidate can hold this role per anchor.
  Unlike the other two, this one isn't returned as a proposal — see below.
- NONE: no relationship/role, or a case the deterministic heuristics
  already cover. Choose NONE whenever unsure.

**Two different trust levels inside one call, applied differently**:
CAPTION_OF/REFERENCES are additive-only, so they're returned as *candidates*
(`propose_edges`'s return value) — never written to `document.edges`
directly, exactly as before. HEADING_PARENT is structural — it deletes the
deterministically created `Artifact -> content` PARENT_OF edge and
**reparents** it as `heading Text -> content` (deepening the tree by one
level), the one point in this project where LLM involvement is opened up
for PARENT_OF at all (an explicit opt-in exception to schema.py's "PARENT_OF
is deterministic" principle) — so it's applied directly to `document` in
place, with a cycle check (`_is_ancestor`) before every reparent, and left
untouched whenever ambiguous (more than one, or zero, candidates picked) or
the API call fails. A reparented anchor's new PARENT_OF edge carries
`properties["reparented_from"]` (the old parent's id) so a caller can find
every reparent `propose_edges` performed by scanning `document.edges` after
the call, without a separate return value for it.

This module can be imported without the `openai` package (an optional
dependency), but it's required when `propose_edges` is actually called — so
a user who only uses the extractors isn't forced into an unneeded
dependency.

**`resolve_ambiguous_captions`**: a separate function from `propose_edges`.
The deterministic CAPTION_OF heuristics in docx.py/pptx.py/pdf.py attach an
edge to both sides when the caption text is immediately preceded/followed
by a Table/Image on both sides and it can't tell which (a proposal, not
final) — a VLM narrows that ambiguity by looking at the actual pixels.
Unlike `propose_edges`, it doesn't create a new edge — it **only removes
whichever of the already-existing candidates isn't grounded** (since the
worst failure mode is "removing a correct one," not "inventing one that
doesn't exist," it modifies `document` directly in place) — see the
function's own docstring.

**Heading-PARENT_OF for a Table/Image anchor** (formerly the separate
`promote_heading_parents` function, folded into `propose_edges` on
2026-09-12): `PARENT_OF` as specified by `schema.py` ("hierarchical
parent-child, deterministic, File->Artifact->content") was the one edge
type in this project with the principle of "no LLM proposal" —
HEADING_PARENT makes an **opt-in exception** to that principle, reparenting
`Artifact -> Table/Image` to `heading Text -> Table/Image` (see the
"Two different trust levels" note above for the mechanics). Unlike
CAPTION_OF/REFERENCES (additive only) and `resolve_ambiguous_captions`
(removal only), this is an operation that "deletes a deterministic
structural edge and reasserts a new one based on an LLM judgment," so the
worst failure mode can be "reparenting the structure incorrectly" — hence
it always leaves the original structure (Artifact as parent) untouched when
ambiguous or the API call fails, the same conservatism the standalone
function used to apply.

**`propose_edges(..., include_text_anchors=True)`**: extends HEADING_PARENT
judgment to Text anchors too (e.g. a subheading Text under its parent
heading Text, a body paragraph under its section's heading) — this gets
closer to a real document outline, but since Text can now be both parent
and child, a cycle risk arises (e.g. A proposed as B's heading while B is
simultaneously proposed as A's heading); `_is_ancestor` blocks any reparent
that would create one. CAPTION_OF/REFERENCES are never judged for a Text
anchor (a Text doesn't caption/reference another Text in this schema), so
this runs as a separate batched pass rather than sharing the Table/Image
anchor's one-call-per-anchor path — see "Batching and concurrency" below
for why. Off by default (`include_text_anchors=False`) since the number of
anchors grows from "one per Table/Image" to "one per paragraph," a much
larger opt-in cost.

**`merge_fragmented_text`**: PDF only (a no-op with no clusters caught at
all on other format documents). `extractors/pdf._reorder_same_line_blocks`
only fixes the **order** of several Text nodes on the same line
(overlapping y) without merging them — because pure geometry alone can't
safely tell apart one expression split across several blocks by a
sub/superscript (should be merged) from two unrelated semantic units that
happen to sit side by side at the same height (must not be merged —
confirmed: two authors' names on the same line,
`docs/vlm-integration-research.md` §11.2). This function leaves that
judgment to a VLM — for each same-line candidate cluster, it asks "is this a
split expression fragment, or is it separate," and if the answer is to
merge, it merges that cluster into one Text node (the remaining fragment
nodes / edges pointing at them are removed). If it's ambiguous or the API
call fails, that cluster is left fragmented as-is (the partial-failure
principle).

**`merge_semantic_text_groups`**: instead of limiting to the same line,
gathers PDF Text nodes into nearby regions across the 2D layout, then gives
a VLM the actual page crop with numbered bboxes plus the source text to
partition into complete semantic units. Handles, in a general way, cases
that need both vertical and horizontal placement read together — a
name+affiliation+email, or a title/caption/item split across several
blocks. Geometry only builds candidate regions and doesn't presume a merge;
the model only picks node indices. The final text deterministically joins
the source text in spatial order.

**`propose_synthetic_groups`**: while `merge_semantic_text_groups` judges
"should fragmented text be merged into one," this function handles the
opposite problem — there was no way in the existing schema to express that
several sibling nodes that are each already complete (e.g. 8 author
name+affiliation+email blocks, each already merged into one node by
`merge_semantic_text_groups`) form one conceptual unit, "the authors," even
with no explicit heading or container in the source (confirmed: the 8
authors on page 1 of `1706.03762` stayed as 8 independent Text nodes, and
none of `CAPTION_OF`/`REFERENCES`/`PARENT_OF` could express the
relationship "these 8 are one group"). `schema.py`'s `Group` node
(`synthetic=True`) is responsible for that expression, and this function
proposes creating one — candidates (the list of siblings sharing the same
parent) are built deterministically, and the actual judgment (requiring at
least 2 independent cues among spatial/visual/structural/semantic/boundary
signals) is left to the VLM. Like HEADING_PARENT reparenting, it deletes and
reasserts structural edges, but since what it creates is a **new Group
node** rather than an existing one, there's no cycle risk (no `_is_ancestor`
check needed). Runs in `apply_vlm_enrichment` for PPTX with attached slide images;
otherwise opt-in. See the function's own docstring.

**Batching and concurrency** (2026-09-05, empirical basis in
`docs/vlm-integration-research.md` §14): `merge_fragmented_text` and
`propose_edges`'s Text-anchor HEADING_PARENT pass (`include_text_anchors=True`,
one per paragraph) were built as one API call per judgment unit
(cluster/anchor), which took 753 seconds on a document with 250+ anchors —
mostly a fixed per-request round-trip delay rather than judgment time. Now
several items are judged in one batch inside a single structured-output
call (`batch_size`, default 25 — a tradeoff between reducing round trips
and "Lost in the Middle" degradation), and batches run concurrently via a
`ThreadPoolExecutor` (`max_workers`, default 8). The judgment itself is
still independent per item, so there's no loss of safety — only the unit of
failure grows from one item to one batch. Confirmed on re-measurement: 753s
-> 98s (~7.7x), with no regression in merge/reparent result quality. The
Text-anchor pass keeps individual calls as the one exception, for anchors
that have a pixel (to avoid the confusion of mixing several images into one
batch) — Table/Image anchors always go through individual calls regardless
(they're the one-call-per-anchor path that also judges CAPTION_OF/
REFERENCES, described above), so this batching only ever applies to
`include_text_anchors=True`'s text-only paragraphs.

**Enumerated-sibling collapsing** (inside `propose_edges`'s Text-anchor
pass, not a separate function — see `_propose_text_heading_parents`'s
docstring): `build_context_windows`'s `window` bounds each anchor's own
candidate list to a handful of nearby Text nodes — enough for a typical
anchor, but a section with more preceding body lines than the window holds
can push its own heading out of a later sibling's candidate list entirely,
and no layout image rescues this (the model can only pick an index from the
candidates it was actually given). Confirmed on a real xlsx document: a
flat "1) .../2) .../3) ..." list where "1)"'s own window reached the
heading fine, but "3)"'s own window was exactly consumed by "1)", "2)", and
a sub-bullet before ever reaching that same heading a few lines further up.
Rather than asking the same underlying question once per member and hoping
they agree (or patching a straggler up afterward), the enumeration's own
numbering is used *before* any VLM call: a flat, consecutively-numbered run
is collapsed into a **single** HEADING_PARENT question, asked using the
run's first (and so heading-closest) member's own candidate window, with
the answer applied to every member identically — never a disagreement to
adjudicate, and never a later member's worse-positioned window in play at
all.

**Row-sibling window widening** (`_widen_row_sibling_windows`, inside
`propose_edges`'s Table/Image pass): xlsx's spatial candidate ranking (see
"Two different trust levels" above) means several Table/Image nodes
anchored at the exact same row — a common pattern, several photos (or a
small table) side by side illustrating one point — can each see a
*different* HEADING_PARENT window purely because of which column they
happen to sit in. Confirmed on a real xlsx document: 4 such photos, 3 of
them had their section heading rank among their own nearest spatial
neighbors, but the 4th — sharing its column with two unrelated nearby
lines (a numbered item two bullets below, and its own elaboration) — had
those two outrank the heading entirely, so it was silently left
unreparented while its row-siblings resolved fine. Unlike the
enumerated-sibling case above, CAPTION_OF/REFERENCES still needs judging
per-anchor (each photo/table can show something different), so the
anchors can't be collapsed into one question — instead, each anchor's own
candidate list is widened with the union of its same-row siblings' own
candidates (deduplicated): if the heading is close enough to be *any*
sibling's neighbor, every sibling gets a fair shot at it. CAPTION_OF/
REFERENCES eligibility (`caption_eligible_ids`) is computed before this widening and
never touched by it, so a candidate borrowed only from a sibling's window
still can't become *this* image's own caption/reference.

**Caption/reference exclusion** (`_caption_or_reference_source_ids`,
consumed by `_propose_text_heading_parents`'s `excluded_candidate_ids`): a
Text already judged — deterministically (`caption_prefix_edges`/
`reference_label_edges`, already in `document.edges`) or by this same
call's own Table/Image pass (`proposals`, not yet applied) — to caption or
reference a Table/Image is dropped from every *other* anchor's
HEADING_PARENT candidate list before the Text-anchor pass ever calls the
model. `_HEADING_INSTRUCTIONS` already says a caption/label "names one
object, not a section," but that was only ever a prompt-level request, not
mechanically enforced for a caption with no recognizable label prefix (so
undetected by the deterministic heuristics) — confirmed on a real pptx
document: a short image caption got picked as an unrelated Text's section
heading purely because nothing had disqualified it as a candidate. Reusing
this project's own CAPTION_OF/REFERENCES judgment (a narrower, more
reliable question than the open-ended "is this a legitimate heading") is
more robust than a new geometric caption-likeness heuristic (e.g. bbox
overlap with a nearby Image) — no new per-format tuning, and it mechanically
enforces a rule already declared rather than inventing one.

**`nest_numbered_headings`**: needs no VLM/API, pure text pattern matching.
`propose_edges`'s HEADING_PARENT judgment only considers each content node
individually for "is there a heading-looking text nearby that could be its
parent," with no notion of the hierarchy rule that a numbered subheading
("3.1") should go under its section ("3") — confirmed (`1706.03762`): 11 of
13 numbered subheadings stayed directly under the Artifact with the VLM
step alone.
This function finds text in the form "N.M Title" and deterministically
reparents it under its number's parent section. Since the parent is
determined purely by the number (`"3.1"`'s parent is always `"3"`), this
can never create a cycle structurally, so unlike HEADING_PARENT reparenting
it needs no separate cycle check. To keep a table data row ("1 512 512
...") from being mistaken for a heading just because it starts with a
digit, only a number immediately followed by a letter counts as a heading
(a counterexample confirmed empirically).

**`apply_vlm_enrichment`**: a convenience function that runs
`merge_semantic_text_groups` + `merge_fragmented_text` + `propose_edges`
(with `include_text_anchors`) + `nest_numbered_headings` all at once — used
when a call site (the CLI's `--vlm-enrichment`, the demo app) just wants
"use the VLM or not" as a single switch (`nest_numbered_headings` itself
doesn't need a VLM, but it's always bundled in since it directly fills the
hierarchical-subheading gap HEADING_PARENT reparenting leaves — the flat
enumerated-sibling gap is instead closed inside `propose_edges` itself, see
above). This doesn't erase the trust-level differences described above
(structural merge/reparent vs. additive only, VLM judgment vs. deterministic
pattern) — it just reduces the repetition of calling them together, so
call the original functions directly if you only need some of them. The
order is `merge_semantic_text_groups` -> `merge_fragmented_text` ->
`propose_edges` -> `nest_numbered_headings` — running the merges first is
because it's better for the Table/Image-surrounding context text the other
functions see to be clean rather than fragmented, and putting
`nest_numbered_headings` right after `propose_edges` is to definitively
clean up the subheading relationships the VLM missed. CAPTION_OF/REFERENCES
proposals from `propose_edges` still need an explicit
`document.edges.extend(...)` after
this call — HEADING_PARENT reparenting is already applied by the time
`propose_edges` returns, but the additive proposals never mutate `document`
on their own (same rule as calling `propose_edges` standalone).
"""
from __future__ import annotations

import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Callable, Literal, Sequence

import openpyxl
import pymupdf
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .._geometry import cluster_indices
from .._image_util import encode_image_data_url, encode_png_bytes_data_url
from ..capture.xlsx_capture import capture_table_image
from ..extractors.pdf import _y_overlap_frac
from ..schema import ArticDocument, Edge, EdgeType, Node, NodeType
from ._config import DEFAULT_MODEL
from .trace import HeadingOutcome, ProposalRecord, SiblingGroups, TraceClient, parse_with_context, trace_session

logger = logging.getLogger(__name__)

CONTEXT_WINDOW = 3  # how many content nodes before/after the anchor to offer as candidates

# Batching/concurrency defaults shared by `propose_edges`'s Text-anchor pass
# (`_propose_text_heading_parents`) and `merge_fragmented_text`.
# Confirmed (2026-09-05, `1706.03762`): calling 250+ anchors one by one,
# sequentially, took 753 seconds — mostly a fixed per-request round-trip
# delay (a latency floor) rather than the judgment itself. Several items are
# packed into one structured-output call to cut round trips (batching), and
# batches run concurrently (a thread pool — the openai client is thread-safe)
# to cut the time further.
#
# `_BATCH_SIZE` is a tradeoff between "fewer round trips" and "Lost in the
# Middle" (Liu et al., 2023 — a model is prone to miss items buried in the
# middle of the context). Putting the whole document in one call
# theoretically minimizes round trips but risks less attention per item, so
# rather than picking an extreme with no measurement, a middle value
# (in the 20s-30s) was set as the default.
_BATCH_SIZE = 25
_DEFAULT_MAX_WORKERS = 8  # number of concurrent requests — a default safe within API rate limits


def _chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class _EdgeProposal(BaseModel):
    context_index: int = Field(description="Zero-based index of the node in the candidate list.")
    edge_type: Literal["CAPTION_OF", "REFERENCES", "HEADING_PARENT", "NONE"]
    rationale: str = Field(description="One concise sentence explaining the decision.")


class _EdgeProposalResult(BaseModel):
    proposals: list[_EdgeProposal]


_INSTRUCTIONS = """\
You judge three independent roles a candidate can have relative to one
anchor: CAPTION_OF, REFERENCES, and HEADING_PARENT. For CAPTION_OF and
REFERENCES, you are a corrective pass, not the primary classifier — a
deterministic heuristic already resolves every case with an explicit marker
(a caption starting with a table/figure label prefix, in Korean or English,
e.g. "Table "/"Figure ", and any other text that cites that same label by
name elsewhere, e.g. "Figure 1" or its Korean equivalent with no space
before the number) before you ever see this document. You only run on what's
left for those two roles: candidates with no such marker at all. Assume most
of them are NONE. HEADING_PARENT has no such deterministic pre-filter for a
Table/Image anchor — judge it on its own merits, but a wrong reparenting is
still worse than leaving the structure unchanged, so prefer NONE there too
when unsure.

You receive one anchor node (Table or Image) and a list of nearby candidate
nodes (all Text). Candidates have already been filtered for proximity, but
proximity alone is not evidence of any of the three roles. Some candidates
are marked "(too far for CAPTION_OF/REFERENCES)" — a nearer table/image sits
between that candidate and the anchor, so it can still be HEADING_PARENT but
must never be CAPTION_OF/REFERENCES.

## Repeated document structures
Documents often repeat a pattern such as one table or photo followed by a
short description. A candidate may therefore describe a neighboring anchor
instead. Do not assign CAPTION_OF or REFERENCES merely because a generic title
could fit, such as "measurement results," "specification table," or "sample
photo." Require concrete correspondence in names, values, identifiers, or
content that distinguishes this anchor from its neighbors.

## Roles
- CAPTION_OF: The Text specifically names or describes this Table/Image with
  no explicit label prefix (a leading "Table "/"Figure "/... in either
  language — those are already handled and must NOT be re-proposed here).
  Its concrete title, item names, values, or other details must still
  unmistakably match this anchor's actual content and no neighboring one.
- REFERENCES: Treat this the way a paper's inline "[1]" points at its exact
  bibliography entry, not the way one sentence relates to another by topic.
  A citation of the numbered/lettered label itself (e.g. "Figure 1", in
  either language) is already handled deterministically and must NOT be
  re-proposed here. Assign REFERENCES only when, with no such label present,
  the candidate still (a) names or quotes a specific value, identifier, or
  detail that appears in this anchor and distinguishes it from any
  neighboring table/figure, or (b) explicitly points at "the table/figure"
  as an object ("as shown above", "see the photo below") even without a
  number. Sharing a subject, discussing a related measurement, or
  supporting the same argument is NOT a reference by itself — that is
  exactly the "related, not referencing" looseness to avoid.
- HEADING_PARENT: The candidate is the actual section or subsection heading
  that this anchor's content belongs under, such as a short title like "3.
  Measurement Results." Never a caption/label ("Table 1"/"Figure 2" names
  one object, not a section), and never a body sentence merely above or
  below the anchor — select it only when the anchor is genuinely content of
  the section the candidate names. At most one candidate can be
  HEADING_PARENT for a given anchor; if more than one plausibly qualifies,
  or none clearly does, choose NONE for all of them rather than guessing.
- NONE: None of the above, or the case is already covered by a
  deterministic heuristic. When uncertain, choose NONE. Never infer any of
  the three roles from proximity, shared topic, or narrative plausibility
  alone.

## Requirements
- Return one decision for every candidate, including NONE decisions.
- Base decisions on the actual candidate text and any attached anchor/layout
  images. Explain why the candidate has the role you chose (or came close
  but didn't qualify), and for CAPTION_OF/REFERENCES, why it isn't already
  covered by the deterministic label heuristic.
- When an anchor image is attached, treat its pixels as the primary evidence;
  table-grid previews and names are only supporting context.
"""


def _node_preview(n: Node) -> str:
    if n.type == NodeType.TEXT:
        return n.properties.get("text", n.name)[:200]
    if n.type == NodeType.TABLE:
        # For a table anchor with no pixel (capture_path), this grid preview
        # is the only basis for judgment — too narrow (an earlier version
        # used 3 rows x 6 cols) cuts off the concrete items/values needed to
        # tell repeated tables apart, making the "accepted when it's clear"
        # criterion unverifiable. Give it generously (5 rows x 8 cols),
        # enough for the "concrete correspondence" judgment _INSTRUCTIONS
        # requires.
        grid = n.properties.get("grid", [])
        preview_rows = grid[:5]
        return f"{n.name} | " + " // ".join(" | ".join(row[:8]) for row in preview_rows)
    return n.name  # Image


def _node_layout_hint(n: Node) -> str:
    """A short hint that maps the prompt's text index to a position on the layout image."""
    props = n.properties
    if "page_index" in props and "bbox" in props:
        return f"page={props['page_index']} bbox={props['bbox']}"
    if "slide_index" in props and "bbox" in props:
        return f"node_id={n.id} visual_id={n.id.rsplit(':', 1)[-1]} slide={props['slide_index']} bbox={props['bbox']}"
    if "range" in props:
        return f"cells={props['range']}"
    if "row" in props and "col" in props:
        return f"cell=R{props['row']}C{props['col']}"
    return "layout=unknown"


def _anchor_pixel_path(anchor: Node) -> str | None:
    """The anchor node's capture image path. `capture_path` for Table (xlsx
    table visual capture), `image_path` for Image (filled by
    docx/pptx/xlsx extractors). None if neither is present (e.g. a capture
    failure, or a format that doesn't fill this property yet) — the caller
    automatically falls back to the text-only path."""
    if anchor.type == NodeType.TABLE:
        return anchor.properties.get("capture_path")
    if anchor.type == NodeType.IMAGE:
        path = anchor.properties.get("image_path")
        # Native OLE previews remain WMF/EMF; these are not VLM input formats.
        # External slide evidence still shows the object in context.
        return path if path and Path(path).suffix.lower() not in {".wmf", ".emf"} else None
    return None


# ---------------------------------------------------------------------------
# Layout crops — keeps the principle "only ask about the relationship
# between two already-final nodes" (module docstring §1), but the judgment
# itself sometimes needs to see the "actual page (sheet) layout" (where a
# caption sits relative to a table, what the alignment/spacing looks like)
# that text or an isolated content crop alone can't reveal — the crop
# `_anchor_pixel_path` gives (the table/image alone; an xlsx table capture
# doesn't even keep margins) shows "what the content is" but not "where it
# sits."
#
# Supports PDF/XLSX/PPTX: for PDF, `extractors/pdf.py` fills every
# Text/Table/Image node with a `bbox` (0-1000 normalized) + `page_index`,
# and for XLSX, `extractors/xlsx.py` fills cell coordinates
# (`range` or `row`/`col`) + which worksheet it belongs to — for both,
# simply reopening the original file lets those coordinates crop and show
# the actual on-screen area. PPTX uses supplied slide images or native
# reconstruction, with labels positioned using shapes' normalized bboxes. DOCX is excluded, since a stable native
# layout render isn't possible without LibreOffice.
# `_build_layout_crop_renderer` hides the per-format
# branching, and the caller (`propose_edges`) only uses the (render, close)
# pair.
# ---------------------------------------------------------------------------


def _open_pdf_document(document: ArticDocument) -> pymupdf.Document | None:
    """If `document` is a PDF extraction result, reopens the original PDF
    and returns it — used by layout crop rendering
    (`_render_pdf_region_crop`) to get actual page pixels. None if it isn't
    a PDF (a format constraint) or the original file has moved/been deleted
    — the caller silently falls back to the existing text-only path (no
    path fails completely, the same principle as this module's other VLM
    calls)."""
    if document.format != "pdf":
        return None
    try:
        return pymupdf.open(document.source_path)
    except Exception:  # noqa: BLE001 — the original PDF is lost/corrupted, proceed with no layout crop
        return None


def _bbox_union(bboxes: list[dict]) -> dict:
    return {
        "x_min": min(b["x_min"] for b in bboxes),
        "y_min": min(b["y_min"] for b in bboxes),
        "x_max": max(b["x_max"] for b in bboxes),
        "y_max": max(b["y_max"] for b in bboxes),
    }


def _render_pdf_region_crop(
    pdf_doc: pymupdf.Document | None, nodes: list[Node], margin_frac: float = 0.05, min_margin_pt: float = 8.0
) -> bytes | None:
    """If `nodes` all have a `bbox` (0-1000 normalized) on the same PDF
    page, crops the region enclosing all of them, with a bit of margin, from
    that page and renders it as a PNG — so the VLM can see the actual page
    layout directly (text alone can't convey spatial information like
    "which table this caption sits right below").

    None if `pdf_doc` is absent (a non-PDF document, or the original file is
    lost), `nodes` has no `bbox`/`page_index` (only pdf.py fills this
    property), or the pages differ — the caller silently falls back to the
    text-only path."""
    if pdf_doc is None or not nodes:
        return None
    page_indices = {n.properties.get("page_index") for n in nodes}
    bboxes = [n.properties.get("bbox") for n in nodes]
    if len(page_indices) != 1 or any(b is None for b in bboxes):
        return None  # pages differ, or a node with no bbox is mixed in — can't crop

    page_index = next(iter(page_indices))
    if page_index is None or not (0 <= page_index < pdf_doc.page_count):
        return None

    union = _bbox_union(bboxes)
    page = pdf_doc[page_index]
    w, h = page.rect.width, page.rect.height
    x0, y0 = union["x_min"] / 1000 * w, union["y_min"] / 1000 * h
    x1, y1 = union["x_max"] / 1000 * w, union["y_max"] / 1000 * h
    mx, my = max((x1 - x0) * margin_frac, min_margin_pt), max((y1 - y0) * margin_frac, min_margin_pt)
    rect = pymupdf.Rect(max(0, x0 - mx), max(0, y0 - my), min(w, x1 + mx), min(h, y1 + my))
    if rect.width <= 0 or rect.height <= 0:
        return None
    pixmap = page.get_pixmap(clip=rect, dpi=200)
    return pixmap.tobytes("png")


def _render_layout_crop_for_anchor_pdf(
    pdf_doc: pymupdf.Document | None, anchor: Node, candidates: list[Node]
) -> bytes | None:
    """Calls `_render_pdf_region_crop` gathering only the candidates that are
    "on the same page" as the anchor — so that even in the rare case where
    the window spans a page boundary, that one candidate doesn't scrap the
    whole crop (a candidate on another page still stays in the candidate
    list as a text description, it's just left out of the crop)."""
    if pdf_doc is None:
        return None
    anchor_page = anchor.properties.get("page_index")
    if anchor_page is None:
        return None
    same_page = [anchor] + [c for c in candidates if c.properties.get("page_index") == anchor_page]
    return _render_pdf_region_crop(pdf_doc, same_page)


_XLSX_RANGE_RE = re.compile(r"^R(\d+)C(\d+):R(\d+)C(\d+)$")


def _xlsx_node_range(n: Node) -> tuple[int, int, int, int] | None:
    """Extracts (min_row, min_col, max_row, max_col) from the position info
    xlsx.py fills in. A Table/Text node has a `range`
    ("R{r1}C{c1}:R{r2}C{c2}") string, while a standalone Image outside any
    table range only has a single anchor cell (`row`/`col`) — that case is
    treated as a one-cell range. None if none of these are present (a
    format constraint)."""
    range_str = n.properties.get("range")
    if range_str:
        m = _XLSX_RANGE_RE.match(range_str)
        if m:
            r1, c1, r2, c2 = (int(x) for x in m.groups())
            return (r1, c1, r2, c2)
    row, col = n.properties.get("row"), n.properties.get("col")
    if row is not None and col is not None:
        return (row, col, row, col)
    return None


def _xlsx_sheet_index_map(document: ArticDocument) -> dict[str, int]:
    """Content node id -> the `sheet_index` of the worksheet it belongs to.
    `extractors/xlsx.py` only ever directly links Artifact (worksheet) ->
    content with one PARENT_OF edge (xlsx has no nesting like docx's images
    inside table cells) — so only that edge needs to be checked."""
    artifacts_by_id = {n.id: n for n in document.nodes if n.type == NodeType.ARTIFACT}
    mapping: dict[str, int] = {}
    for e in document.edges:
        if e.type != EdgeType.PARENT_OF or e.source_id not in artifacts_by_id:
            continue
        sheet_index = artifacts_by_id[e.source_id].properties.get("sheet_index")
        if sheet_index is not None:
            mapping[e.target_id] = sheet_index
    return mapping


def _open_xlsx_layout_context(document: ArticDocument) -> tuple[openpyxl.Workbook, dict[str, int], Path] | None:
    """If `document` is an xlsx extraction result, opens (the workbook, a
    node-id->sheet_index mapping, the original path). None if it isn't
    xlsx, or the original is lost/corrupted — the caller silently falls
    back to the text-only path with no crop."""
    if document.format != "xlsx":
        return None
    try:
        wb = openpyxl.load_workbook(document.source_path, data_only=True)
    except Exception:  # noqa: BLE001 — the original xlsx is lost/corrupted, proceed with no layout crop
        return None
    return wb, _xlsx_sheet_index_map(document), Path(document.source_path)


def _render_xlsx_region_crop(ws, min_row: int, min_col: int, max_row: int, max_col: int, xlsx_path: Path) -> bytes | None:
    """Reuses `capture_table_image` (the same renderer as xlsx.py's Table
    visual capture) to render into a temp file, then just reads the bytes
    and deletes it — a layout crop is a single-API-call throwaway with no
    reason to persist as a permanent artifact (the same purpose as
    `_render_pdf_region_crop` pulling PNG bytes straight from memory — the
    xlsx renderer's contract is to save the PIL canvas straight to a file
    path, so it goes through a temp file once)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "layout_crop.png"
        canvas_w, canvas_h, _ = capture_table_image(ws, min_row, min_col, max_row, max_col, tmp_path, xlsx_path=xlsx_path)
        if canvas_w <= 0 or canvas_h <= 0 or not tmp_path.exists():
            return None
        return tmp_path.read_bytes()


def _render_layout_crop_for_anchor_xlsx(
    wb: openpyxl.Workbook, sheet_index_map: dict[str, int], xlsx_path: Path, anchor: Node, candidates: list[Node]
) -> bytes | None:
    """Gathers only the candidates on the "same worksheet" as the anchor and
    crops the region enclosing their cell ranges — the same idea as PDF's
    `_render_layout_crop_for_anchor_pdf`, but since xlsx coordinates are
    cell rows/columns rather than points, no normalization/margin
    calculation is needed (`capture_table_image`'s default padding=1 cell
    already serves as the margin)."""
    anchor_sheet = sheet_index_map.get(anchor.id)
    if anchor_sheet is None:
        return None
    same_sheet = [anchor] + [c for c in candidates if sheet_index_map.get(c.id) == anchor_sheet]
    ranges = [r for n in same_sheet if (r := _xlsx_node_range(n)) is not None]
    if not ranges or not (0 <= anchor_sheet < len(wb.worksheets)):
        return None
    min_row = min(r[0] for r in ranges)
    min_col = min(r[1] for r in ranges)
    max_row = max(r[2] for r in ranges)
    max_col = max(r[3] for r in ranges)
    return _render_xlsx_region_crop(wb.worksheets[anchor_sheet], min_row, min_col, max_row, max_col, xlsx_path)


def _build_layout_crop_renderer(
    document: ArticDocument,
) -> tuple[Callable[[Node, list[Node]], bytes | None], Callable[[], None]]:
    """Returns a (render, close) pair — the "anchor+candidates -> layout
    crop PNG" function matching `document`'s format, and a function that
    cleans up the resources it holds onto (an open PDF/workbook). If the
    format isn't supported (DOCX) or the original file is lost, render is a
    no-op that always returns None, and close is a no-op too — the caller
    (`propose_edges`) only needs these two, with no need to know the
    format."""
    no_op: tuple[Callable[[Node, list[Node]], bytes | None], Callable[[], None]] = (
        lambda anchor, candidates: None,
        lambda: None,
    )

    if document.format == "pdf":
        pdf_doc = _open_pdf_document(document)
        if pdf_doc is None:
            return no_op
        return (lambda anchor, candidates: _render_layout_crop_for_anchor_pdf(pdf_doc, anchor, candidates)), pdf_doc.close

    if document.format == "xlsx":
        ctx = _open_xlsx_layout_context(document)
        if ctx is None:
            return no_op
        wb, sheet_index_map, xlsx_path = ctx
        return (
            lambda anchor, candidates: _render_layout_crop_for_anchor_xlsx(
                wb, sheet_index_map, xlsx_path, anchor, candidates
            ),
            wb.close,
        )

    if document.format == "pptx":
        from .slide_images import render_slide_evidence
        from ..capture.pptx_capture import PptxSlideRenderer
        from pptx.exc import PackageNotFoundError
        from zipfile import BadZipFile
        import warnings

        renderer = None
        attempted = False

        def render_pptx(anchor: Node, candidates: list[Node]) -> bytes | None:
            nonlocal renderer, attempted
            evidence = render_slide_evidence(document, anchor, candidates)
            if evidence is not None:
                return evidence
            if not attempted:
                attempted = True
                try:
                    renderer = PptxSlideRenderer(document.source_path)
                except (OSError, ValueError, KeyError, PackageNotFoundError, BadZipFile) as exc:
                    warnings.warn(f"Cannot reconstruct PPTX: {exc}", stacklevel=2)
            if renderer is None:
                return None
            index = anchor.properties.get("slide_index")
            if not isinstance(index, int) or not 0 <= index < len(renderer.presentation.slides):
                return None
            try:
                reconstruction = renderer.render(index)
            except (OSError, ValueError, KeyError) as exc:
                warnings.warn(f"Cannot reconstruct slide {index}: {exc}", stacklevel=2)
                return None
            return render_slide_evidence(document, anchor, candidates, reconstruction=reconstruction)

        def close_pptx() -> None:
            if renderer is not None:
                renderer.close()

        return render_pptx, close_pptx

    return no_op


_DEFAULT_ANCHOR_TYPES = (NodeType.TABLE, NodeType.IMAGE)


def build_context_windows(
    nodes: list[Node],
    window: int = CONTEXT_WINDOW,
    anchor_types: tuple[NodeType, ...] = _DEFAULT_ANCHOR_TYPES,
    *,
    boundary_types: tuple[NodeType, ...] = _DEFAULT_ANCHOR_TYPES,
) -> list[tuple[Node, list[Node]]]:
    """A list of (anchor, candidates) pairs. The anchor type is
    `anchor_types` (default Table/Image); candidates are always the
    surrounding Texts — if `TEXT` is included in `anchor_types`, a Text
    anchor's own candidates always exclude itself (only other Texts become
    candidates).

    Assumes `nodes` is already sorted in document order (NEXT order) —
    just use the order `ArticDocument.content_nodes()` produces as-is.

    **Boundary (`boundary_types`, default Table/Image)**: while searching in
    each direction from the anchor for up to `window` **Text** candidates,
    if another node belonging to `boundary_types` is encountered, the search
    in that direction stops right there (that node itself isn't added as a
    candidate either) — because Text beyond it is text that's "already
    closer to another table/image." Without this, in a document where
    tables/images sit closely packed (e.g. an engineering review document
    that repeats `[Table A] [Caption A] [Table B] [Caption B]`), Table A's
    context window would end up mixing in Caption B, and Table B's window
    would mix in Caption A — since each anchor is judged independently, that
    leads to a caption getting wrongly linked to the neighboring table
    (confirmed: a CAPTION_OF/REFERENCES edge does get created, but "the edge
    connects the wrong pair of nodes"). Even when `TEXT` is included in
    `anchor_types` (`propose_edges`'s Text-anchor HEADING_PARENT pass,
    `include_text_anchors=True`), `boundary_types` keeps its default
    (Table/Image) — heading search shouldn't stop at every single
    body-paragraph Text, only at content like a table/image that actually
    functions as a section boundary.

    `window` counts **Text nodes actually added as candidates**, not nodes
    scanned — a non-boundary, non-Text node (e.g. several Images in a row
    illustrating the current paragraph) is skipped over for free rather than
    eating into the budget. Confirmed on a real xlsx review document: a
    numbered subsection ("3) ...") sitting right after 4 embedded photos had
    zero Text within the old `window`-nodes-scanned reach in that direction,
    so its actual section heading (further back, past the photos) was never
    even offered as a HEADING_PARENT candidate — silently leaving it
    unreparented, at the same level as its own heading instead of nested
    under it, with 1)/2) (which had no photo gap before their heading)
    reparenting correctly right next to it. A node this can't reach at all:
    the anchor itself and the start/end of `nodes`.
    """
    windows = []
    for i, n in enumerate(nodes):
        if n.type not in anchor_types:
            continue

        left: list[Node] = []
        j = i - 1
        while j >= 0 and len(left) < window:
            other = nodes[j]
            if other.type in boundary_types:
                break
            if other.type == NodeType.TEXT:
                left.append(other)
            j -= 1
        left.reverse()  # restore document order (left -> right)

        right: list[Node] = []
        j = i + 1
        while j < len(nodes) and len(right) < window:
            other = nodes[j]
            if other.type in boundary_types:
                break
            if other.type == NodeType.TEXT:
                right.append(other)
            j += 1

        candidates = left + right
        if candidates:
            windows.append((n, candidates))
    return windows


def _content_artifact_map(document: ArticDocument) -> dict[str, str]:
    from ..structure import content_artifact_map

    return content_artifact_map(document)


def _spatial_distance(document: ArticDocument, left: Node, right: Node) -> tuple[float, float]:
    """The empty-space gap and center distance between XLSX cell ranges or PPTX normalized bboxes."""
    if document.format == "xlsx":
        left_range, right_range = _xlsx_node_range(left), _xlsx_node_range(right)
        if left_range is None or right_range is None:
            return float("inf"), float("inf")
        ly0, lx0, ly1, lx1 = left_range
        ry0, rx0, ry1, rx1 = right_range
    else:
        left_box, right_box = left.properties.get("bbox"), right.properties.get("bbox")
        if left_box is None or right_box is None:
            return float("inf"), float("inf")
        lx0, ly0, lx1, ly1 = (
            left_box["x_min"], left_box["y_min"], left_box["x_max"], left_box["y_max"]
        )
        rx0, ry0, rx1, ry1 = (
            right_box["x_min"], right_box["y_min"], right_box["x_max"], right_box["y_max"]
        )
    dx = max(0.0, max(lx0, rx0) - min(lx1, rx1))
    dy = max(0.0, max(ly0, ry0) - min(ly1, ry1))
    center_distance = abs((lx0 + lx1) - (rx0 + rx1)) + abs((ly0 + ly1) - (ry0 + ry1))
    return dx + dy, center_distance


def _build_document_context_windows(
    document: ArticDocument,
    *,
    window: int,
    anchor_types: tuple[NodeType, ...],
    boundary_types: tuple[NodeType, ...],
) -> list[tuple[Node, list[Node]]]:
    """PPTX considers every Text in the same slide, ranked by proximity.
    In-plot labels otherwise crowd out below-plot captions such as before/after.
    XLSX retains its bounded spatial window; other formats use document order.

    PPTX Text anchors also use the full slide: on a two-column PBA review,
    a page number and preceding section blocks exhausted the reading-order
    window before the last two sections could reach their slide title.
    Proximity orders candidates but cannot exclude that title.

    The Text-anchor HEADING_PARENT pass (`anchor_types=(NodeType.TEXT,)`,
    always called with `boundary_types=()`) is the exception for
    XLSX: it reuses the plain reading-order scan (`build_context_windows`,
    scoped to one sheet) instead of the spatial ranking below — see
    that branch's own comment for why. A Table/Image anchor's own
    HEADING_PARENT window (also `boundary_types=()`, judged together with
    CAPTION_OF/REFERENCES in `_propose_for_anchor`) deliberately keeps the
    spatial ranking instead — confirmed on a real xlsx document: switching
    it to reading-order too let a spatially-distant, textually-distracting
    line (itself quoting the document's own title) crowd out a table's
    genuinely nearest, spatially-obvious heading, breaking a
    Table→heading pairing that the spatial ranking already got right.
    """
    content = document.content_nodes()
    if document.format in {"docx", "pdf"}:
        # Cached TOC entries describe navigation, not nearby body sections.
        content = [node for node in content if not node.properties.get("is_toc")]
        native_children = {
            edge.target_id for edge in document.edges
            if edge.type == EdgeType.PARENT_OF
            and edge.properties.get("structural_source") in {"docx_outline", "pdf_outline"}
        }
        windows = build_context_windows(
            content, window=window, anchor_types=anchor_types, boundary_types=boundary_types
        )
        # Table/Image still need their caption/reference judgment. Their native
        # parents are protected again at application time.
        return [(anchor, candidates) for anchor, candidates in windows
                if anchor.type != NodeType.TEXT or anchor.id not in native_children]
    if document.format not in ("xlsx", "pptx"):
        return build_context_windows(
            content, window=window, anchor_types=anchor_types, boundary_types=boundary_types
        )
    artifact_by_node = _content_artifact_map(document)
    if not artifact_by_node:
        return build_context_windows(
            content, window=window, anchor_types=anchor_types, boundary_types=boundary_types
        )
    if document.format == "xlsx" and boundary_types == () and anchor_types == (NodeType.TEXT,):
        # HEADING_PARENT wants "the nearest heading-like text when scanning
        # outward in reading order, skipping past intervening non-boundary
        # content for free" (see build_context_windows's docstring) — not
        # "nearest in any direction by raw pixel/cell distance," which the
        # spatial ranking below is tuned for instead (it was built for
        # CAPTION_OF/REFERENCES, where the physically nearest text really is
        # the best caption candidate, and works well enough for a
        # Table/Image anchor's own HEADING_PARENT judgment too — see this
        # function's docstring). Confirmed on a real xlsx document: a
        # numbered subsection's own body text (a phenomenon/cause writeup
        # right above it) ranked spatially closer than its actual section
        # heading a few lines further up, so the heading never even made the
        # top-`window` cut — while reading-order scanning finds it correctly
        # once `build_context_windows` skips non-Text for free (see its
        # docstring). Scoped to Text anchors only — see the module-level
        # docstring for why a Table/Image anchor keeps the spatial ranking.
        # Grouping preserves each sheet's row/column order and keeps
        # candidates from leaking across sheets.
        groups: dict[str, list[Node]] = {}
        for node in content:
            artifact_id = artifact_by_node.get(node.id)
            if artifact_id is not None:
                groups.setdefault(artifact_id, []).append(node)
        return [
            window_pair
            for group_nodes in groups.values()
            for window_pair in build_context_windows(
                group_nodes, window=window, anchor_types=anchor_types, boundary_types=boundary_types
            )
        ]
    windows: list[tuple[Node, list[Node]]] = []
    for anchor in content:
        if anchor.type not in anchor_types:
            continue
        artifact_id = artifact_by_node.get(anchor.id)
        if artifact_id is None:
            continue
        ranked = [
            (_spatial_distance(document, anchor, node), node)
            for node in content
            if node.type == NodeType.TEXT and node.id != anchor.id
            and artifact_by_node.get(node.id) == artifact_id
        ]
        if document.format != "pptx":
            ranked = [(distance, node) for distance, node in ranked if distance[0] != float("inf")]
        ranked.sort(key=lambda item: item[0])
        selected = ranked if document.format == "pptx" else ranked[:window]
        candidates = [node for _, node in selected]
        if candidates:
            windows.append((anchor, candidates))
    return windows


def _propose_for_anchor(
    client,
    model: str,
    anchor: Node,
    candidates: list[Node],
    *,
    caption_eligible_ids: set[str] | None = None,
    layout_crop_fn: Callable[[Node, list[Node]], bytes | None] | None = None,
) -> tuple[list[Edge], tuple[int, str] | None]:
    """Returns (CAPTION_OF/REFERENCES proposals, heading_choice). `candidates`
    may reach further than a CAPTION_OF/REFERENCES candidate normally would
    (see `propose_edges`'s wide/narrow window split) so HEADING_PARENT can
    still see past a neighboring table/image — `caption_eligible_ids`
    (candidate ids from the narrower, boundary-respecting window) marks
    which of them stay eligible for CAPTION_OF/REFERENCES; `None` means no
    narrowing (every candidate eligible, e.g. the xlsx/pptx spatial path
    where this distinction doesn't apply). `heading_choice` is
    (candidate_index, rationale) for the sole candidate marked
    HEADING_PARENT, or None if zero or more than one were (ambiguous is
    treated the same as none — see the module docstring)."""
    candidate_desc = "\n".join(
        f"[{i}] ({c.type.value}, {_node_layout_hint(c)}) {_node_preview(c)}"
        + ("" if caption_eligible_ids is None or c.id in caption_eligible_ids else " (too far for CAPTION_OF/REFERENCES)")
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"Anchor node ({anchor.type.value}, {_node_layout_hint(anchor)}): {_node_preview(anchor)}\n\n"
        f"Candidate nodes, ordered by layout proximity (the order itself is not evidence):\n"
        f"{candidate_desc}\n\n"
        "Classify every candidate's role as CAPTION_OF, REFERENCES, HEADING_PARENT, or NONE. "
        "At most one candidate should be HEADING_PARENT. Choose NONE when uncertain."
    )

    # Attach each of the two kinds of image separately (when present) —
    # because they answer different questions: the isolated crop
    # (`_anchor_pixel_path`) shows "what this table/image's content is,"
    # while the layout crop (`layout_crop_fn`, PDF/XLSX/PPTX — see
    # `_build_layout_crop_renderer`) shows "how it's actually laid out on
    # the page/sheet relative to the candidates." If neither is present,
    # falls back to text-only as before — no path fails completely.
    blocks: list[dict] = [{"type": "input_text", "text": prompt}]

    pixel_path = _anchor_pixel_path(anchor)
    if pixel_path and Path(pixel_path).exists():
        blocks.append({"type": "input_text", "text": "[Close-up image of the anchor node]"})
        blocks.append({"type": "input_image", "image_url": encode_image_data_url(pixel_path), "detail": "high"})

    layout_png = layout_crop_fn(anchor, candidates) if layout_crop_fn is not None else None
    if layout_png is not None:
        blocks.append({
            "type": "input_text",
            "text": "[Layout evidence for the anchor and candidates. PPTX may be a labeled reconstruction; missing or approximate visual details are not evidence of absence. Inspect their positions, "
            "alignment, spacing, and whether a caption is directly above or below the anchor.]",
        })
        blocks.append({"type": "input_image", "image_url": encode_png_bytes_data_url(layout_png), "detail": "high"})

    content: list[dict] | str = blocks if len(blocks) > 1 else prompt

    response = parse_with_context(client, [(anchor, candidates)],
        model=model,
        instructions=_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        text_format=_EdgeProposalResult,
    )
    parsed = response.output_parsed
    if parsed is None:
        return [], None
    edges: list[Edge] = []
    heading_picks: list[tuple[int, str]] = []
    for p in parsed.proposals:
        if p.edge_type == "NONE" or not (0 <= p.context_index < len(candidates)):
            continue
        if p.edge_type == "HEADING_PARENT":
            heading_picks.append((p.context_index, p.rationale))
            continue
        target = candidates[p.context_index]
        if caption_eligible_ids is not None and target.id not in caption_eligible_ids:
            continue  # defense in depth — the model was told this candidate is HEADING_PARENT-only
        edges.append(
            Edge(
                type=EdgeType(p.edge_type),
                source_id=target.id,
                target_id=anchor.id,
                properties={"rationale": p.rationale, "proposed_by": f"llm:{model}"},
            )
        )
    # More than one HEADING_PARENT pick is exactly as ambiguous as zero —
    # a wrong reparenting is worse than none, so don't guess which to trust.
    heading_choice = heading_picks[0] if len(heading_picks) == 1 else None
    return edges, heading_choice


def _widen_row_sibling_windows(
    document: ArticDocument, wide_windows: list[tuple[Node, list[Node]]]
) -> list[tuple[Node, list[Node]]]:
    """Several Table/Image nodes anchored at the exact same xlsx row (a
    common real pattern — several photos, or a small summary table next to
    a couple of photos, side by side illustrating one point) can each have
    a genuinely different nearest-by-distance HEADING_PARENT candidate set,
    purely because of which *column* they happen to sit in: confirmed on a
    real document, 4 photos at the same row illustrating one bullet — 3 of
    them (in columns further from a numbered item two bullets below) had
    their section heading rank in their own top-`window` spatial neighbors,
    but the 4th (which happened to share that other item's column) had two
    irrelevant nearby lines outrank the heading entirely, so it was
    silently left unreparented while its 3 row-siblings resolved fine.
    (Only confirmed for Image so far; Table is included on the same
    reasoning — this project already treats Table/Image as one class of
    anchor everywhere else — but revisit if a real Table row-sibling case
    ever turns out to need something different.)

    Fix: an anchor's HEADING_PARENT candidates are widened with the union
    of its same-row siblings' own candidates (deduplicated) — if the
    heading is close enough to be *any* sibling's neighbor, every sibling
    gets a fair shot at it, not just whichever one happens to sit in the
    same column. CAPTION_OF/REFERENCES eligibility is untouched by this —
    it's computed separately (`narrow_ids_by_anchor`, before this
    widening), so a candidate borrowed only from a sibling's window can
    still supply HEADING_PARENT but is correctly still ineligible as
    *this* anchor's own caption/reference.

    An anchor with no row-sibling (the overwhelming majority) is returned
    unchanged — this only ever adds candidates, and only within a group of
    2+ same-row Table/Image nodes in the same Artifact. Deliberately not
    generalized further (e.g. to PPTX shapes at the same visual position)
    without a confirmed real case — `row` is an xlsx-only property.
    """
    artifact_by_node = _content_artifact_map(document)
    candidates_by_id = {anchor.id: candidates for anchor, candidates in wide_windows}
    group_ids_by_key: dict[tuple[str | None, int], list[str]] = {}
    for anchor, _ in wide_windows:
        row = anchor.properties.get("row")
        if anchor.type not in _DEFAULT_ANCHOR_TYPES or row is None:
            continue
        key = (artifact_by_node.get(anchor.id), row)
        group_ids_by_key.setdefault(key, []).append(anchor.id)

    widened: list[tuple[Node, list[Node]]] = []
    for anchor, candidates in wide_windows:
        row = anchor.properties.get("row")
        eligible = anchor.type in _DEFAULT_ANCHOR_TYPES and row is not None
        group_ids = group_ids_by_key.get((artifact_by_node.get(anchor.id), row), []) if eligible else []
        if len(group_ids) < 2:
            widened.append((anchor, candidates))
            continue
        merged = list(candidates)
        seen_ids = {c.id for c in candidates}
        seen_ids.add(anchor.id)
        for sibling_id in group_ids:
            if sibling_id == anchor.id:
                continue
            for c in candidates_by_id.get(sibling_id, []):
                if c.id not in seen_ids:
                    seen_ids.add(c.id)
                    merged.append(c)
        widened.append((anchor, merged))
    return widened


def _caption_or_reference_source_ids(document: ArticDocument, extra_edges: Sequence[Edge] = ()) -> set[str]:
    """Every node id that is the *source* of a CAPTION_OF/REFERENCES edge —
    either already in `document.edges` (deterministic, from
    `scaffold.caption_prefix_edges`/`reference_label_edges`, or an earlier
    `resolve_ambiguous_captions`/`propose_edges` call) or freshly proposed
    in `extra_edges` (this same `propose_edges` call's own Table/Image
    pass, not yet applied to `document`).

    Used to keep a Text that's already judged to specifically caption or
    reference a Table/Image from also being offered as some *other*
    anchor's HEADING_PARENT candidate — `_HEADING_INSTRUCTIONS` already
    says a caption/label "names one object, not a section," but that was
    only ever a prompt-level request; nothing stopped a caption text with
    no recognizable label prefix (so undetected by the deterministic
    heuristics, and not yet resolved by this pass either) from still
    reaching the candidate list and getting picked — confirmed on a real
    pptx document: a short caption sitting right under an unrelated photo
    got picked as an unrelated Text's section heading, purely because nothing
    had disqualified it as a candidate. Reusing this project's own
    CAPTION_OF/REFERENCES judgment (a narrower, more reliable question —
    "does this Text specifically caption this Table/Image" — than the
    open-ended "is this Text a legitimate section heading") is more robust
    than adding a new geometric caption-likeness heuristic (e.g. bbox
    overlap with a nearby Image): it needs no new per-format tuning, and it
    mechanically enforces a rule the prompt already declares, rather than
    inventing a new one."""
    ids = {
        e.source_id for e in document.edges
        if e.type in (EdgeType.CAPTION_OF, EdgeType.REFERENCES)
    }
    ids.update(
        e.source_id for e in extra_edges
        if e.type in (EdgeType.CAPTION_OF, EdgeType.REFERENCES)
    )
    return ids


def propose_edges(
    document: ArticDocument,
    client=None,
    model: str = DEFAULT_MODEL,
    window: int = CONTEXT_WINDOW,
    include_text_anchors: bool = False,
    *,
    batch_size: int = _BATCH_SIZE,
    max_workers: int = _DEFAULT_MAX_WORKERS,
    trace_dir: str | Path | None = None,
) -> list[Edge]:
    """For each Table/Image anchor in `document`, judges CAPTION_OF,
    REFERENCES, and HEADING_PARENT against its surrounding Text in **one
    VLM call per anchor** — see the module docstring's "Two different trust
    levels inside one call" for why these three are judged together and
    applied differently.

    **CAPTION_OF/REFERENCES**: doesn't modify `document.edges` directly —
    returned in this function's result list, and it's up to the caller
    whether to adopt them via `document.edges.extend(...)` after human
    review (the principle that every LLM-proposed edge in this schema is "a
    proposal, not final," see schema.py).

    **HEADING_PARENT**: applied directly to `document.edges` (deletes the
    `Artifact -> content` PARENT_OF edge, reparents it under the chosen
    heading Text), guarded by a cycle check (`_is_ancestor`) — an anchor
    left unreparented (no clear heading, an ambiguous multi-pick, or an API
    failure) keeps its original structure untouched. A reparented anchor's
    new PARENT_OF edge carries `properties["reparented_from"]`, so scan
    `document.edges` for that key after the call to find every reparent this
    made — it isn't part of this function's return value.

    `include_text_anchors=True` additionally judges HEADING_PARENT for Text
    anchors (a paragraph reparented under its own section heading) — off by
    default since the anchor count grows from "one per Table/Image" to "one
    per paragraph." CAPTION_OF/REFERENCES are never judged for a Text
    anchor, so this runs as its own batched pass (`batch_size`/`max_workers`
    — see the module docstring's "Batching and concurrency"), not merged
    into the one-call-per-anchor path above.

    If `client` isn't given, `openai.OpenAI()` is built with its default
    constructor (needs the `OPENAI_API_KEY` environment variable) — this is
    why the `openai` package is an optional dependency.

    If the API call for one anchor (or one batch, for a batched Text anchor)
    fails (a rate limit/timeout/transient network error, etc.), only that
    anchor/batch is skipped and the rest are still processed — the same
    partial-failure principle as the other LLM batch code
    (`caption_images.py`, `table_structure.py`).

    `trace_dir` optionally saves actual requests/images, candidate ID mappings,
    parsed responses/errors, application reasons and graph snapshots in a
    unique local run directory. It records document content, not credentials.

    If `document` is a PDF or XLSX extraction result, the original file is
    opened once (`_build_layout_crop_renderer`) and reused across every
    anchor in both passes — reopening it per anchor would be wasteful, and
    for any other format, or if the original is lost, this automatically
    becomes a no-op renderer, silently falling back to the text/isolated-crop
    path with no layout crop (see `_propose_for_anchor`).
    """
    if client is None:
        from openai import OpenAI  # lazy import — openai isn't needed unless this function is called

        client = OpenAI()

    if trace_dir is not None:
        with trace_session(client, trace_dir, document) as traced:
            return propose_edges(document, client=traced, model=model, window=window,
                                 include_text_anchors=include_text_anchors,
                                 batch_size=batch_size, max_workers=max_workers)
    if isinstance(client, TraceClient):
        client.save("relations-before.json", document)

    # Table/Image anchors: the wide window (no boundary) is what
    # HEADING_PARENT needs to see past a neighboring table/image to find the
    # real section heading; `narrow_ids_by_anchor` marks which of those
    # candidates are still close enough (boundary-respecting) to be eligible
    # for CAPTION_OF/REFERENCES — see `_propose_for_anchor`'s docstring. For
    # xlsx/pptx, `_build_document_context_windows`'s spatial path ignores
    # `boundary_types` entirely, so both windows come out identical there
    # (no behavior change for those formats).
    wide_windows = _build_document_context_windows(
        document, window=window, anchor_types=_DEFAULT_ANCHOR_TYPES, boundary_types=()
    )
    narrow_ids_by_anchor = {
        a.id: {c.id for c in cands}
        for a, cands in _build_document_context_windows(
            document, window=window, anchor_types=_DEFAULT_ANCHOR_TYPES, boundary_types=_DEFAULT_ANCHOR_TYPES
        )
    }
    wide_windows = _widen_row_sibling_windows(document, wide_windows)

    proposals: list[Edge] = []
    heading_reparents: list[tuple[Node, Node, str]] = []  # (anchor, new heading parent, rationale)

    layout_crop_fn, close_layout_resources = _build_layout_crop_renderer(document)
    try:
        for anchor, candidates in wide_windows:
            try:
                edges, heading_choice = _propose_for_anchor(
                    client, model, anchor, candidates,
                    caption_eligible_ids=narrow_ids_by_anchor.get(anchor.id, set()),
                    layout_crop_fn=layout_crop_fn,
                )
            except Exception:  # noqa: BLE001 — e.g. an API error, skip just this anchor and continue with the rest
                continue
            proposals.extend(edges)
            if heading_choice is not None:
                index, rationale = heading_choice
                heading_reparents.append((anchor, candidates[index], rationale))

        if include_text_anchors:
            heading_reparents.extend(
                _propose_text_heading_parents(
                    document, client, model, window,
                    layout_crop_fn=layout_crop_fn,
                    batch_size=batch_size, max_workers=max_workers,
                    excluded_candidate_ids=_caption_or_reference_source_ids(document, proposals),
                )
            )
    finally:
        close_layout_resources()

    _apply_heading_reparents(document, model, heading_reparents, trace=client if isinstance(client, TraceClient) else None)
    if isinstance(client, TraceClient):
        client.save("relations-after.json", document)
        client.save("relation-proposals.json", ProposalRecord(proposals=proposals))
    return proposals


# ---------------------------------------------------------------------------
# Reparenting PARENT_OF from heading Text -> content (deepening the tree) —
# an opt-in exception to the principle schema.py nails down that "PARENT_OF
# is deterministic." See the module docstring's "Two different trust levels
# inside one call" and "propose_edges(..., include_text_anchors=True)"
# paragraphs. The Table/Image half of this lives in `_propose_for_anchor`
# above (one call together with CAPTION_OF/REFERENCES); everything below is
# the Text-anchor extension plus the shared apply/cycle-check machinery.
# ---------------------------------------------------------------------------


class _HeadingParentChoice(BaseModel):
    anchor_index: int = Field(
        default=0,
        description="Zero-based index of the anchor in a batched request; ignored for a single-anchor request.",
    )
    parent_index: int | None = Field(
        description="Zero-based candidate index of the section heading that owns this content, or null if none."
    )
    rationale: str = Field(description="One concise sentence explaining the decision.")


class _HeadingParentBatchResult(BaseModel):
    choices: list[_HeadingParentChoice]


_HEADING_INSTRUCTIONS = """\
Determine which section heading owns a content node in a document graph. You
receive one anchor node (a Text paragraph — a Table/Image anchor's heading is
judged elsewhere, alongside its CAPTION_OF/REFERENCES) and nearby candidate
nodes (all Text).

## Decision criteria
- Select a candidate only when it names the enclosing semantic unit that
  owns the anchor. Titles and section/subsection headings are all eligible.
- If the anchor is itself a subsection heading, find its parent section
  heading; if it is a body paragraph, find the heading of its section. The
  anchor itself is never included in the candidates.
- Choose the nearest enclosing level in the semantic hierarchy, not simply
  the nearest text in space. A title governing the whole content region is
  an eligible parent of its top-level sections; its broad scope does not
  disqualify it. Physical containers and semantic headings are distinct.
  When no intermediate heading exists, a top-level section belongs directly
  to the overall title that governs it. Do not require an extra section
  heading between them or reject that title as "document-level."
- Judge ownership of the entire anchor block. When a block contains both a
  heading and its body, seek the parent of that section, not a heading for
  a sentence inside it. Distinguish peer sections from their shared parent
  using meaning and layout together.
- Navigation, running metadata, and decorative elements are not section
  content. Shared position, formatting, or markers alone do not establish
  a parent-child relationship.
- Never select the following as headings:
  - Captions or labels such as "Table 1" or "Figure 2." They name one object,
    not a section.
  - Body prose or sentences listing values and conditions. Do not make one
    body paragraph the parent of another.
  - A candidate that is merely above or below the anchor. Select it only when
    the anchor is genuinely content of the section named by that candidate.
- If no genuine heading exists or the choice is ambiguous, set parent_index to
  null. A wrong reparenting is worse than leaving the structure unchanged.
- When a layout image is attached, inspect alignment, hierarchy, and visual
  section boundaries. Use the close-up anchor image and text semantics too.
"""


def _propose_heading_parent(
    client, model: str, anchor: Node, candidates: list[Node], *, layout_png: bytes | None = None
) -> tuple[int, str] | None:
    """The index+rationale of whichever `candidates` (all Text) is the
    heading for `anchor` (always Text here — a Table/Image anchor's heading
    is judged by `_propose_for_anchor` instead), or None if there isn't one.
    Same pixel-attachment convention as `_propose_for_anchor`."""
    candidate_desc = "\n".join(
        f"[{i}] ({_node_layout_hint(c)}) {_node_preview(c)}" for i, c in enumerate(candidates)
    )
    prompt = (
        f"Anchor node ({anchor.type.value}, {_node_layout_hint(anchor)}): {_node_preview(anchor)}\n\n"
        f"Candidate nodes (all Text):\n{candidate_desc}\n\n"
        "Return the index of the section heading that owns the anchor, or null if none."
    )

    pixel_path = _anchor_pixel_path(anchor)
    blocks: list[dict] = [{"type": "input_text", "text": prompt}]
    if pixel_path and Path(pixel_path).exists():
        blocks.append({"type": "input_text", "text": "[Close-up image of the anchor node]"})
        blocks.append({"type": "input_image", "image_url": encode_image_data_url(pixel_path), "detail": "high"})
    if layout_png is not None:
        blocks.append({
            "type": "input_text",
            "text": "[Layout evidence (PPTX may be a labeled reconstruction; missing or approximate details are not evidence of absence). Inspect the anchor and candidate positions, alignment, and visual section boundaries before selecting a parent heading.]",
        })
        blocks.append({"type": "input_image", "image_url": encode_png_bytes_data_url(layout_png), "detail": "high"})
    content: list[dict] | str = blocks if len(blocks) > 1 else prompt

    response = parse_with_context(client, [(anchor, candidates)],
        model=model,
        instructions=_HEADING_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        text_format=_HeadingParentChoice,
    )
    parsed = response.output_parsed
    if parsed is None:
        logger.warning("Heading judgment returned no parsed output for %s", anchor.id)
        return None
    logger.debug("Heading judgment for %s: parent_index=%s; %s", anchor.id, parsed.parent_index, parsed.rationale)
    if parsed.parent_index is None or not (0 <= parsed.parent_index < len(candidates)):
        return None
    return parsed.parent_index, parsed.rationale


def _propose_heading_parents_batch(
    client, model: str, items: list[tuple[Node, list[Node]]]
) -> list[_HeadingParentChoice]:
    """Judges several `items` (anchor, candidate list) in one call — the
    batching path that cuts round trips (see the `_BATCH_SIZE` comment near
    the top of the module). **Only for anchors with no visual material
    (text-only)** — an anchor with an isolated pixel or a layout crop is
    handled with an individual call by `_propose_heading_parent` instead
    (kept separate because mixing several images into one batch risks the
    model losing track of which image belongs to which anchor — since most
    anchors have no pixel when `include_text_anchors=True` is on, most of
    the batching benefit survives this separation anyway). The prompt states
    explicitly to judge each anchor independently — one anchor's judgment
    must not affect another's."""
    blocks = []
    for i, (anchor, candidates) in enumerate(items):
        candidate_desc = "\n".join(
            f"  [{j}] ({_node_layout_hint(c)}) {_node_preview(c)}" for j, c in enumerate(candidates)
        )
        blocks.append(
            f"### Anchor {i} ({anchor.type.value}, {_node_layout_hint(anchor)}): {_node_preview(anchor)}\n"
            f"Candidate nodes (all Text):\n{candidate_desc}"
        )
    prompt = (
        f"You are given {len(items)} anchors. Evaluate each anchor independently; one anchor's "
        "candidate list and decision must not affect another. For every anchor, return the candidate "
        "index of its section heading, or null if none. Set anchor_index to the corresponding anchor.\n\n"
        + "\n\n".join(blocks)
    )
    response = parse_with_context(client, items,
        model=model,
        instructions=_HEADING_INSTRUCTIONS,
        input=[{"role": "user", "content": prompt}],
        text_format=_HeadingParentBatchResult,
    )
    parsed = response.output_parsed
    return parsed.choices if parsed else []


def _current_parent_id(edges: list[Edge], node_id: str) -> str | None:
    for e in edges:
        if e.type == EdgeType.PARENT_OF and e.target_id == node_id:
            return e.source_id
    return None


def _is_ancestor(edges: list[Edge], candidate_ancestor_id: str, node_id: str) -> bool:
    """Whether `candidate_ancestor_id` is an ancestor of `node_id` (based on
    the current PARENT_OF edges).

    `_apply_heading_reparents` calls this before linking `new_parent ->
    anchor` — if `anchor` is already an ancestor of `new_parent`, adding
    that edge would create a cycle the moment it's added, so it has to be
    blocked. Now that Text can become the parent of another Text (`TEXT`
    included in anchor_types), this is a case that can actually happen,
    unlike before (only Text as parent, only Table/Image as child)."""
    cur = node_id
    seen = {cur}
    while True:
        parent = _current_parent_id(edges, cur)
        if parent is None or parent in seen:  # not found, or a defensive cycle guard
            return False
        if parent == candidate_ancestor_id:
            return True
        seen.add(parent)
        cur = parent


def _propose_text_heading_parents(
    document: ArticDocument,
    client,
    model: str,
    window: int,
    *,
    layout_crop_fn: Callable[[Node, list[Node]], bytes | None],
    batch_size: int,
    max_workers: int,
    excluded_candidate_ids: set[str] = frozenset(),
) -> list[tuple[Node, Node, str]]:
    """The `propose_edges(..., include_text_anchors=True)` extension: for
    every Text content node, judges whether a nearby Text candidate is
    actually the title (heading) of the section it belongs to. Returns
    (anchor, heading, rationale) tuples for `_apply_heading_reparents` to
    apply — doesn't touch `document` itself, so a caller inspecting
    `propose_edges`'s CAPTION_OF/REFERENCES-only side effects mid-call never
    sees a half-applied reparent.

    A paragraph Text becomes a reparent target too here (e.g. a subheading
    Text under its parent heading Text, a body paragraph under its
    section's heading) — this gets closer to a real document outline, but
    since Text can now be both parent and child, a cycle risk arises (e.g.
    A proposed as B's heading while B is simultaneously proposed as A's
    heading); `_apply_heading_reparents`'s `_is_ancestor` check blocks any
    reparent that would create one. The number of anchors is much larger
    than Table/Image (hundreds of Text in a document means hundreds of
    anchors), which is exactly why this is a separate, batched pass rather
    than folded into `_propose_for_anchor`'s one-call-per-anchor path.

    **Batching and concurrency** (2026-09-05, empirical basis in
    `docs/vlm-integration-research.md` §14): calling one API request per
    anchor sequentially (the old approach) took over 750 seconds on 250+
    anchors — mostly a fixed per-request round-trip delay rather than the
    judgment itself. Now **anchors with no visual material (text-only)** are
    grouped into batches of `batch_size` and judged in one call
    (`_propose_heading_parents_batch`), while **anchors with an isolated
    pixel or a layout crop** are still handled with individual calls, to
    avoid the risk of mixing several images into one batch — both groups run
    concurrently, up to `max_workers`, via a thread pool (the openai client
    is thread-safe). The judgment itself still comes out independently per
    anchor, so there's no loss of safety — only the unit of failure grows
    from one anchor to one batch. Confirmed on re-measurement: 753s -> 98s
    (~7.7x), with no regression in reparent result quality.

    Safeguards: if the candidate list is judged to have no real heading
    (`parent_index=null`), or the API call fails (the whole batch, for a
    batched call) — the item is simply left out of the returned list (the
    partial-failure principle, applied at the batch level; `NONE`-worthy
    ambiguity is `_apply_heading_reparents`'s job to catch for the ones that
    are returned).

    **Enumerated-sibling collapsing**: a flat "1) .../2) .../3) ..." run
    (`_enumerated_sibling_groups`) is asked about **once**, not once per
    member — the numbering already proves every member shares one parent,
    so N independent questions about the same underlying answer only add a
    chance they disagree, or that a later member's own candidate window
    (starting further from the heading than an earlier member's) misses the
    heading entirely even though an earlier member's window would have
    found it (confirmed on a real xlsx document: "1)"/"2)" resolved
    correctly, but "3)"'s own window was exactly consumed by "1)", "2)",
    and a sub-bullet before ever reaching their shared heading a few lines
    further up). The run's first (and so heading-closest) member stands in
    for the whole group — its own window is used for the one decision, and
    the result is applied to every member identically. A run whose own
    first member has no candidate window at all (rare) isn't collapsed;
    each member then falls back to being judged independently as if there
    were no run.

    **Caption/reference exclusion** (`excluded_candidate_ids`, from
    `_caption_or_reference_source_ids` — see that function's docstring): a
    Text already judged to specifically caption or reference a Table/Image
    is dropped from every *other* anchor's candidate list before this ever
    calls the model — `_HEADING_INSTRUCTIONS` already says a caption/label
    "names one object, not a section," so this only mechanically enforces
    a rule already declared, for the cases (no recognizable label prefix)
    the deterministic heuristics can't catch on their own. An excluded
    Text can still be an anchor itself (it belongs under some heading too),
    it just stops being offered as *someone else's*.
    """
    # boundary_types=() — unlike the Table/Image narrow window, this doesn't
    # stop at a boundary: it's normal for a section to have several
    # tables/images (e.g. "3. Measurement Results" comes with one table and
    # one photo) so cutting off heading search for a body Text just because
    # it hit another Table/Image along the way would make it miss its
    # actual heading. The problem of a caption leaking to the neighboring
    # table when tables/images sit close together (see
    # build_context_windows's default boundary_types) is specific to a
    # Table/Image anchor's CAPTION_OF/REFERENCES and doesn't apply here.
    windows = _build_document_context_windows(
        document, window=window, anchor_types=(NodeType.TEXT,), boundary_types=()
    )
    if excluded_candidate_ids:
        windows = [
            (anchor, [c for c in candidates if c.id not in excluded_candidate_ids])
            for anchor, candidates in windows
        ]
        windows = [(anchor, candidates) for anchor, candidates in windows if candidates]

    window_by_anchor_id = {anchor.id: candidates for anchor, candidates in windows}
    group_by_representative_id: dict[str, list[Node]] = {}
    collapsed_ids: set[str] = set()
    for group in _enumerated_sibling_groups(document):
        representative, *rest = group
        if representative.id not in window_by_anchor_id:
            continue  # the heading-closest member has no candidates of its own — fall back to per-member judgment
        group_by_representative_id[representative.id] = group
        collapsed_ids.update(m.id for m in rest)
    windows = [(anchor, candidates) for anchor, candidates in windows if anchor.id not in collapsed_ids]
    if isinstance(client, TraceClient):
        client.save("heading-groups.json", SiblingGroups(groups=[
            [node.id for node in group] for group in group_by_representative_id.values()
        ]))

    visual_windows = [(anchor, candidates, layout_crop_fn(anchor, candidates)) for anchor, candidates in windows]

    def _has_isolated_pixel(anchor: Node) -> bool:
        path = _anchor_pixel_path(anchor)
        return bool(path and Path(path).exists())

    with_visual = [
        item for item in visual_windows if _has_isolated_pixel(item[0]) or item[2] is not None
    ]
    text_only = [(anchor, candidates) for anchor, candidates, crop in visual_windows if (
        not _has_isolated_pixel(anchor) and crop is None
    )]

    def _fetch_single(
        anchor: Node, candidates: list[Node], layout_png: bytes | None
    ) -> dict[str, tuple[int, str]]:
        try:
            choice = _propose_heading_parent(
                client, model, anchor, candidates, layout_png=layout_png
            )
        except Exception as exc:  # noqa: BLE001 — leave this anchor's original structure as-is
            logger.warning("Heading judgment failed for %s (%s)", anchor.id, type(exc).__name__)
            return {}
        return {anchor.id: choice} if choice is not None else {}

    def _fetch_batch(batch: list[tuple[Node, list[Node]]]) -> dict[str, tuple[int, str]]:
        try:
            choices = _propose_heading_parents_batch(client, model, batch)
        except Exception as exc:  # noqa: BLE001 — preserve every anchor in the failed batch
            logger.warning("Heading judgment failed for batch of %d anchors (%s)", len(batch), type(exc).__name__)
            return {}
        result: dict[str, tuple[int, str]] = {}
        for choice in choices:
            if not (0 <= choice.anchor_index < len(batch)) or choice.parent_index is None:
                continue
            anchor, candidates = batch[choice.anchor_index]
            if not (0 <= choice.parent_index < len(candidates)):
                continue
            result[anchor.id] = (choice.parent_index, choice.rationale)
        return result

    choice_by_anchor_id: dict[str, tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [
            pool.submit(_fetch_single, anchor, candidates, crop)
            for anchor, candidates, crop in with_visual
        ]
        futures += [pool.submit(_fetch_batch, batch) for batch in _chunked(text_only, max(1, batch_size))]
        for future in as_completed(futures):
            choice_by_anchor_id.update(future.result())

    reparents: list[tuple[Node, Node, str]] = []
    for anchor, candidates in windows:  # kept in original document order — the cycle check applying these depends on order
        choice = choice_by_anchor_id.get(anchor.id)
        if choice is None:
            continue
        index, rationale = choice
        heading = candidates[index]
        for target in group_by_representative_id.get(anchor.id, [anchor]):
            reparents.append((target, heading, rationale))
    return reparents


def _apply_heading_reparents(document: ArticDocument, model: str, reparents: list[tuple[Node, Node, str]], *, trace: TraceClient | None = None) -> list[str]:
    """Applies (anchor, new heading parent, rationale) reparents to
    `document.edges` in place, in the order given (both `propose_edges`'s
    Table/Image pass and `_propose_text_heading_parents` build their lists
    in original document order — the cycle check below depends on that).

    Safeguards: if the anchor's existing PARENT_OF parent can't be found
    (should exist structurally, but may already have been reparented by an
    earlier item in this same list) or the reparent would create a cycle
    (`_is_ancestor`) — the original structure is left as-is and the item is
    skipped (the same partial-failure principle as the rest of this
    module).

    Returns the ids of anchors actually reparented (for logging/
    verification) — every one of them also carries
    `properties["reparented_from"]` on its new PARENT_OF edge, which is how
    `propose_edges`'s own docstring tells a caller to find these without a
    dedicated return value there.
    """
    artifact_by_node = _content_artifact_map(document)
    promoted: list[str] = []
    for index, (anchor, heading, rationale) in enumerate(reparents):
        old_edge = next(
            (e for e in document.edges if e.type == EdgeType.PARENT_OF and e.target_id == anchor.id), None
        )
        outcome = "applied"
        if old_edge is None:
            outcome = "missing_parent"
        elif artifact_by_node.get(anchor.id) is None or artifact_by_node.get(anchor.id) != artifact_by_node.get(heading.id):
            outcome = "artifact_boundary"
        elif old_edge.properties.get("structural_source") in {"docx_outline", "pdf_outline"}:
            outcome = "native_hierarchy"
        elif anchor.id == heading.id or _is_ancestor(document.edges, anchor.id, heading.id):
            outcome = "cycle"
        elif old_edge.source_id == heading.id:
            outcome = "already_parent"
        if trace is not None:
            trace.save(f"heading-outcomes/{index}.json", HeadingOutcome(
                anchor_id=anchor.id, parent_id=heading.id, rationale=rationale, outcome=outcome,
            ))
        if outcome != "applied":
            continue

        document.edges.remove(old_edge)
        document.edges.append(
            Edge(
                type=EdgeType.PARENT_OF,
                source_id=heading.id,
                target_id=anchor.id,
                properties={
                    "rationale": rationale,
                    "proposed_by": f"llm:{model}",
                    "reparented_from": old_edge.source_id,
                },
            )
        )
        promoted.append(anchor.id)

    return promoted


# ---------------------------------------------------------------------------
# Nesting a numbered subheading under its parent section — pure text pattern
# matching, no VLM/API needed. Complements propose_edges's HEADING_PARENT
# judgment (VLM): that judgment has no idea "3.1" should be a child of "3"
# (it only judges each content node individually, and never infers hierarchy
# from a numbering scheme) — confirmed (1706.03762): only 2 of 13
# subheadings ended up under their parent section (5.1->5, 6.1->6), the
# other 11 stayed directly under the Artifact. This function fills that
# gap using number parsing within the current Artifact and section instance.
# ---------------------------------------------------------------------------

_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)[\s]+[A-Za-z]")


def nest_numbered_headings(document: ArticDocument) -> list[str]:
    """Finds Text nodes in the form "N.M Title" (e.g. "3.1 Encoder and
    Decoder Stacks") and **reparents** them under the Text node matching
    their number's parent section, if one exists ("6.3 English Constituency
    Parsing" -> under "6 Results", "3.2.1 Scaled..." -> under "3.2
    Attention") — modifies `document` in place.

    **Needs no VLM/API key** — pure text matching that only looks at the
    numeric pattern before the title (not just anything starting with a
    digit — a letter has to come immediately after the number for it to
    count as a heading, a safeguard against a table data row like
    `"1 512 512 5.29 ..."` being mistaken for a heading just because it
    starts with a digit, a counterexample confirmed empirically).

    Only preceding headings in the same Artifact and still-open numbered
    section are eligible. Repeated numbers start a new section instance;
    TOC entries are excluded. Native hierarchy and cycle safety take
    precedence over the textual pattern. Missing parents leave nodes intact.

    Returns: the list of heading node ids that were actually reparented."""
    artifact_by_node = _content_artifact_map(document)
    headings_by_artifact: dict[str, dict[str, Node]] = {}
    nested: list[str] = []
    for heading in document.content_nodes():
        if heading.type != NodeType.TEXT or heading.properties.get("is_toc"):
            continue
        artifact_id = artifact_by_node.get(heading.id)
        m = _NUMBERED_HEADING_RE.match(heading.properties.get("text", "").strip())
        if artifact_id is None or m is None:
            continue
        number = m.group(1)
        headings = headings_by_artifact.setdefault(artifact_id, {})
        # Keep only strict ancestors of the new number: peers and their
        # descendants belong to a section that has now ended.
        for previous in list(headings):
            if not number.startswith(previous + "."):
                del headings[previous]
        headings[number] = heading
        if "." not in number:
            continue  # a top-level section ("3") has no reparent target — leave as-is
        parent_number = number.rsplit(".", 1)[0]
        parent = headings.get(parent_number)
        if parent is None or _is_ancestor(document.edges, heading.id, parent.id):
            continue
        if _current_parent_id(document.edges, heading.id) == parent.id:
            continue  # already under the right parent

        old_edge = next(
            (e for e in document.edges if e.type == EdgeType.PARENT_OF and e.target_id == heading.id), None
        )
        if old_edge is None or old_edge.properties.get("structural_source") in {"docx_outline", "pdf_outline"}:
            continue
        document.edges.remove(old_edge)
        document.edges.append(
            Edge(
                type=EdgeType.PARENT_OF,
                source_id=parent.id,
                target_id=heading.id,
                properties={"nested_by": "numbered_heading_pattern", "reparented_from": old_edge.source_id},
            )
        )
        nested.append(heading.id)

    return nested


# ---------------------------------------------------------------------------
# Detecting a flat "1)/2)/3)" enumerated-sibling run — used by
# `_propose_text_heading_parents` above to collapse the whole run into one
# HEADING_PARENT question instead of one per member (see that function's
# "Enumerated-sibling collapsing" docstring section for why). A different
# pattern from nest_numbered_headings below ("N.M" hierarchical
# subsections) — this one is a flat sibling list, not a nested hierarchy.
# ---------------------------------------------------------------------------

_ENUM_SIBLING_RE = re.compile(r"^(\d+)\)(?!\s*\d)")


def _enumerated_sibling_groups(document: ArticDocument) -> list[list[Node]]:
    """Runs of Text nodes sharing the same flat "N)" enumeration marker
    (e.g. "1) 현상", "2) 원인", "3) ...") with consecutive, increasing-by-1
    numbers, scoped to one Artifact — the numbering itself is proof they're
    siblings under the same parent, independent of how far apart they sit
    or what non-matching content (a sub-bullet, an embedded photo, ...)
    happens to fall between them. `(?!\\d)` in `_ENUM_SIBLING_RE` keeps a
    tabular row like "1) 512 512 ..." from being mistaken for an enumerated
    item, the same safeguard `_NUMBERED_HEADING_RE` above uses for a
    different pattern."""
    artifact_by_node = _content_artifact_map(document)
    groups: list[list[Node]] = []
    current: list[Node] = []
    current_number: int | None = None
    current_artifact: str | None = None
    current_list: tuple[int, int] | None = None
    current_parent: str | None = None
    for node in document.content_nodes():
        if node.type != NodeType.TEXT:
            continue
        if node.properties.get("outline_level") is not None or node.properties.get("is_toc"):
            if len(current) >= 2:
                groups.append(current)
            current, current_number, current_artifact, current_list = [], None, None, None
            current_parent = None
            continue
        match = _ENUM_SIBLING_RE.match(node.properties.get("text", "").strip())
        list_key = None
        if "list_num_id" in node.properties and "list_level" in node.properties:
            list_key = (node.properties["list_num_id"], node.properties["list_level"])
        if match is None and list_key is None:
            # Preserve the historical manual-marker behavior: non-numbered
            # content (including an embedded image's neighboring paragraph)
            # may sit between numbered siblings. Automatic lists are still
            # grouped by their shared numId/ilvl when their members recur.
            continue
        number = int(match.group(1)) if match else None
        artifact_id = artifact_by_node.get(node.id)
        if artifact_id is None:
            # No resolvable Artifact ancestor at all (shouldn't happen for a
            # real extractor's output) — never bridge a group across this
            # node, and never start one from it either (two `None`s must
            # not equal each other into a false same-artifact match).
            if len(current) >= 2:
                groups.append(current)
            current, current_number, current_artifact, current_list = [], None, None, None
            continue
        same_list = list_key is not None and list_key == current_list
        same_manual = list_key is None and current_list is None and match is not None and current_number is not None and number == current_number + 1
        parent_id = _current_parent_id(document.edges, node.id)
        if current and artifact_id == current_artifact and parent_id == current_parent and (same_list or same_manual):
            current.append(node)
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [node]
        current_number = number
        current_artifact = artifact_id
        current_list = list_key
        current_parent = parent_id
    if len(current) >= 2:
        groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# Merging same-line text fragments — a VLM judges "should be merged vs. a
# separate semantic unit," something pure geometry can't safely tell apart,
# and actually merges what extractors/pdf._reorder_same_line_blocks has only
# fixed the order of. See the module docstring's "merge_fragmented_text"
# paragraph.
# ---------------------------------------------------------------------------

MERGE_MODEL = DEFAULT_MODEL
_FRAGMENT_LINE_Y_OVERLAP = 0.5  # same criterion as extractors.pdf._LINE_Y_OVERLAP (minimum y-overlap fraction to count as the same line)
# Since bbox is in 0-1000 normalized coordinates, this value isn't directly
# comparable to extractors.pdf._LINE_MAX_HEIGHT (30pt, in pt units), but the
# intent is the same — a safety margin against a large block like a
# paragraph/footnote/watermark bridging several unrelated "lines" (confirmed
# in §11.2).
_FRAGMENT_LINE_MAX_HEIGHT = 40


class _FragmentMergeDecision(BaseModel):
    cluster_index: int = Field(
        default=0,
        description="Zero-based index of the cluster in a batched request; ignored for a single-cluster request.",
    )
    should_merge: bool = Field(
        description="True only if the fragments are pieces of one split sentence, formula, or phrase; false for separate semantic units that merely share a line. Use false when uncertain."
    )
    merged_text: str = Field(
        default="", description="When should_merge is true, join the fragments naturally in the given order without inventing content."
    )


class _FragmentMergeBatchResult(BaseModel):
    decisions: list[_FragmentMergeDecision]


_MERGE_INSTRUCTIONS = """\
Determine whether adjacent PDF text blocks are fragments of one expression.
PDF extraction can split a single line around superscripts, subscripts, font
size changes, or baseline shifts. Merge such fragments to restore the original
formula, sentence, or phrase.

Never merge separate semantic units that merely share the same vertical
position, such as different author names or different item headings. When
uncertain, do not merge; an incorrect merge is worse than leaving fragments.

Some clusters include a page crop showing the original line. Treat it as the
primary evidence. Inspect baseline offsets, visible gaps, columns, and whether
the fragments visually form one expression. If the image shows separate units,
do not merge even when the text alone looks joinable.

Fragments are already ordered from left to right. When merging, preserve every
provided fragment and only normalize necessary spacing. Do not invent, omit,
or alter content.
"""


def _find_same_line_text_clusters(document: ArticDocument) -> list[list[Node]]:
    """Re-applies the same idea as `extractors/pdf._reorder_same_line_blocks`
    (clustering "the same line" by y overlap + a height cap) to Text nodes
    on an already-built `ArticDocument`. Only Text nodes with `bbox`/
    `page_index` properties qualify (only pdf.py fills these), so this is
    naturally a no-op on other format documents, with no cluster caught at
    all. A cluster of size 1 (a node with no merge candidate) is excluded."""
    by_page: dict[int, list[Node]] = {}
    for n in document.nodes:
        if n.type != NodeType.TEXT:
            continue
        if "bbox" not in n.properties or "page_index" not in n.properties:
            continue
        by_page.setdefault(n.properties["page_index"], []).append(n)

    def bbox_tuple(n: Node) -> tuple[float, float, float, float]:
        b = n.properties["bbox"]
        return (b["x_min"], b["y_min"], b["x_max"], b["y_max"])

    def _height(box: tuple[float, float, float, float]) -> float:
        return box[3] - box[1]

    clusters: list[list[Node]] = []
    for nodes in by_page.values():
        def connected(i: int, j: int) -> bool:
            bi, bj = bbox_tuple(nodes[i]), bbox_tuple(nodes[j])
            if _height(bi) > _FRAGMENT_LINE_MAX_HEIGHT or _height(bj) > _FRAGMENT_LINE_MAX_HEIGHT:
                return False
            return _y_overlap_frac(bi, bj) >= _FRAGMENT_LINE_Y_OVERLAP

        for members in cluster_indices(len(nodes), connected):
            if len(members) < 2:
                continue
            ordered = sorted(members, key=lambda i: bbox_tuple(nodes[i])[0])
            clusters.append([nodes[i] for i in ordered])

    return clusters


def _decide_fragment_merge_batch(
    client, model: str, batch: list[tuple[list[Node], bytes | None]]
) -> list[_FragmentMergeDecision]:
    """Judges several clusters in one call — the batching path that cuts
    round trips (see the `_BATCH_SIZE` comment near the top of the module).
    The prompt states explicitly to judge each cluster completely
    independently — one cluster's judgment must not affect another's.

    Each element of `batch` is (a cluster, the actual page-crop PNG bytes
    for that line, or None) — when a crop is present, an image block is
    inserted right after that cluster's text description (the same way
    `resolve_ambiguous_captions` puts several candidate images in one call).
    The crop is None when the PDF can't be reopened (the original is lost)
    or that cluster's page can't be found, and this function silently falls
    back to text-only for just that cluster — this can vary cluster by
    cluster."""
    content: list[dict] = []
    for ci, (cluster, crop_png) in enumerate(batch):
        frag_desc = "\n".join(f"  [{i}] {n.properties.get('text', '')!r}" for i, n in enumerate(cluster))
        content.append({"type": "input_text", "text": f"### Cluster {ci} ({len(cluster)} fragments):\n{frag_desc}"})
        if crop_png is not None:
            content.append(
                {"type": "input_image", "image_url": encode_png_bytes_data_url(crop_png), "detail": "high"}
            )
    intro = (
        f"You are given {len(batch)} same-line fragment clusters. Fragments in each cluster are "
        "ordered left to right. Some clusters are followed by a crop of the original page line; "
        "judge clusters without an image from text alone. Evaluate every cluster independently, "
        "and set cluster_index to the corresponding cluster."
    )
    content.insert(0, {"type": "input_text", "text": intro})

    response = client.responses.parse(
        model=model,
        instructions=_MERGE_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        text_format=_FragmentMergeBatchResult,
    )
    parsed = response.output_parsed
    return parsed.decisions if parsed else []


def merge_fragmented_text(
    document: ArticDocument,
    client=None,
    model: str = MERGE_MODEL,
    *,
    batch_size: int = _BATCH_SIZE,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> list[str]:
    """For each same-line candidate cluster (`_find_same_line_text_clusters`),
    asks a VLM "is this a split expression fragment, or is it separate,"
    and if the answer is to merge, modifies `document` in place to actually
    merge it. See the module docstring's "merge_fragmented_text" paragraph.

    If `document` is a PDF extraction result (and the original file still
    exists), for each cluster the page area that line is actually printed
    on is cropped and shown to the VLM alongside the text
    (`_render_pdf_region_crop`) — so it can judge from directly seeing a
    baseline mismatch or visual gap that text alone can't reveal. If it
    isn't a PDF or the original is lost, it judges from text alone as
    before, with no crop (no path fails completely).

    **Batching and concurrency** (2026-09-05, empirical basis in
    `docs/vlm-integration-research.md` §14): instead of calling one API
    request per cluster sequentially, clusters are grouped into batches of
    `batch_size` and judged in one call (`_decide_fragment_merge_batch`),
    and batches run concurrently, up to `max_workers`, via a thread pool
    (the openai client is thread-safe) — cutting the round trips/time that
    used to grow with the number of clusters. The judgment itself still
    comes out independently per cluster, so there's no loss of safety —
    only the unit of failure grows from one cluster to one batch.

    The cluster's first (leftmost) node is kept and its `text`/`name` is
    updated to the merged text (its existing edges like `PARENT_OF` carry
    over unchanged, so no new edge needs to be created) — the rest of the
    cluster's fragment nodes, and edges pointing at them, are removed.
    `bbox` is merged into the rectangle enclosing the whole cluster.
    `properties["merged_from"]` (the list of removed node ids) and
    `properties["merged_by"]` (the model name) are kept for traceability.

    If the model says "don't merge" (`should_merge=False`), `merged_text` is
    empty, or the API call fails (the whole batch, for a batched call), that
    cluster is left fragmented as-is (the partial-failure principle, the
    same one `propose_edges` follows, applied at the batch level).

    Returns: the list of node ids that survive as the merge result (i.e.
    whose text was updated) — since this runs concurrently, the order may
    not exactly match cluster order — call with `max_workers=1` if order
    matters."""
    if client is None:
        from openai import OpenAI  # lazy import — openai isn't needed unless this function is called

        client = OpenAI()

    clusters = _find_same_line_text_clusters(document)
    if not clusters:
        return []

    # Crop rendering (local, pymupdf) is all finished sequentially before
    # running the API call thread pool — since rendering a `pymupdf.Document`
    # concurrently across multiple threads isn't guaranteed safe, splitting
    # it into "slow rendering sequential / fast network calls concurrent"
    # preserves both thread safety and the batching benefit.
    pdf_doc = _open_pdf_document(document)
    try:
        cluster_crops = [_render_pdf_region_crop(pdf_doc, cluster) for cluster in clusters]
    finally:
        if pdf_doc is not None:
            pdf_doc.close()
    clusters_with_crops = list(zip(clusters, cluster_crops))

    def _fetch_batch(
        batch: list[tuple[list[Node], bytes | None]]
    ) -> list[tuple[list[Node], _FragmentMergeDecision]]:
        try:
            decisions = _decide_fragment_merge_batch(client, model, batch)
        except Exception:  # noqa: BLE001 — the whole batch failed, leave every cluster in this batch fragmented as-is
            return []
        return [(batch[d.cluster_index][0], d) for d in decisions if 0 <= d.cluster_index < len(batch)]

    resolved: list[tuple[list[Node], _FragmentMergeDecision]] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(_fetch_batch, batch) for batch in _chunked(clusters_with_crops, max(1, batch_size))]
        for future in as_completed(futures):
            resolved.extend(future.result())

    merged: list[str] = []
    for cluster, decision in resolved:
        if not decision.should_merge or not decision.merged_text.strip():
            continue

        anchor, rest = cluster[0], cluster[1:]
        anchor.properties["text"] = decision.merged_text
        anchor.name = decision.merged_text[:40] + ("…" if len(decision.merged_text) > 40 else "")
        anchor.properties["merged_from"] = [n.id for n in rest]
        anchor.properties["merged_by"] = f"llm:{model}"
        xs = [n.properties["bbox"] for n in cluster]
        anchor.properties["bbox"] = {
            "x_min": min(b["x_min"] for b in xs),
            "y_min": min(b["y_min"] for b in xs),
            "x_max": max(b["x_max"] for b in xs),
            "y_max": max(b["y_max"] for b in xs),
        }

        remove_ids = {n.id for n in rest}
        document.nodes = [n for n in document.nodes if n.id not in remove_ids]
        document.edges = [
            e for e in document.edges if e.source_id not in remove_ids and e.target_id not in remove_ids
        ]
        merged.append(anchor.id)

    return merged


# ---------------------------------------------------------------------------
# Merging semantic Text groups from the 2D layout — a more general step than
# `merge_fragmented_text`, which only restores physical fragments on the
# same line. Geometry only builds candidate regions for the VLM to review;
# the actual semantic-unit partitioning is done by a VLM looking at a
# numbered page crop.
# ---------------------------------------------------------------------------

SEMANTIC_MERGE_MODEL = DEFAULT_MODEL
_SEMANTIC_HORIZONTAL_GAP = 80
_SEMANTIC_VERTICAL_GAP = 32
_SEMANTIC_MAX_NODE_HEIGHT = 80
_SEMANTIC_MAX_REGION_NODES = 20


class _SemanticTextGroup(BaseModel):
    node_indices: list[int] = Field(
        description="Indices of Text nodes in this region that form one complete semantic unit. Exclude standalone nodes."
    )
    rationale: str = Field(description="One concise sentence explaining why these nodes form one semantic unit.")


class _SemanticRegionDecision(BaseModel):
    region_index: int = Field(description="Zero-based index of the region in the batched input.")
    groups: list[_SemanticTextGroup] = Field(
        default_factory=list,
        description="Groups to merge, or an empty list if none. Never include one node in multiple groups.",
    )


class _SemanticRegionBatchResult(BaseModel):
    decisions: list[_SemanticRegionDecision]


_SEMANTIC_MERGE_INSTRUCTIONS = """\
Restore Text nodes extracted from a PDF page into complete semantic units by
using both their actual two-dimensional layout and their content. Each region
image shows numbered Text bounding boxes, and the original text list uses the
same indices.

Group separate PDF blocks only when they must be read together to form one
independent information unit. Examples include name + affiliation + email,
titles/captions/sentences/formulas split across blocks, or a bullet/number plus
its item text. Do not group nodes merely because they are close or share a
line. Different people, columns, items, headings versus body content, and
independent paragraphs must remain separate.

Rules:
- Use alignment, columns, whitespace, containment, and text semantics together.
- Every group must contain at least two nodes. Do not output already-complete
  standalone nodes.
- Never include one node in multiple groups or invent indices outside the region.
- When uncertain, do not merge. A false merge is worse than leaving nodes split.
- Do not rewrite text. Return only node_indices and rationale.
"""


def _find_semantic_text_regions(
    document: ArticDocument,
    *,
    horizontal_gap: float = _SEMANTIC_HORIZONTAL_GAP,
    vertical_gap: float = _SEMANTIC_VERTICAL_GAP,
    max_region_nodes: int = _SEMANTIC_MAX_REGION_NODES,
) -> list[list[Node]]:
    """Builds a 2D proximity graph from per-page Text bboxes and returns its
    connected components as VLM review regions. This step doesn't presume
    meaning. Only nodes that are horizontally close with overlapping y, or
    vertically close with overlapping x, are connected as neighbors. An
    overly large body-text/watermark node is excluded so it doesn't bridge
    several unrelated lines."""
    by_page: dict[int, list[Node]] = {}
    for node in document.nodes:
        if node.type != NodeType.TEXT or "bbox" not in node.properties or "page_index" not in node.properties:
            continue
        by_page.setdefault(node.properties["page_index"], []).append(node)

    def box(node: Node) -> tuple[float, float, float, float]:
        bbox = node.properties["bbox"]
        return bbox["x_min"], bbox["y_min"], bbox["x_max"], bbox["y_max"]

    def gap(a0: float, a1: float, b0: float, b1: float) -> float:
        return max(0.0, max(a0, b0) - min(a1, b1))

    regions: list[list[Node]] = []
    for page_nodes in by_page.values():
        def connected(i: int, j: int) -> bool:
            lx0, ly0, lx1, ly1 = box(page_nodes[i])
            rx0, ry0, rx1, ry1 = box(page_nodes[j])
            if ly1 - ly0 > _SEMANTIC_MAX_NODE_HEIGHT or ry1 - ry0 > _SEMANTIC_MAX_NODE_HEIGHT:
                return False
            x_gap = gap(lx0, lx1, rx0, rx1)
            y_gap = gap(ly0, ly1, ry0, ry1)
            same_row = y_gap == 0 and x_gap <= horizontal_gap
            same_column = x_gap == 0 and y_gap <= vertical_gap
            return same_row or same_column

        for members in cluster_indices(len(page_nodes), connected):
            if len(members) < 2:
                continue
            ordered = sorted(members, key=lambda i: (box(page_nodes[i])[1], box(page_nodes[i])[0]))
            nodes = [page_nodes[i] for i in ordered]
            for start in range(0, len(nodes), max(2, max_region_nodes)):
                chunk = nodes[start : start + max(2, max_region_nodes)]
                if len(chunk) >= 2:
                    regions.append(chunk)
    return regions


def _render_annotated_pdf_region(
    pdf_doc: pymupdf.Document | None, nodes: list[Node]
) -> bytes | None:
    """Draws Text bboxes and their input indices onto the region crop to
    make the VLM's image-to-list correspondence explicit. Returns None if
    there's no original, falling back to text-only judgment."""
    if pdf_doc is None or not nodes:
        return None
    pages = {node.properties.get("page_index") for node in nodes}
    if len(pages) != 1 or None in pages:
        return None
    page_index = next(iter(pages))
    if not (0 <= page_index < pdf_doc.page_count):
        return None

    page = pdf_doc[page_index]
    w, h = page.rect.width, page.rect.height
    union = _bbox_union([node.properties["bbox"] for node in nodes])
    x0, y0 = union["x_min"] / 1000 * w, union["y_min"] / 1000 * h
    x1, y1 = union["x_max"] / 1000 * w, union["y_max"] / 1000 * h
    margin = 18.0
    clip = pymupdf.Rect(max(0, x0 - margin), max(0, y0 - margin), min(w, x1 + margin), min(h, y1 + margin))
    if clip.width <= 0 or clip.height <= 0:
        return None
    dpi = 200
    pixmap = page.get_pixmap(clip=clip, dpi=dpi)
    image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
    draw = ImageDraw.Draw(image)
    scale = dpi / 72
    colors = ("#e53935", "#1e88e5", "#43a047", "#8e24aa", "#fb8c00")
    for index, node in enumerate(nodes):
        bbox = node.properties["bbox"]
        bx0, by0 = bbox["x_min"] / 1000 * w, bbox["y_min"] / 1000 * h
        bx1, by1 = bbox["x_max"] / 1000 * w, bbox["y_max"] / 1000 * h
        rect = tuple(round((value - offset) * scale) for value, offset in (
            (bx0, clip.x0), (by0, clip.y0), (bx1, clip.x0), (by1, clip.y0)
        ))
        color = colors[index % len(colors)]
        draw.rectangle(rect, outline=color, width=4)
        label = f"[{index}]"
        label_box = draw.textbbox((rect[0], rect[1]), label)
        draw.rectangle(label_box, fill=color)
        draw.text((rect[0], rect[1]), label, fill="white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _decide_semantic_regions_batch(
    client, model: str, batch: list[tuple[list[Node], bytes | None]]
) -> list[_SemanticRegionDecision]:
    content: list[dict] = [{
        "type": "input_text",
        "text": f"Partition each of these {len(batch)} 2D text regions into independent semantic units.",
    }]
    for region_index, (nodes, crop) in enumerate(batch):
        node_text = "\n".join(
            f"  [{i}] bbox={node.properties['bbox']} text={node.properties.get('text', '')!r}"
            for i, node in enumerate(nodes)
        )
        content.append({"type": "input_text", "text": f"### Region {region_index}\n{node_text}"})
        if crop is not None:
            content.append({"type": "input_image", "image_url": encode_png_bytes_data_url(crop), "detail": "high"})
    response = client.responses.parse(
        model=model,
        instructions=_SEMANTIC_MERGE_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        text_format=_SemanticRegionBatchResult,
    )
    parsed = response.output_parsed
    return parsed.decisions if parsed else []


def merge_semantic_text_groups(
    document: ArticDocument,
    client=None,
    model: str = SEMANTIC_MERGE_MODEL,
    *,
    batch_size: int = 8,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> list[str]:
    """Merges PDF Text nodes into semantic units according to the page's 2D
    layout and content.

    The geometry-based proximity graph only builds the VLM input regions and
    doesn't finalize any merge. The VLM partitions each region into semantic
    groups by looking at a numbered actual page crop and the source text
    list. Invalid or overlapping groups, and API failures, are conservatively
    skipped. The merged text isn't generated by the model — the source nodes
    are joined with newlines in top-to-bottom, left-to-right order, keeping
    the first node and removing the rest along with their edges."""
    if client is None:
        from openai import OpenAI

        client = OpenAI()
    regions = _find_semantic_text_regions(document)
    if not regions:
        return []

    pdf_doc = _open_pdf_document(document)
    try:
        crops = [_render_annotated_pdf_region(pdf_doc, region) for region in regions]
    finally:
        if pdf_doc is not None:
            pdf_doc.close()
    region_inputs = list(zip(regions, crops))

    def fetch(batch):
        try:
            return batch, _decide_semantic_regions_batch(client, model, batch)
        except Exception:  # noqa: BLE001 — leave just the failed batch as-is
            return batch, []

    results = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(fetch, batch) for batch in _chunked(region_inputs, max(1, batch_size))]
        for future in as_completed(futures):
            results.append(future.result())

    proposals: list[tuple[list[Node], str]] = []
    claimed_ids: set[str] = set()
    for batch, decisions in results:
        for decision in decisions:
            if not (0 <= decision.region_index < len(batch)):
                continue
            region = batch[decision.region_index][0]
            for group in decision.groups:
                indices = list(dict.fromkeys(group.node_indices))
                if len(indices) < 2 or any(index < 0 or index >= len(region) for index in indices):
                    continue
                nodes = [region[index] for index in indices]
                ids = {node.id for node in nodes}
                if ids & claimed_ids:
                    continue
                claimed_ids.update(ids)
                proposals.append((nodes, group.rationale))

    document_order = {node.id: index for index, node in enumerate(document.nodes)}
    merged: list[str] = []
    for nodes, rationale in sorted(proposals, key=lambda item: min(document_order[n.id] for n in item[0])):
        nodes.sort(key=lambda node: (
            node.properties["bbox"]["y_min"], node.properties["bbox"]["x_min"]
        ))
        anchor = min(nodes, key=lambda node: document_order[node.id])
        rest = [node for node in nodes if node.id != anchor.id]
        text = "\n".join(node.properties.get("text", node.name) for node in nodes).strip()
        anchor.properties["text"] = text
        anchor.name = text[:40] + ("…" if len(text) > 40 else "")
        anchor.properties["bbox"] = _bbox_union([node.properties["bbox"] for node in nodes])
        anchor.properties["merged_from"] = [node.id for node in rest]
        anchor.properties["merged_by"] = f"vlm-semantic:{model}"
        anchor.properties["merge_reason"] = rationale
        remove_ids = {node.id for node in rest}
        document.nodes = [node for node in document.nodes if node.id not in remove_ids]
        document.edges = [
            edge for edge in document.edges
            if edge.source_id not in remove_ids and edge.target_id not in remove_ids
        ]
        merged.append(anchor.id)
    return merged


# ---------------------------------------------------------------------------
# Reifying sibling nodes as a synthetic Group node — see schema.py's Group
# node, and the module docstring's "propose_synthetic_groups" paragraph.
# ---------------------------------------------------------------------------

GROUP_MODEL = DEFAULT_MODEL
_MAX_GROUP_REGION_NODES = 60  # a sibling list bigger than this (an extreme
# case with no empirical basis) is skipped rather than arbitrarily split —
# a bad split could cut a real group across the boundary (the
# generalization principle: don't hastily handle a case never actually
# encountered).


class _SyntheticGroupCandidate(BaseModel):
    member_indices: list[int] = Field(
        description="Zero-based indices (within this region) of the sibling nodes that form one group. At least two."
    )
    group_type: str | None = Field(
        default=None,
        description="A short (1-3 word) label for the group's common role, e.g. 'authors', 'kpi_metrics'. "
        "Null if the members clearly belong together but no clean common label applies.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence that these nodes truly form one conceptual unit.")
    basis: list[Literal["spatial", "visual", "structural", "semantic", "boundary"]] = Field(
        default_factory=list,
        description="Which independent cues support this group: spatial proximity, visual repetition "
        "(alignment/spacing/style), structural repetition (same field pattern repeated per member), "
        "shared semantic role, or a clear boundary separating the members from surrounding siblings.",
    )
    rationale: str = Field(description="One concise sentence explaining the grouping.")


class _SiblingRegionDecision(BaseModel):
    region_index: int = Field(description="Zero-based index of the region in the batched input.")
    groups: list[_SyntheticGroupCandidate] = Field(
        default_factory=list,
        description="Groups to create, or an empty list if these siblings do not form any group.",
    )


class _SiblingRegionBatchResult(BaseModel):
    decisions: list[_SiblingRegionDecision]


_GROUP_INSTRUCTIONS = """\
You identify groups of sibling elements in a document graph that function as a
single conceptual unit even though the document has no explicit heading or
container for that unit (e.g. several author name/affiliation/email blocks
with no "Authors" heading, or several KPI cards with no enclosing box).

Each region is one parent's full list of direct sibling nodes, in document
order, alongside a rendering of their actual layout.

Create a group only when supported by at least two independent cues:
- spatial: the members sit close together and form one visual region
- visual: repeated alignment, spacing, font, or styling across members
- structural: the members repeat the same internal pattern (e.g. name then
  affiliation then email, repeated per member)
- semantic: the members clearly share the same role in the document
- boundary: whitespace, rules, or surrounding content set the members apart
  as a unit distinct from what comes before and after

Rules:
- Every group must contain at least two members. Do not output a group of one.
- Never include one sibling in more than one group, and never invent indices
  outside the region.
- group_type is your own label for what the group is, not text copied from the
  document — keep it to a few words, and leave it null if the members belong
  together but resist a clean label.
- Do not group merely because siblings are adjacent or share a page/slide.
  Unrelated paragraphs, unrelated captions, and one-off headings must not be
  grouped with anything.
- When uncertain, return no groups for that region. A missed group is worse
  than nothing, but a false group is worse still — prefer no group.
"""


def _children_by_parent(document: ArticDocument) -> dict[tuple[str, int | None], list[Node]]:
    """(parent (PARENT_OF source) id, page_index) -> the list of child
    content nodes (in document order). This is the basis
    `propose_synthetic_groups` uses to judge "who are the current siblings
    under the same parent, on the same page." An already-created Group is
    excluded from the child candidates (not recursively regrouped — a
    scope limit for the first version).

    **Why split further by `page_index` (confirmed, `1706.03762`)**: since
    PDF/DOCX have only one Artifact for the whole document, grouping by
    parent alone would mix a localized sibling set like an author block
    (present on only 1 of 4 pages) into one region with the entire body
    (124 siblings) — which was confirmed to cause (1) the layout crop to
    not render at all, since it only supports "the same page"
    (`_render_layout_crop_for_anchor_pdf`), and (2) the model, with over 100
    siblings, to actually miss a distinct local pattern like the author
    block among the other 129 items (a distinct pattern identifiable from
    text alone via numeric labels, like References, was still caught even
    in the large region, but the author block was missed). A node with no
    `page_index` (XLSX/PPTX — where Artifact is already per sheet/slide, so
    parent alone narrows it down enough; or the rare case of a PDF node
    missing this property) is grouped into a single `None` bucket, matching
    the previous behavior."""
    parent_by_child = {e.target_id: e.source_id for e in document.edges if e.type == EdgeType.PARENT_OF}
    by_region: dict[tuple[str, int | None], list[Node]] = {}
    for node in document.content_nodes():
        if node.type == NodeType.GROUP:
            continue
        parent_id = parent_by_child.get(node.id)
        if parent_id is not None:
            page_index = node.properties.get("page_index")
            by_region.setdefault((parent_id, page_index), []).append(node)
    return by_region


def _decide_sibling_groups_batch(
    client, model: str, batch: list[tuple[list[Node], bytes | None]]
) -> list[_SiblingRegionDecision]:
    content: list[dict] = [{
        "type": "input_text",
        "text": f"Evaluate each of these {len(batch)} sibling regions independently for synthetic groups.",
    }]
    for region_index, (members, crop) in enumerate(batch):
        member_desc = "\n".join(
            f"  [{i}] ({node.type.value}, {_node_layout_hint(node)}) {_node_preview(node)}"
            for i, node in enumerate(members)
        )
        content.append({"type": "input_text", "text": f"### Region {region_index}\n{member_desc}"})
        if crop is not None:
            content.append({"type": "input_image", "image_url": encode_png_bytes_data_url(crop), "detail": "high"})
    response = client.responses.parse(
        model=model,
        instructions=_GROUP_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        text_format=_SiblingRegionBatchResult,
    )
    parsed = response.output_parsed
    return parsed.decisions if parsed else []


def propose_synthetic_groups(
    document: ArticDocument,
    client=None,
    model: str = GROUP_MODEL,
    *,
    batch_size: int = _BATCH_SIZE,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> list[str]:
    """When a VLM judges that sibling content nodes form one conceptual
    unit by placement/repetition/meaning even with no explicit
    heading/container in the source, creates a new `Group` node
    (`synthetic=True`) and reparents them under it — see `schema.py`'s
    Group node description and the module docstring's
    "propose_synthetic_groups" paragraph. Modifies `document` in place.

    **Candidate generation (deterministic)**: doesn't finalize any
    judgment. Only builds a list of sibling nodes ("a region") sharing the
    same parent per the current `PARENT_OF` edges — if `propose_edges`'s
    HEADING_PARENT judgment/`nest_numbered_headings` were run first, the
    siblings are already narrowed down under the correct section, and this
    function uses that result as-is. A region bigger than
    `_MAX_GROUP_REGION_NODES` is skipped (an arbitrary split could cut a
    real group across the boundary, so skipping is chosen over splitting
    with no empirical basis).

    **Judgment (VLM)**: for each region, shows the actual layout crop (only
    for supported formats — PDF/XLSX/PPTX, see
    `_build_layout_crop_renderer`) and a preview of the source text, and
    asks it to group only when there's support from at least 2 independent
    cues among spatial/visual/structural/semantic/boundary signals. States
    explicitly that `group_type` is a label the VLM attaches, not text from
    the source (so a string not present in the source isn't stored as if it
    were content).

    **Applying it**: adds one new Group node per group to
    `document.nodes`, removes the existing `parent -PARENT_OF-> member`
    edges, and reparents as `parent -PARENT_OF-> Group -PARENT_OF-> each
    member`. Since Group is a brand-new node this time, a cycle can never
    arise structurally (unlike HEADING_PARENT reparenting, no `_is_ancestor`
    check is needed).

    **Partial failure**: if a batch call fails, only that batch's regions
    are skipped and the rest are returned as-is (the same principle as the
    other LLM batch code) — the worst failure mode is "failing to create a
    group," not "creating a wrong one."

    Returns: the list of newly created Group node ids (for logging/verification).
    """
    if client is None:
        from openai import OpenAI  # lazy import — openai isn't needed unless this function is called

        client = OpenAI()

    by_region = _children_by_parent(document)
    regions = [
        (parent_id, members)
        for (parent_id, _page_index), members in by_region.items()
        if 2 <= len(members) <= _MAX_GROUP_REGION_NODES
    ]
    if not regions:
        return []

    layout_crop_fn, close_layout_resources = _build_layout_crop_renderer(document)
    try:
        region_inputs = [
            (parent_id, members, layout_crop_fn(members[0], members[1:])) for parent_id, members in regions
        ]
    finally:
        close_layout_resources()

    def fetch(batch):
        batch_payload = [(members, crop) for _parent_id, members, crop in batch]
        try:
            return batch, _decide_sibling_groups_batch(client, model, batch_payload)
        except Exception:  # noqa: BLE001 — leave just the failed batch as-is
            return batch, []

    results = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(fetch, batch) for batch in _chunked(region_inputs, max(1, batch_size))]
        for future in as_completed(futures):
            results.append(future.result())

    document_order = {node.id: index for index, node in enumerate(document.nodes)}
    proposals: list[tuple[str, list[Node], _SyntheticGroupCandidate]] = []
    claimed_ids: set[str] = set()
    for batch, decisions in results:
        for decision in decisions:
            if not (0 <= decision.region_index < len(batch)):
                continue
            parent_id, members, _crop = batch[decision.region_index]
            for group in decision.groups:
                indices = list(dict.fromkeys(group.member_indices))
                if len(indices) < 2 or any(index < 0 or index >= len(members) for index in indices):
                    continue
                group_members = [members[index] for index in indices]
                ids = {node.id for node in group_members}
                if ids & claimed_ids:
                    continue
                claimed_ids.update(ids)
                proposals.append((parent_id, group_members, group))

    filename = Path(document.source_path).name
    created: list[str] = []
    for counter, (parent_id, group_members, group) in enumerate(
        sorted(proposals, key=lambda item: min(document_order[n.id] for n in item[1]))
    ):
        group_members.sort(key=lambda node: document_order[node.id])
        existing_ids = {node.id for node in document.nodes}
        while f"content:{filename}:grp{counter}" in existing_ids:
            counter += 1
        group_id = f"content:{filename}:grp{counter}"
        group_node = Node(
            id=group_id,
            type=NodeType.GROUP,
            name=f"Group ({group.group_type})" if group.group_type else "Group",
            properties={
                "synthetic": True,
                "group_type": group.group_type,
                "confidence": group.confidence,
                "basis": group.basis,
                "rationale": group.rationale,
                "proposed_by": f"llm:{model}",
            },
        )
        document.nodes.append(group_node)
        member_ids = {n.id for n in group_members}
        document.edges = [
            e for e in document.edges
            if not (e.type == EdgeType.PARENT_OF and e.source_id == parent_id and e.target_id in member_ids)
        ]
        document.edges.append(Edge(type=EdgeType.PARENT_OF, source_id=parent_id, target_id=group_id))
        document.edges.extend(
            Edge(type=EdgeType.PARENT_OF, source_id=group_id, target_id=n.id) for n in group_members
        )
        created.append(group_id)
    return created


def apply_vlm_enrichment(
    document: ArticDocument, client=None, model: str = DEFAULT_MODEL, *, include_text_anchors: bool = True,
    slide_images: dict[int, str | Path] | None = None,
    trace_dir: str | Path | None = None,
) -> list[str]:
    """`model` overrides every step's own default (`MERGE_MODEL`/
    `SEMANTIC_MERGE_MODEL`/`GROUP_MODEL`, all normally `DEFAULT_MODEL`) in
    one place — this bundle is exactly the "use the VLM or not" on/off
    switch the CLI's `--vlm-enrichment` flag maps to, so a caller that wants
    a non-default model shouldn't have to call the underlying functions by
    hand just to pass `model=` to each.

    Optional `slide_images` maps every zero-based PPTX slide index to an
    external PNG/JPEG. These supply full-slide visual evidence to heading,
    relation, and synthetic-group decisions; grouping runs last for PPTX
    documents with attached slide images. No external converter is invoked.
    Without supplied slide images, native PPTX reconstruction supplies layout evidence.

    `trace_dir` records the bundle's model calls and graph snapshots locally;
    see `propose_edges` for the relation/heading diagnostic details.

    A convenience function that runs `merge_semantic_text_groups` +
    `merge_fragmented_text` + `propose_edges` (CAPTION_OF/REFERENCES/
    HEADING_PARENT, `include_text_anchors` forwarded) + `nest_numbered_headings`
    all at once — exactly the bundle the CLI's `--vlm-enrichment` flag and
    the demo app's "VLM enrichment" checkbox use. `nest_numbered_headings`
    needs no VLM, but it's always bundled in since it directly fills a gap
    `propose_edges`'s HEADING_PARENT judgment structurally can't close on
    its own (a hierarchical "N.M" subheading nesting the model has no
    notion of — see that function's docstring; a *flat* "1)/2)/3)..."
    enumerated-sibling run is instead handled inside `propose_edges` itself,
    by collapsing the whole run into one HEADING_PARENT question rather
    than patching it up afterward — see `_propose_text_heading_parents`'s
    docstring); the rest are the same `OPENAI_API_KEY`/model call anyway, so
    there's no reason for a call site that just asks "use the VLM or not"
    as one switch (the CLI, the demo) to turn them on/off separately.

    **This function doesn't erase each step's trust-level differences** —
    `merge_semantic_text_groups` merges semantic units from the 2D layout,
    `merge_fragmented_text` merges same-line fragments into one (both a VLM
    judgment), `propose_edges`'s CAPTION_OF/REFERENCES still only adds
    edges while its HEADING_PARENT reparents structurally (a VLM judgment
    either way), and `nest_numbered_headings` also reparents structurally
    but by a deterministic pattern only (yet another different trust
    level). This function only reduces the repetition of calling those
    steps together by hand each time — call the original functions
    directly if you only need some of them.

    The order is `merge_semantic_text_groups` -> `merge_fragmented_text` ->
    `propose_edges` -> `nest_numbered_headings`. Running the merges first is
    because it's better for the Table/Image-surrounding context text the
    other functions see to be clean rather than fragmented, and putting
    `nest_numbered_headings` right after `propose_edges` is to definitively
    fix, using the number pattern, a subheading-to-parent-section
    relationship the VLM missed (or misplaced) (confirmed: in `1706.03762`,
    11 of 13 subheadings stayed directly under the Artifact with the VLM
    step alone, and `nest_numbered_headings` cleaned up all of them using
    only the number pattern). `client` is shared by the functions that need
    the API (since each would otherwise create its own new
    `openai.OpenAI()` when `client=None`).

    The return value is the list of node ids `propose_edges`'s HEADING_PARENT
    judgment actually reparented (captured right after that call, before
    `nest_numbered_headings` adds its own — the existing signature is kept)
    — merged/re-nested node ids can each be found in `document.nodes` as
    nodes with `properties["merged_from"]`/`properties["nested_by"]`, and
    added CAPTION_OF/REFERENCES edges can be checked directly in
    `document.edges`.
    """
    if slide_images is not None:
        from .slide_images import attach_pptx_slide_images

        attach_pptx_slide_images(document, slide_images)

    if client is None:
        from openai import OpenAI  # lazy import — same reason as propose_edges

        client = OpenAI()

    if trace_dir is not None:
        with trace_session(client, trace_dir, document) as traced:
            return apply_vlm_enrichment(document, client=traced, model=model,
                                        include_text_anchors=include_text_anchors)

    merge_semantic_text_groups(document, client=client, model=model)
    merge_fragmented_text(document, client=client, model=model)
    proposals = propose_edges(document, client=client, model=model, include_text_anchors=include_text_anchors)
    promoted = [
        e.target_id for e in document.edges
        if e.type == EdgeType.PARENT_OF and "reparented_from" in e.properties
    ]
    nest_numbered_headings(document)
    document.edges.extend(proposals)
    if document.format == "pptx" and any(
        n.type == NodeType.ARTIFACT and n.properties.get("slide_image_path")
        for n in document.nodes
    ):
        propose_synthetic_groups(document, client=client, model=model)
    return promoted


# ---------------------------------------------------------------------------
# Narrowing an ambiguous double proposal from the deterministic CAPTION_OF
# heuristic (docx.py/pptx.py/pdf.py) with a VLM — replaces "a proposal, not
# final, for human review" with VLM verification.
# ---------------------------------------------------------------------------

DISAMBIGUATION_MODEL = DEFAULT_MODEL


class _CaptionDisambiguation(BaseModel):
    confirmed_indices: list[int] = Field(
        description="All zero-based candidate-image indices actually described by the caption; may contain multiple or no indices."
    )
    rationale: str = Field(description="One concise sentence explaining the decision.")


_DISAMBIGUATION_INSTRUCTIONS = """\
Determine which candidate images are actually described by one caption.

A deterministic heuristic may attach a caption to both neighboring images
when it cannot tell which one is correct. Inspect the actual pixels and narrow
the candidates to one, multiple, or none.

Rules:
- Match the caption's concrete content against each candidate's pixels. For
  example, do not select a pipe photo for a caption describing a circuit board.
- Be conservative when uncertain. A false confirmation is worse than a missed
  one because this decision removes unconfirmed edges.
- Select multiple candidates only when the caption genuinely describes all of
  them.
"""


def _ambiguous_caption_groups(document: ArticDocument) -> list[tuple[Node, list[tuple[Edge, Node]]]]:
    """Groups CAPTION_OF edges by source (the caption Text) and returns
    only the groups with 2+ targets — i.e. ambiguous ones where the
    deterministic heuristic attached to both the preceding and following
    neighbor. A CAPTION_OF with only one target is already certain, so it's
    left untouched (avoids wasting a VLM call)."""
    nodes_by_id = document.nodes_by_id()
    by_source: dict[str, list[Edge]] = {}
    for e in document.edges:
        if e.type == EdgeType.CAPTION_OF:
            by_source.setdefault(e.source_id, []).append(e)

    groups: list[tuple[Node, list[tuple[Edge, Node]]]] = []
    for source_id, edges in by_source.items():
        if len(edges) < 2:
            continue
        source = nodes_by_id.get(source_id)
        if source is None:
            continue
        candidates = [(e, nodes_by_id[e.target_id]) for e in edges if e.target_id in nodes_by_id]
        if len(candidates) >= 2:
            groups.append((source, candidates))
    return groups


def resolve_ambiguous_captions(
    document: ArticDocument, client=None, model: str = DISAMBIGUATION_MODEL
) -> list[str]:
    """Narrows the ambiguous double proposal created by docx.py/pptx.py/
    pdf.py's deterministic CAPTION_OF heuristic (one caption text attached
    to both the preceding and following image) with a VLM, modifying
    `document` **in place** — removes the unconfirmed side's edge from
    `document.edges`.

    A different trust level from `propose_edges` (returns new edges as a
    "proposal," adoption is the caller's call) — this function **doesn't
    create a new claim, it only removes whichever of the already-existing
    candidates isn't grounded** (the worst failure mode is "removing a
    correct one," not "inventing one that doesn't exist"). So, like
    `caption_images.py`, this is applied directly in place — though that one
    filled in pure supplementary info (`vlm_description`), while this
    changes the graph's **structure** (edges), so it isn't at exactly the
    same trust level: on failure (a call exception/empty response/no
    pixels), it safely **leaves it ambiguous** as before (neither removes
    both nor arbitrarily keeps just one) — preserving a state a person can
    still review.

    Returns: the list of caption node ids that were actually resolved (for
    logging/verification).
    """
    if client is None:
        from openai import OpenAI  # lazy import — openai isn't needed unless this function is called

        client = OpenAI()

    groups = _ambiguous_caption_groups(document)
    resolved: list[str] = []
    edges_to_remove: set[tuple[EdgeType, str, str]] = set()

    for source, candidates in groups:
        pixel_paths = [_anchor_pixel_path(tgt) for _, tgt in candidates]
        if any(p is None or not Path(p).exists() for p in pixel_paths):
            continue  # if any candidate has no pixel, judgment isn't possible — safely leave it ambiguous

        caption_text = source.properties.get("text", source.name)
        content: list[dict] = [{"type": "input_text", "text": f"Caption text: {caption_text!r}"}]
        for i, (_, tgt) in enumerate(candidates):
            content.append({"type": "input_text", "text": f"[Candidate {i}] {tgt.name}"})
            content.append({
                "type": "input_image",
                "image_url": encode_image_data_url(pixel_paths[i]),
                "detail": "high",
            })

        try:
            response = client.responses.parse(
                model=model,
                instructions=_DISAMBIGUATION_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                text_format=_CaptionDisambiguation,
            )
        except Exception:  # noqa: BLE001 — e.g. an API error, leave just this group ambiguous
            continue

        parsed = response.output_parsed
        if parsed is None:
            continue

        confirmed = set(parsed.confirmed_indices)
        for i, (e, _) in enumerate(candidates):
            if i not in confirmed:
                edges_to_remove.add((e.type, e.source_id, e.target_id))
        resolved.append(source.id)

    if edges_to_remove:
        document.edges = [
            e for e in document.edges if (e.type, e.source_id, e.target_id) not in edges_to_remove
        ]

    return resolved
