# 🕸️ Articling

> **Turn documents into structured graphs.**

[![Tests](https://github.com/san9min/articling/actions/workflows/tests.yml/badge.svg)](https://github.com/san9min/articling/actions/workflows/tests.yml)
[![License MIT](https://img.shields.io/badge/license-MIT-brightgreen)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![Pydantic v2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/pydantic/pydantic/main/docs/badge/v2.json)](https://pydantic.dev)

**Articling** is a lightweight Python SDK and CLI that turns
**DOCX, PPTX, XLSX, and PDF** into a unified, typed document graph.

## What is Articling?

Articling converts DOCX, PPTX, XLSX, and PDF files into a typed **document
graph** (`ArticDocument`) for GraphRAG and Neo4j workflows. It represents
document content and structure as nodes and edges, including caption and
reference relationships.

Core extraction uses **format-native Python libraries**, with no LibreOffice
or API access required. Optional VLM enrichment adds semantic relationships
and refines heading hierarchy.

![Articling pipeline: DOCX/PPTX/XLSX/PDF → articling (Extractor + VLM Enrichment) → ArticDocument graph](docs/assets/figure_pipeline.png)

*(The native PDF path does not create `Table`/vector `Image` nodes by
default — both are available as opt-in add-ons.)*

### Why VLM enrichment matters

Document layout conveys meaning: a caption belongs to a figure, and a
paragraph belongs under a heading. Optional VLM enrichment uses visual
layout to resolve relationships that native parsing alone cannot settle.

See [VLM setup and diagnostics](docs/vlm-enrichment.md) and
[format-specific behavior and limitations](docs/extraction-details.md).

## Quickstart

### 1. Install

```bash
pip install -e .
```

Requires Python 3.10+. Optional features are installed as extras:

```bash
pip install -e ".[relations]"          # LLM CAPTION_OF/REFERENCES proposals, VLM captions, PDF chunking, PDF table detection (OpenAI backend)
pip install -e ".[pdf-tables-local]"   # local-model backend for PDF table detection (torch/transformers/docling-core, heavy)
pip install -e ".[neo4j]"              # push_to_neo4j
pip install -e ".[dev]"                # pytest
```

### 2. Convert a document (CLI)

```bash
python -m articling.cli report.xlsx --format json -o report.json
python -m articling.cli report.xlsx --format cypher -o report.cypher
```

Add `--vlm-enrichment` to apply VLM enrichment (requires the `relations`
extra and `OPENAI_API_KEY`), and `--check` to validate the graph:

```bash
python -m articling.cli deck.pptx --vlm-enrichment --check -o graph.json
```

See [VLM setup and diagnostics](docs/vlm-enrichment.md) or the
[CLI reference](articling/.agents/skills/articling/references/cli.md) for more options.

### 3. Python usage

```python
from articling import extract
from articling.export.neo4j import to_cypher_script, push_to_neo4j

doc = extract("report.xlsx")
print(len(doc.nodes), len(doc.edges))

# Just the script, no connection
open("report.cypher", "w").write(to_cypher_script(doc))

# Or push straight into Neo4j (pip install "articling[neo4j]")
push_to_neo4j(doc, uri="bolt://localhost:7687", auth=("neo4j", "password"))
```

## Demo

There's a local demo app that shows the resulting graph interactively once
you upload a file:

```bash
pip install -r demo/requirements.txt
python demo/app.py
# -> http://127.0.0.1:8420
```

See [demo/README.md](demo/README.md) for usage and screenshots.

## Neo4j mapping

- `Node.type` → node label (`File`/`Artifact`/`Text`/`Table`/`Image`/`Group`)
- `Edge.type` → relationship type (`PARENT_OF`/`NEXT`/`CAPTION_OF`/`REFERENCES`)
- `Node.id` → `id` property, target of the uniqueness constraint (filename-based,
  so it stays unique even across merged documents)
- Nested properties (e.g. `Table.grid`, `capture_size`) are serialized as JSON
  strings, since Neo4j properties only allow scalars/arrays of scalars —
  `json.loads` them back on read.

## License

MIT — see [LICENSE](LICENSE).

### Native placement and semantic hierarchy

All four extractors use `articling.structure.finalize_structure` for native
placement preservation and deterministic caption/reference matching within each
Artifact. Content nodes retain `properties.native_location` with `artifact_id`
and, when known, zero-based `page_index`. This is source placement evidence;
`PARENT_OF` represents the interpreted hierarchy and can change independently.
The nested property is included in JSON and serialized by the existing Neo4j
property exporter. Older graphs without it still resolve scope through parents.

DOCX outline levels and matched PDF bookmarks feed the same hierarchy engine.
Its ancestor stack spans the entire document, including page breaks. Slides and
sheets remain separate scopes. Word headings own subsequent body blocks until
an equal or higher heading; PDF bookmarks establish only matched heading
relationships. Unmatched bookmarks block inheritance through missing ancestors.
DOCX page numbers are omitted when native extraction cannot establish them;
page geometry remains separate from logical parentage. Optional visual/LLM
rendering still uses format-specific adapters.
