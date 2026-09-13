"""articling CLI — extracts a single file into an ArticDocument and saves it
as JSON/Cypher.

Usage:
    python -m articling.cli report.xlsx
    python -m articling.cli report.xlsx --format cypher -o report.cypher
    python -m articling.cli report.xlsx --vlm-enrichment   # heading reparenting + CAPTION_OF/REFERENCES proposals (requires OPENAI_API_KEY)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import extract
from .export.json_export import to_json
from .export.neo4j import to_cypher_script
from .relations.propose import nest_numbered_headings
from .scaffold import check_invariants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DOCX/PPTX/XLSX/PDF -> ArticDocument")
    parser.add_argument("path", type=Path, help="input file (.docx/.pptx/.xlsx/.pdf)")
    parser.add_argument("--format", choices=["json", "cypher"], default="json")
    parser.add_argument("-o", "--output", type=Path, default=None, help="stdout if omitted")
    parser.add_argument(
        "--vlm-enrichment", action="store_true",
        help=(
            "run relations.propose.apply_vlm_enrichment — merge semantic Text groups from the 2D layout and same-line fragments, "
            "propose CAPTION_OF/REFERENCES and reparent heading Text as parents in the same call (propose_edges, "
            "include_text_anchors=True; a flat '1)/2)/3)...' enumerated-sibling run is asked about once and applied to "
            "every member, not asked once per member), add the CAPTION_OF/REFERENCES proposals to the edges, then re-nest "
            "numbered subheadings under their section (nest_numbered_headings, already run once on the base graph — see "
            "below — but re-run here to catch a section HEADING_PARENT just moved) (requires OPENAI_API_KEY)"
        ),
    )
    parser.add_argument("--check", action="store_true", help="check scaffold invariants (PARENT_OF/NEXT integrity) and report to stderr")
    parser.add_argument("--slide-images", type=Path, help="JSON object mapping zero-based PPTX slide indices to PNG/JPEG paths (relative to the JSON file); requires --vlm-enrichment")
    parser.add_argument("--model", help="override the VLM model used by --vlm-enrichment (default: relations._config.DEFAULT_MODEL)")
    parser.add_argument("--trace-dir", type=Path, help="save local VLM requests, image evidence, judgments and graph snapshots; requires --vlm-enrichment")
    args = parser.parse_args(argv)
    if args.slide_images and not args.vlm_enrichment:
        parser.error("--slide-images requires --vlm-enrichment")
    if args.model and not args.vlm_enrichment:
        parser.error("--model requires --vlm-enrichment")
    if args.trace_dir and not args.vlm_enrichment:
        parser.error("--trace-dir requires --vlm-enrichment")

    document = extract(args.path)
    # Needs no VLM/API key — pure text pattern matching ("3.1" under "3"),
    # see nest_numbered_headings's own docstring — so it belongs to the base
    # graph unconditionally, not gated behind --vlm-enrichment. Kept in the
    # --vlm-enrichment bundle too (see apply_vlm_enrichment): its own
    # HEADING_PARENT reparenting can move a numbered heading to a new
    # section, which this call alone (running before that) can't see yet.
    nest_numbered_headings(document)

    if args.vlm_enrichment:
        from .relations.propose import DEFAULT_MODEL, apply_vlm_enrichment

        slide_images = None
        if args.slide_images:
            try:
                mapping = json.loads(args.slide_images.read_text())
                if not isinstance(mapping, dict) or any(not k.isdecimal() or str(int(k)) != k or not isinstance(v, str) for k, v in mapping.items()):
                    raise ValueError("expected an object mapping canonical zero-based indices to paths")
                slide_images = {int(k): args.slide_images.parent / v for k, v in mapping.items()}
            except (OSError, ValueError) as exc:
                parser.error(f"Invalid --slide-images manifest: {exc}")
        apply_vlm_enrichment(document, model=args.model or DEFAULT_MODEL, slide_images=slide_images, trace_dir=args.trace_dir)

    if args.check:
        problems = check_invariants(document.nodes, document.edges)
        if problems:
            print(f"[check] {len(problems)} invariant violation(s):", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
        else:
            print("[check] no invariant violations", file=sys.stderr)

    output = to_cypher_script(document) if args.format == "cypher" else to_json(document)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(f"saved -> {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
