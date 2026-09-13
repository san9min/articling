# Extraction details

[Back to README](../README.md)

## PPTX

PPTX extraction preserves OLE object metadata and original preview bytes,
including WMF/EMF. Normalized bounding boxes include nested group translation,
scaling, rotation, and reflection; raw geometry stays in its parent coordinate
system. A textless shape drawn directly on top of a picture (a highlight box,
an unglued arrow) never becomes its own node, but is composited onto that
picture's saved image instead of being lost, mirroring how XLSX preserves
annotations drawn on top of a photo. A native chart becomes a Table node
(its categories/series data, not a re-rendered picture); SmartArt has no
python-pptx object model or rendering support at all, so its typed text
labels are recovered from the diagram's data instead of being lost. A
native table also gets a visual capture (cell shading, merges, borders),
the same as XLSX tables, since the slide-reconstruction renderer already
knows how to draw one.

### Slide reconstruction

PPTX VLM enrichment reconstructs slide images locally with `python-pptx` and
Pillow. No LibreOffice, PowerPoint, AppleScript, or external converter runs.

Reconstruction reads source shapes in paint order, including background artwork,
picture cropping/transparency/rotation, common shapes and connectors, text runs,
and merged table cells. It preserves the slide aspect ratio. Font substitution
and text fitting keep text inside its own box; they can differ from PowerPoint's
typography. WMF/EMF previews, SmartArt, complex geometry/effects, and table theme
styles are not fully reproduced. Per-slide warnings identify substitutions,
fitted text, and skipped shapes. A failed shape does not abort other shapes.

The VLM receives a clean reconstruction followed by a copy with `visual_id`
labels. These images are explicitly marked approximate; missing visual details
must not be treated as proof of absence. Each slide is cached across anchors
within an enrichment stage. Native extraction itself still makes no VLM calls.

### Heading assignment

For PPTX, each text block can select its parent heading from all other Text
nodes on the same slide, so intervening sections cannot push the slide title
out of its candidate window. The VLM still decides which heading owns it.
The slide title is an eligible parent of top-level section blocks, including
blocks that contain both a section heading and its body text.

## DOCX and numbered headings

Numbered headings are scoped to preceding, still-open sections within one
Artifact. Native Word list identities do not bridge heading or parent
boundaries, and `numId=0` is treated as numbering removal. Both heading
reparenting and graph checks now guard against parent cycles.

## PDF

PDF extraction also restores tightly adjacent horizontal single-line text
fragments before VLM enrichment. Matching baseline, font, size and color are
required; intervening drawings/images block joining. Numeric-only fragments,
rotated labels and multi-line groups remain separate. Nodes retain original
`pdf_text_lines` (line/span geometry in PDF points), `pdf_block_numbers`, and
`native_merged_by="same_line_continuation"` when joined; node `bbox` remains
normalized to 0–1000. This conservative geometric heuristic does not establish
semantic equivalence. Optional VLM passes still handle unresolved groups.

PDF bookmarks establish native heading-to-heading hierarchy when their titles
match extracted Text blocks; destination coordinates disambiguate repeated
titles. This does not automatically assign all intervening body text to a
section. Confirmed outline parents are preserved during VLM enrichment.

## XLSX heading assignment

For XLSX heading assignment, consecutive numbered text siblings share one
VLM question. Tables and images anchored at the same row within a sheet
share heading candidates while retaining separate judgments and their own
caption/reference eligibility. See [relation enrichment details](../articling/.agents/skills/articling/references/relations.md).
