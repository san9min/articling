# Examples

One real, publicly available document per supported format, small enough to
commit and safe to redistribute — for anyone trying Articling without their
own DOCX/PPTX/XLSX/PDF on hand. These are separate from
[`test_dataset_private/`](../test_dataset_private) and [`runs/`](../runs),
which hold real company documents and stay gitignored.

Each file is picked for two things: it's a genuinely well-known document in
its own community (not just a blank template), and it's born-digital rather
than a bad scan, so extraction actually reflects Articling's output quality
rather than upstream OCR noise. Node/edge counts below are from
`--check`, run from the repo root — regenerate them yourself to confirm.

## `docx/apa-professional-sample-paper.docx`

- **Source**: APA Style's official ["Professional sample paper" (without annotations)](https://apastyle.apa.org/style-grammar-guidelines/paper-format/sample-papers), the reference template used across academic writing.
- **License**: published by APA specifically for download and reuse as a formatting reference. Not a public-domain dedication — re-check APA's terms before any redistribution beyond this kind of open-source example use.
- **Why this file**: a real heading hierarchy (Title/Abstract/Introduction/Method/...), a Table, a Figure with a caption, and a References list in one document — exercises Text/Table/Image nodes together with `CAPTION_OF`-shaped structure.

```bash
python -m articling.cli examples/docx/apa-professional-sample-paper.docx --check
```

96 nodes (92 Text, 1 Table, 1 Image, File, Artifact) · 95 `PARENT_OF` edges.

## `pptx/census-center-of-population.pptx`

- **Source**: U.S. Census Bureau, ["Mean Center of Population Shift, 1790–2020"](https://www2.census.gov/geo/maps/DC2020/PopCenter/Center_of_Pop_Shift_1790-2020.pptx) — the map deck behind a Census factoid that gets cited every ten years.
- **License**: U.S. federal government work — public domain domestically (17 U.S.C. § 105).
- **Why this file**: a real 24-slide government deck with 48 embedded map images spread across slides — exercises PPTX Image extraction and slide-to-slide `NEXT` edges at a realistic scale, not a 2-slide toy deck.

```bash
python -m articling.cli examples/pptx/census-center-of-population.pptx --check
```

472 nodes (399 Text, 48 Image, 24 Artifact/slide, File) · 782 edges (471 `PARENT_OF`, 288 `REFERENCES`, 23 `NEXT`).

## `xlsx/financial-sample.xlsx`

- **Source**: Microsoft's official ["Financial Sample" workbook](https://learn.microsoft.com/power-bi/create-reports/sample-financial-download) — the sample data behind most Power BI/Excel tutorials on the internet.
- **License**: Microsoft-provided sample data, freely redistributed for exactly this kind of demo/tutorial use (already mirrored in hundreds of public tutorial repos).
- **Why this file**: it uses a real formal Excel Table (`ws.tables`, the "blue-striped" Insert > Table kind) spanning 700 rows — a different code path from the border-detected tables in the test fixtures. Dogfooding this file is what caught a real bug: openpyxl's `TableList.items()` returns `(name, ref_string)` pairs, not `(name, Table)` like a plain dict — `_table_ranges` was calling `.ref` on the string and crashing on every real workbook with a formal Table. Fixed in [`xlsx.py`](../articling/extractors/xlsx.py) with a regression test (`test_xlsx_native_table_extract`).

```bash
python -m articling.cli examples/xlsx/financial-sample.xlsx --check
```

3 nodes (File, Artifact, 1 Table — `financials`, 701 rows × 16 cols) · 2 `PARENT_OF` edges.

## `pdf/bitcoin-whitepaper.pdf`

- **Source**: Satoshi Nakamoto, ["Bitcoin: A Peer-to-Peer Electronic Cash System"](https://bitcoin.org/bitcoin.pdf) — arguably one of the most widely mirrored PDFs on the internet.
- **License**: never copyrighted or restricted by the author; bitcoin.org itself has freely distributed the unmodified file for 15+ years. Not a formal public-domain dedication, so flag this if your organization needs one before redistributing further.
- **Why this file**: small (9 pages, 184 KB) and cleanly born-digital — a fast PDF smoke test — with a genuine References section, useful later for testing reference-style heuristics.

```bash
python -m articling.cli examples/pdf/bitcoin-whitepaper.pdf --check
```

171 nodes (169 Text, File, Artifact) · 170 `PARENT_OF` edges.

## Trying them interactively

Every file above also works with the local upload demo
([`demo/README.md`](../demo/README.md)) — it renders the graph visually and
lets you click a node to see its text/table grid/image:

```bash
python demo/app.py   # http://127.0.0.1:8420 — then drag in any file above
```

## Note on capture images

Table/Image extraction also writes PNG captures next to the source file
(`examples/<format>/captures/`, gitignored). Those aren't committed here:
the `capture_path`/`image_path` properties in the exported JSON are resolved
to an absolute path at generation time (`scaffold.file_node`/
`save_image_bytes`), so a JSON committed from one machine wouldn't point
anywhere real on another — regenerate locally with `-o graph.json` instead
of committing output.
