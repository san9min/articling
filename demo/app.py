"""Articling interactive demo — a local server.

Upload a file and it interactively shows the `ArticDocument` graph
articling built (File/Artifact/Text/Table/Image/Group nodes +
PARENT_OF/NEXT/CAPTION_OF/REFERENCES edges). Clicking a node shows details
like text/table grid/image (a table shows the engine's composited capture
PNG, an image shows the original bytes) / Group basis
(group_type·confidence·basis).

PDF always has `extractors.pdf_figures.enrich_pdf_figures` (vector-graphics
figure detection) on — since it's pure geometric detection needing no
VLM/API key (see README "PDF Vector Figure Detection"), it's safe to always
apply with no checkbox. Even on failure (rare, since it's a deterministic
step) only a warning shows in the summary bar and the rest of the result is
returned normally.

Turning on the `vlm_enrichment` option (one checkbox in the frontend, off
by default) runs `relations.propose.apply_vlm_enrichment(doc)` right after
extraction, applying every VLM-based enrichment — the same behavior as the
CLI's `--vlm-enrichment` flag. Under the hood it's still four steps at
different trust levels:

1. `relations.propose.merge_semantic_text_groups(doc)` — PDF only. Merges
   PDF Text nodes into complete semantic units from the page's 2D layout
   (a name+affiliation+email, a title split across blocks, ...).
2. `relations.propose.merge_fragmented_text(doc)` — PDF only (silently a
   no-op on other formats). Among same-line text fragments whose **order**
   `extractors/pdf._reorder_same_line_blocks` has already fixed, merges the
   ones the model judges are "really one split expression" (the problem of
   inline math with sub/superscripts splitting into several Text nodes,
   see `docs/vlm-integration-research.md` §11.2). Pure geometry alone can't
   tell "a split expression" from "two unrelated things that happen to
   share a line" (confirmed: two authors' names on the same line), so the
   judgment is left to a VLM.
3. `relations.propose.propose_edges(doc, include_text_anchors=True)` — for
   each Table/Image, judges CAPTION_OF/REFERENCES **and** HEADING_PARENT
   together in one call: CAPTION_OF/REFERENCES are added to the graph as
   proposals (see below), while a HEADING_PARENT pick reparents the
   deterministically created `Artifact -> content` PARENT_OF to
   `heading Text -> content` (deepening the tree) directly — the one point
   in this project where LLM involvement is opened up for PARENT_OF at all,
   so it needs the most careful handling (see the function docstring).
   `include_text_anchors=True` additionally judges HEADING_PARENT for every
   paragraph Text too (its own separate, batched pass, since
   CAPTION_OF/REFERENCES never apply to a Text anchor) — API calls grow
   with the number of paragraphs and can get slow. Cycle prevention is
   handled by the function itself via `_is_ancestor`. A flat
   "1) .../2) .../3) ..." enumerated-sibling run is collapsed into **one**
   HEADING_PARENT question rather than one per member — a bounded candidate
   window can push a section's own heading out of a later member's
   candidates entirely (confirmed on a real xlsx document: "1)"'s own
   window reached the shared heading fine, but "3)"'s own window was
   exactly consumed by "1)", "2)", and an unrelated sub-bullet before ever
   reaching that same heading a few lines further up), so the run's own
   numbering is used to ask once, using the run's first (heading-closest)
   member's own window, and apply that single answer to every member.
4. `relations.propose.nest_numbered_headings(doc)` — needs no VLM/API, pure
   text pattern matching, for a different (hierarchical "N.M") pattern.
   Step #3's HEADING_PARENT judgment only considers each content node
   individually and has no notion that "3.1" should go under "3" —
   confirmed (`1706.03762`): only 2 of 15 subheadings ended up under their
   parent section with step #3 alone. This function always correctly fills
   that gap using only number parsing ("3.2.1" goes under "3.2", not "3").

`apply_vlm_enrichment` itself (#4 the only exception, needing no
API) is just a convenience function bundling these because the rest need the same
OPENAI_API_KEY/model call anyway, so there's no reason to expose them
individually in the frontend — it doesn't erase these steps' trust-level
differences (structural merge/reparent vs. deterministic reparent vs.
additive only) (see the function docstrings). Why it's opt-in: see README
"CAPTION_OF / REFERENCES Proposals" — it costs money and is a "proposal"
premised on human review, not a final answer.

The `synthetic_groups` option (a separate frontend checkbox, off by
default) runs `relations.propose.propose_synthetic_groups(doc)` — when a
VLM judges that several sibling nodes (e.g. several author
name+affiliation+email blocks) form one conceptual unit even with no
explicit heading/container in the source, it creates a new `Group` node
(`synthetic=True`) and reparents them under it. It's at the same level as
`propose_edges`'s HEADING_PARENT role (creating new structure and
reparenting), so it isn't bundled into `apply_vlm_enrichment` and is
exposed as a separate checkbox — it can be turned on independently of
`vlm_enrichment`, but running it after heading reparenting has already
finished (turning that on first) gives a narrower, more accurate sibling
candidate region (confirmed: `docs/vlm-integration-research.md` §15).

**`GET /api/status`** (`openai_key_configured`) and `/api/extract`'s
`openai_api_key` form field — let the browser accept a key directly and try
VLM enrichment even when the server's `.env` has no key (with no restart
needed after editing it) (it used to be that you'd only see the "skipped,
no key" warning after turning on the checkbox and finishing the whole
upload — bad UX, fixed 2026-09-05). A key entered this way is used only for
that one request and never stored anywhere (never written to the session
directory, the response JSON, or the server log) — on the browser side it
only lives in `sessionStorage` and disappears when the tab closes (see
`app.js`). Good enough for a local-only (127.0.0.1) demo, but never use
this for anything exposed to a network.

Each extraction result is saved as-is to
`demo/.sessions/<session_id>/result.json`, and keeping `?session=<session_id>`
in the URL (only changes browser history, not a redirect) means reloading
or bookmarking loads that same graph again via `GET /api/session/{id}` with
no re-extraction (see `app.js`'s `loadSession`) — since a non-deterministic
step like VLM enrichment mixed in means even the same file can produce a
different result on re-extraction, it needs to be possible to pin and
revisit "that exact result just produced" when visually verifying an
engine artifact itself, like a capture PNG or an annotation composite.

Running it:
    pip install -r demo/requirements.txt
    python demo/app.py
    # -> http://127.0.0.1:8420
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # import the articling/ package directly, no pip install needed

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")  # CONTRIBUTING.md convention: read OPENAI_API_KEY etc. from .env

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from articling.extractors import docx as ext_docx
from articling.extractors import pdf as ext_pdf
from articling.extractors import pptx as ext_pptx
from articling.extractors import xlsx as ext_xlsx
from articling.schema import ArticDocument, NodeType
from articling.scaffold import check_invariants

SESSIONS_DIR = Path(__file__).resolve().parent / ".sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # 60MB — for demo purposes, handles a large xlsx but isn't unbounded

EXTRACTORS = {
    ".docx": ext_docx.extract,
    ".pptx": ext_pptx.extract,
    ".xlsx": ext_xlsx.extract,
    ".pdf": ext_pdf.extract,
}

app = FastAPI(title="articling demo")

_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_original_filename(filename: str, suffix: str) -> str:
    """Uses the actual uploaded filename as the filename saved to disk —
    since `articling.scaffold.file_node` uses `path.name` as-is for the
    File node's `name`/`id` (giving it a fixed name like "original.pdf"
    would make the graph preview/node label show just "original" instead of
    the real filename), the real name has to be preserved starting from
    here for the graph to show it.

    `Path(...).name` first drops any directory part (path manipulation like
    `../`), and only characters that could be problematic on a filesystem
    are replaced with `_` (spaces/Unicode/Korean are left as-is — most
    filesystems support them fine). If the name is empty or just dots
    (".", ".."), it safely falls back to `original{suffix}`."""
    name = Path(filename or "").name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip()
    if not name or set(name) <= {"."}:
        return f"original{suffix}"
    return name[:200]  # avoid path-length issues — a generous cap


def _summarize(doc: ArticDocument) -> dict[str, Any]:
    node_counts = {t.value: 0 for t in NodeType}
    for n in doc.nodes:
        node_counts[n.type.value] += 1
    edge_counts: dict[str, int] = {}
    for e in doc.edges:
        edge_counts[e.type.value] = edge_counts.get(e.type.value, 0) + 1
    return {
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "invariant_violations": check_invariants(doc.nodes, doc.edges),
    }


def _serialize_nodes(doc: ArticDocument, session_id: str, session_dir: Path) -> list[dict[str, Any]]:
    """Converts the absolute paths in Node.properties (capture_path/image_path)
    into a `/files/...` URL the frontend can use directly, adding them as
    `capture_url`/`image_url` (the original properties are kept exactly as
    the export schema — so the demo doesn't distort the schema)."""
    out = []
    for n in doc.nodes:
        props = dict(n.properties)
        for path_key, url_key in (("capture_path", "capture_url"), ("image_path", "image_url")):
            raw = props.get(path_key)
            if raw:
                try:
                    rel = Path(raw).resolve().relative_to(session_dir.resolve())
                    props[url_key] = f"/files/{session_id}/{rel.as_posix()}"
                except ValueError:
                    pass  # a path outside the session — the demo can't show it, skip silently
        out.append({"id": n.id, "type": n.type.value, "name": n.name, "properties": props})
    return out


def _find_existing_session_id(filename: str, vlm_enrichment: bool, synthetic_groups: bool) -> str | None:
    """If a session already exists with the same original filename *and* the
    same enrichment settings (the `filename`/`vlm_enrichment`/
    `synthetic_groups` fields in `result.json`, the latter two as requested
    on upload, exactly as the browser sent them), returns that `session_id`
    — so that re-extraction overwrites this session instead of creating a
    new one (avoiding duplicate entries with the same title piling up in the
    sidebar). Settings are part of the identity on purpose: the base
    (deterministic) graph and a VLM-enriched one are different results worth
    keeping side by side, not overwriting each other just because the
    filename matches — only re-running with the *same* settings is treated
    as "redo this" and overwrites. Compares by filename/settings only, not
    file content — re-uploading different content under the same name and
    settings is still treated as "the same session" and overwritten."""
    for session_dir in SESSIONS_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        result_path = session_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (
            isinstance(data, dict)
            and data.get("filename") == filename
            and bool(data.get("vlm_enrichment")) == vlm_enrichment
            and bool(data.get("synthetic_groups")) == synthetic_groups
        ):
            return session_dir.name
    return None


@app.get("/api/sessions")
def list_sessions() -> JSONResponse:
    """The list of saved sessions the left sidebar shows — lightly returns
    just the title (original filename) pulled from each session
    directory's `result.json` (the graph body is loaded separately on
    click, via `GET /api/session/{id}`). A directory with no `result.json`
    (e.g. one left uncleaned after a failed extraction) is skipped. Sorted
    most-recent-first by `result.json`'s mtime — an overwrite from
    re-extraction bumps the mtime, so it naturally floats to the top."""
    items: list[dict[str, Any]] = []
    for session_dir in SESSIONS_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        result_path = session_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        items.append(
            {
                "session_id": session_dir.name,
                "filename": data.get("filename") or session_dir.name,
                "mtime": result_path.stat().st_mtime,
                "vlm_enrichment": bool(data.get("vlm_enrichment")),
                "synthetic_groups": bool(data.get("synthetic_groups")),
            }
        )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return JSONResponse(items)


@app.get("/api/status")
def get_status() -> JSONResponse:
    """Called once by the frontend on page load to check "does the server
    already have OPENAI_API_KEY" — if so, the VLM enrichment checkbox turns
    on right away; if not, a field for entering a key in the browser is
    shown (see `openai_api_key` in `extract_document` below). The key value
    itself is never put in the response — only whether it exists."""
    return JSONResponse({"openai_key_configured": bool(os.environ.get("OPENAI_API_KEY"))})


@app.post("/api/extract")
def extract_document(
    file: UploadFile = File(...),
    vlm_enrichment: bool = Form(False),
    synthetic_groups: bool = Form(False),
    openai_api_key: str | None = Form(None),
) -> JSONResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in EXTRACTORS:
        raise HTTPException(400, f"Unsupported file extension: {suffix!r} (supported: {sorted(EXTRACTORS)})")

    # The as-requested settings are the session's identity (see
    # _find_existing_session_id) and what gets persisted to result.json —
    # `vlm_enrichment`/`synthetic_groups` themselves get downgraded to False
    # below when there's no usable API key, but that's about what actually
    # ran, not about which session this upload belongs to/should overwrite.
    requested_vlm_enrichment, requested_synthetic_groups = vlm_enrichment, synthetic_groups

    # If a session already exists with the same original filename *and* the
    # same enrichment settings, don't create a new one — overwrite that
    # session (keeping the same session_id) — instead of piling up duplicate
    # titles in the sidebar list, running it again with the same settings
    # updates the existing entry with the latest result and moves it to the
    # top. A different combination of vlm_enrichment/synthetic_groups is
    # kept as its own session instead (the base graph and a VLM-enriched one
    # are different results worth comparing, not overwriting). Deletes the
    # old captures/original file wholesale and recreates them so the
    # previous run's capture PNGs don't stick around as orphaned files.
    existing_session_id = _find_existing_session_id(
        file.filename or "", requested_vlm_enrichment, requested_synthetic_groups
    )
    session_id = existing_session_id or uuid.uuid4().hex[:12]
    session_dir = SESSIONS_DIR / session_id
    if existing_session_id:
        shutil.rmtree(session_dir, ignore_errors=True)
    session_dir.mkdir(parents=True)

    original_path = session_dir / _safe_original_filename(file.filename, suffix)
    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(413, f"File too large ({len(data) / 1e6:.1f}MB > {MAX_UPLOAD_BYTES / 1e6:.0f}MB limit)")
    original_path.write_bytes(data)

    t0 = time.time()
    try:
        doc = EXTRACTORS[suffix](original_path, capture_dir=session_dir / "captures")
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(500, f"Extraction failed: {exc}") from exc

    warnings: list[str] = []

    if suffix == ".pdf":
        # detects figures drawn as vector graphics (plots/heatmaps etc.,
        # not raster-embedded) — needs no VLM/API key, pure geometric
        # detection, so it's always on in the demo (see README "PDF Vector
        # Figure Detection"). Why there's no separate opt-in checkbox
        # unlike enrich_tables: it's free and has no model confirmation
        # step, so it's safe to always show.
        try:
            from articling.extractors.pdf_figures import enrich_pdf_figures

            enrich_pdf_figures(doc, capture_dir=session_dir / "captures")
        except Exception as exc:  # noqa: BLE001 — a deterministic step, but a failure doesn't block the whole thing
            traceback.print_exc()
            warnings.append(f"PDF vector figure detection failed (returning raster images only): {exc!r}")
    # Prefer a key the client entered directly in the browser — so the
    # server's .env config is left untouched and this value is used only
    # for that one request and discarded (once the request is handled,
    # this value never survives anywhere — never written to the session
    # directory/response JSON/log). Falls back to the server process's
    # OPENAI_API_KEY (.env) as before if there's none.
    resolved_key = (openai_api_key or "").strip() or os.environ.get("OPENAI_API_KEY")
    if vlm_enrichment and not resolved_key:
        warnings.append("Skipping VLM enrichment — no OPENAI_API_KEY. Enter a key in the field above or set it in the server's .env.")
        vlm_enrichment = False
    if synthetic_groups and not resolved_key:
        warnings.append("Skipping Synthetic Group proposal — no OPENAI_API_KEY. Enter a key in the field above or set it in the server's .env.")
        synthetic_groups = False

    client = None
    if vlm_enrichment or synthetic_groups:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=resolved_key)  # use the browser-entered key or the server's .env key (resolved_key) as-is
            # Cheaply check upfront whether the key itself is wrong —
            # merge_fragmented_text/propose_edges/propose_synthetic_groups
            # all follow the partial-failure
            # principle of "if one anchor/cluster/batch fails, skip just
            # that one and keep going" (intentional by the library's design
            # — meant for a normal case where only some calls fail, like a
            # rate limit), so if the key itself is wrong and every single
            # call fails, no exception propagates up and it just silently
            # ends with "nothing was produced" (a real bug confirmed
            # empirically — a user entering a typo'd key just gets the
            # deterministic graph back with no warning at all, which is
            # confusing). `models.list()` is a lightweight, free call, so
            # this one check is the sole exception placed up front.
            client.models.list()
        except Exception as exc:  # noqa: BLE001 — e.g. key auth failure
            traceback.print_exc()
            warnings.append(f"Failed to initialize the OpenAI client (the deterministic graph is still returned normally): {exc!r}")
            vlm_enrichment = False
            synthetic_groups = False

    if vlm_enrichment:
        try:
            from articling.relations.propose import apply_vlm_enrichment

            trace_root = os.environ.get("ARTICLING_TRACE_DIR")
            apply_vlm_enrichment(doc, client=client, trace_dir=Path(trace_root) / session_id if trace_root else None)
        except Exception as exc:  # noqa: BLE001 — a genuinely exceptional case (partial failure is already handled inside the function)
            # propose_edges already skips a single failed anchor internally,
            # so an exception reaching this far is a genuinely exceptional
            # case like client creation failing — leave the full traceback
            # in the server console for diagnosis.
            traceback.print_exc()
            warnings.append(f"VLM enrichment failed (the deterministic graph is still returned normally): {exc!r}")

    if synthetic_groups:
        try:
            from articling.relations.propose import propose_synthetic_groups

            # At the same level of opt-in as `propose_edges`'s
            # HEADING_PARENT role (creating new structure and reparenting),
            # so it isn't bundled into `apply_vlm_enrichment` (see README "Synthetic Group
            # Node") — the demo exposes that same trust-level distinction
            # as a separate checkbox. If `vlm_enrichment` was turned on
            # first, heading reparenting has already finished, giving a
            # narrower, more accurate sibling candidate (confirmed:
            # `docs/vlm-integration-research.md` §15).
            propose_synthetic_groups(doc, client=client)
        except Exception as exc:  # noqa: BLE001 — a genuinely exceptional case (partial failure is already handled inside the function)
            traceback.print_exc()
            warnings.append(f"Synthetic Group proposal failed (the rest of the graph is still returned normally): {exc!r}")
    elapsed = time.time() - t0

    result = {
        "session_id": session_id,
        "filename": file.filename,
        # As-requested, not the possibly-downgraded local variables — this
        # is what makes a base run and a VLM-enriched run of the same file
        # distinct sessions (see _find_existing_session_id) rather than
        # overwriting each other.
        "vlm_enrichment": requested_vlm_enrichment,
        "synthetic_groups": requested_synthetic_groups,
        "elapsed_sec": round(elapsed, 3),
        "summary": _summarize(doc),
        "nodes": _serialize_nodes(doc, session_id, session_dir),
        "edges": [e.model_dump(mode="json") for e in doc.edges],
        "warnings": warnings,
    }
    # Keeps the graph (JSON) this request produced saved as-is in the
    # session directory — until now it only went out as the HTTP response,
    # leaving only image/capture files on disk, with no way to see "the
    # result just produced" again without re-extracting (lost on reload).
    # This lets the same result be loaded again via GET /api/session/{id}
    # with no worry about the graph changing on re-extraction, when
    # visually verifying an engine artifact itself by opening the capture
    # PNG directly (e.g. checking whether an annotation composite actually
    # took effect).
    (session_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return JSONResponse(result)


@app.get("/api/session/{session_id}")
def get_session(session_id: str) -> JSONResponse:
    """Returns `result.json`, as saved by `extract_document`, as-is with no re-extraction.

    When the frontend is loaded with a `?session=<id>` URL, it calls this
    to skip the upload/extraction process entirely and show exactly that
    graph (and exactly those capture/image files) — since re-extracting even
    the same file can produce a different result once a non-deterministic
    step like VLM enrichment mixes in, it must be possible to pin and view
    "that saved result" while debugging."""
    result_path = SESSIONS_DIR / session_id / "result.json"
    if not result_path.is_file():
        raise HTTPException(404, f"Session not found: {session_id!r}")
    return JSONResponse(json.loads(result_path.read_text(encoding="utf-8")))


_SESSION_ID_RE = re.compile(r"^[0-9a-f]{6,40}$")  # the uuid4().hex[:12] format — a whitelist guarding against path manipulation (../ etc.)


@app.delete("/api/session/{session_id}")
def delete_session(session_id: str) -> JSONResponse:
    """Called by the sidebar's delete button — deletes the session
    directory (original file/captures/`result.json`) wholesale.
    Irreversible, so the frontend only calls this after a confirmation
    dialog (see the delete-button handler in `app.js`)."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, f"Invalid session_id: {session_id!r}")
    session_dir = SESSIONS_DIR / session_id
    if not session_dir.is_dir():
        raise HTTPException(404, f"Session not found: {session_id!r}")
    shutil.rmtree(session_dir)
    return JSONResponse({"deleted": session_id})


app.mount("/files", StaticFiles(directory=str(SESSIONS_DIR)), name="files")
app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8420)
