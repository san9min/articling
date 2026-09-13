# Articling Interactive Demo

A local upload demo — upload a file and it interactively shows the graph
(`ArticDocument`) articling produced. A form common in open-source projects:
"spin up a local server, check it in the browser."

```
Browser (file upload) → articling.extract() result graph (interactive visualization via vis-network)
```

## Running it

```bash
# 1) articling's own dependencies (from the repo root, once)
pip install -e .

# 2) demo-only dependencies
pip install -r demo/requirements.txt

# 3) run the server
python demo/app.py
# -> http://127.0.0.1:8420
```

Upload (or drag-and-drop) a `.docx`/`.pptx`/`.xlsx`/`.pdf` file in the
browser and a `File → Artifact → Text/Table/Image` hierarchical graph
(`PARENT_OF`/`NEXT`) appears. Clicking a node shows text/table grid/image in
a detail panel (a table shows the engine's composited capture PNG, an image
shows the original bytes). The summary bar at the top shows node/edge
counts, elapsed time, and `scaffold.check_invariants` results as chips.

The upload header expands for long filenames and wrapped options. Progress
and error messages occupy their own row above the graph, so they remain
readable without overlapping the graph toolbar.

To retain VLM diagnostic evidence, start the demo with
`ARTICLING_TRACE_DIR=/tmp/articling-traces python3 demo/app.py` from the repository
root. With VLM enrichment enabled, each extraction saves a separate run under
that directory, grouped by session ID. The trace includes document text/images,
actual requests, parsed judgments and before/after graphs, but no API key.
Tracing is off when this environment variable is unset; it does not add API calls.

For a document with many nodes (hundreds), the graph auto-shrinks a lot to
fit everything on screen, making it hard to click an individual node
precisely, especially a small Image node — zoom in with the **+ / − / Fit**
buttons (or scroll) at the bottom-right of the graph and click.

PDF always catches figures drawn as vector graphics (plots/heatmaps, etc.,
not raster-embedded) as Image too (`enrich_pdf_figures` — needs no VLM/API
key, so it's always on with no checkbox. See README "PDF Vector Figure
Detection").

### VLM enrichment (opt-in — needs the most careful handling)

Turning on the **"VLM enrichment"**
checkbox next to the upload button runs
`relations.propose.apply_vlm_enrichment(doc)` right after extraction,
applying every VLM enrichment — the same behavior as the CLI's
`--vlm-enrichment` flag. Under the hood it's still four steps at
different trust levels (only #4 needs no API; the rest need the same
`OPENAI_API_KEY`/model call, so there was no reason to expose them as
separate switches in the frontend — that doesn't erase the trust-level
differences between the steps below):

1. `relations.propose.merge_semantic_text_groups(doc)` — PDF only. Merges
   PDF Text nodes into complete semantic units from the page's 2D layout
   (a name+affiliation+email, a title split across blocks, ...).
2. `relations.propose.merge_fragmented_text(doc)` — PDF only (silently a
   no-op on other formats). Among same-line text fragments (the problem of
   inline math with sub/superscripts splitting into several blocks) whose
   **order**
   `extractors/pdf._reorder_same_line_blocks` has already fixed, this
   merges the ones the model judges are "really one split expression." Pure
   geometry alone can't tell "a split expression" from "two unrelated
   things that happen to share a line" (confirmed: two authors' names on
   the same line), so the judgment is left to a VLM.
3. `relations.propose.propose_edges(doc, include_text_anchors=True)` — for
   each Table/Image, judges CAPTION_OF/REFERENCES **and** HEADING_PARENT
   together in **one call**: CAPTION_OF/REFERENCES proposals are added
   straight onto `doc.edges`, while a HEADING_PARENT pick **reparents** the
   deterministically created `Artifact → content` PARENT_OF to `heading
   Text → content` (deepening the tree by one level) directly — getting
   closer to a real document outline. HEADING_PARENT crosses the trust
   boundary much further than CAPTION_OF/REFERENCES (additive only) — it
   **deletes a deterministic structural edge** and reasserts a new one
   based on an LLM judgment. Since the worst failure mode is "reparenting
   the structure incorrectly," before using it see
   [relations.md](../articling/.agents/skills/articling/references/relations.md)
   or the README's "PARENT_OF Reparenting" section. `include_text_anchors=True`
   additionally judges HEADING_PARENT for every paragraph Text too — its
   own separate, batched pass (since CAPTION_OF/REFERENCES never apply to a
   Text anchor), confirmed to cut the time from 753s to 98s (about 7.7x)
   for that many-paragraph case (see README "Batching and Concurrency").
   Cycle risk is checked before every reparent to prevent it (`_is_ancestor`).
   A flat "1) .../2) .../3) ..." enumerated-sibling run is collapsed into
   **one** HEADING_PARENT question rather than one per member — a bounded
   candidate window can push a section's own heading out of a later
   member's candidates entirely (confirmed on a real xlsx document: "1)"'s
   own window reached the shared heading fine, but "3)"'s own window was
   exactly consumed by "1)", "2)", and an unrelated sub-bullet before ever
   reaching that same heading a few lines further up — no layout image
   rescues this, since the model can only pick an index from the candidates
   it was actually given). The run's own numbering is used to ask once,
   using the run's first (heading-closest) member's own window, and apply
   that single answer to every member.
