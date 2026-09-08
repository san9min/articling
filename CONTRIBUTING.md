## Contributing In General

Articling is a small Python SDK/CLI (DOCX/PPTX/XLSX/PDF -> `ArticDocument`
graph). Issues, bug reports, and PRs are welcome.

Before diving in, skim [AGENTS.md](AGENTS.md) — it documents the project
structure, the `ArticDocument` contract, and the conventions extractors and
export code are expected to follow. This file covers the mechanics of
getting a dev environment running and submitting a change; AGENTS.md covers
the *shape* the code should take.

Participation in this project is covered by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Developing

### Create an environment and install

Articling uses plain `pip` — there's no `uv`/`poetry` lockfile to manage.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,relations,neo4j]"
```

- `dev` — `pytest`, needed to run the test suite
- `relations` — `openai`, needed for LLM-proposed `CAPTION_OF`/`REFERENCES`
  edges, VLM captioning, and PDF table detection
- `neo4j` — `neo4j` driver, needed for `push_to_neo4j`

Skip extras you don't need; importing `articling` itself has no heavy
dependencies (see AGENTS.md "Heavy/optional dependencies").

### Smoke-test the CLI

```bash
python -m articling.cli path/to/report.xlsx --format json -o report.json
```

## Coding Style Guidelines

There is no linter/formatter wired up yet (no `ruff`, no pre-commit hooks).
Match the surrounding style instead:

- Type-hinted, `from __future__ import annotations`
- `pathlib.Path` over string paths
- Module docstrings that explain *why*, not just *what*
- Structured Pydantic models over loose dicts for anything that crosses a
  module boundary or gets serialized

See AGENTS.md "Code standards" for the specifics (extractor shape,
`scaffold.py` helpers, the `CAPTION_OF`/`REFERENCES` trust-tier split, etc.).

## Tests

```bash
pytest
```

When submitting a new feature or fix:

- Add a test that verifies a real graph invariant, format-specific edge
  case, or regression — not one that just restates well-established library
  behavior (see AGENTS.md).
- After touching an extractor, run `scaffold.check_invariants` (or
  `articling.cli --check`) — PARENT_OF fan-in, NEXT linearity, and dangling
  edges are the graph's structural contract and are easy to break silently.
- Run `pytest` before considering a change complete.
- If the change intentionally affects extraction output, regenerate the
  relevant sample under `runs/` and review the diff carefully — it doubles
  as real-document regression data, so an unreviewed regen can hide a bug.

## Documentation

- `README.md` is the user-facing entry point (install, CLI/SDK usage,
  format-specific quirks, known limitations). Update it when user-facing
  behavior changes.
- `docs/` holds assets referenced from the docs (e.g. `docs/assets/`) — not a
  generated site, just static files.
- If a change alters CLI flags, SDK signatures, or export shape, also update
  the packaged usage skill at
  [`articling/.agents/skills/articling/SKILL.md`](articling/.agents/skills/articling/SKILL.md)
  — it ships inside the wheel/sdist and is what agents *using* articling
  read, so it needs to stay in sync (see AGENTS.md "Skills").

## Secrets / API keys

The `relations` extra (LLM-proposed edges, VLM captioning, PDF table detection)
and any Neo4j connection need credentials. Never hardcode these.

Copy [`.env.example`](.env.example) to `.env` and fill in real values:

```bash
cp .env.example .env
```

- `OPENAI_API_KEY` — read automatically by the `openai` SDK when
  `propose_edges()`/`caption_content_nodes()`/etc. create their default
  `OpenAI()` client (i.e. when you don't pass your own `client=`). No code change needed,
  just having it in the environment is enough.
- `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` — **not** auto-read by
  `push_to_neo4j(document, uri, auth)`; it takes the URI and `(user,
  password)` as explicit arguments, so load these yourself (`os.environ` +
  `python-dotenv`'s `load_dotenv()`) and pass them in.

`.env` is already excluded via `.gitignore` — only `.env.example` (no real
values) is meant to be committed.