4. `relations.propose.nest_numbered_headings(doc)` — needs no VLM/API, pure
   text pattern matching, for a different (hierarchical "N.M") pattern.
   Step #3's HEADING_PARENT judgment only considers each content node
   individually and has no notion that a heading numbered "3.1" should go
   under "3" — confirmed (`1706.03762`): only 2 of 15 subheadings ended up
   under their parent section with step #3 alone. This function fills that
   gap using only number parsing ("3.2.1" goes under "3.2", not "3" — since
   the parent is determined purely by number, this can never create a cycle
   structurally, unlike #3, so it has no cycle check).

Off by default (the same opt-in principle as the CLI — it costs money, and
it's a "proposal" premised on human review, not a final answer. See README
"CAPTION_OF / REFERENCES Proposals").

- Requires `OPENAI_API_KEY`. The server (`demo/app.py`) automatically reads
  the repo root's `.env` if present (`load_dotenv`) — otherwise pass it as a
  shell environment variable.
- Even if the key is missing or the API call fails (rate limit, etc.), the
  deterministic graph (`PARENT_OF`/`NEXT`, `CAPTION_OF` made by the
  docx/pptx/pdf caption-prefix heuristic) is still returned normally, with
  the cause shown as a warning chip in the summary bar.
- The graph view's hierarchy level is computed from the actual depth
  following `PARENT_OF` edges, not a static type table (`app.js`'s
  `computeLevels`) — so a reparented Table/Image/Text is drawn one level
  deeper, right under its new parent (the heading Text), not its original
  spot. Clicking a node shows, in the detail panel, a "Parent (promoted
  heading)" field for which heading it was moved under and why (the
  rationale).

### Saved-session sidebar

The left sidebar lists sessions that have a
`demo/.sessions/<id>/result.json`, most recent first (`GET /api/sessions`,
`app.js`'s `refreshSessionList`). The title is that session's original
filename; clicking it loads that graph without re-extracting, via `GET
/api/session/{id}` (the same path as the existing `loadSession` — the URL's
`?session=<id>` is also updated along with it).

**Re-extracting with the same filename doesn't create a new session, it
overwrites the existing one** (`app.py`'s `_find_existing_session_id` —
looks only at the filename, not the content). The `session_id` stays the
same; the previous captures/original file/`result.json` are deleted whole
and recreated — instead of piling up duplicate entries with the same title
in the sidebar, the existing entry is updated with the latest result and
moves to the top of the list. A session directory with no `result.json`
(e.g. extraction failed partway) doesn't show up in the sidebar.

Each item has an **×** button on the right to delete that session (`DELETE
/api/session/{id}` — after a confirmation dialog, deletes the original
file/captures/`result.json` wholesale from disk, irreversible). Deleting
the session currently being viewed also clears the graph on the right. The
sidebar itself can be collapsed/expanded with the `☰` button in the top bar.

## Known limitations

- PDF table detection (`enrich_pdf_tables`), VLM captioning
  (`caption_content_nodes`), and `resolve_ambiguous_captions` (narrowing an
  ambiguous CAPTION_OF) aren't wired into this demo — if needed, add them
  right after `demo/app.py`'s `EXTRACTORS[suffix](...)` call.
- Upload size cap is 60MB (a constant near the top of `demo/app.py`) — this
  keeps demo responses from growing unbounded, not a limit of the engine
  itself.
- Uploaded files and capture artifacts stay under `demo/.sessions/<id>/`
  (gitignored, not deleted on server restart — clean up manually if
  needed).
- The extraction result (nodes/edges/summary) itself is also saved as
  `result.json` in the same directory. As long as `?session=<id>` stays in
  the URL (attached automatically once extraction finishes), reloading the
  page returns that same JSON via `GET /api/session/{id}` with no
  re-extraction — useful for repeatedly checking a result that mixes in a
  non-deterministic step like VLM enrichment, or whether capture/annotation
  PNGs came out as expected, without variation from re-extraction.
