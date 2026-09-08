# Neo4j export reference

`pip install "articling[neo4j]"` for the live-push path; the Cypher-script
path has no extra dependency.

## Mapping

| ArticDocument | Neo4j |
|---|---|
| `Node.type` | node label (`File`/`Artifact`/`Text`/`Table`/`Image`) |
| `Edge.type` | relationship type (`PARENT_OF`/`NEXT`/`CAPTION_OF`/`REFERENCES`) |
| `Node.id` | `id` property, target of a uniqueness constraint (filename-derived, unique even across merged documents) |
| nested properties (e.g. `Table.grid`, `capture_size`) | JSON string — Neo4j properties must be scalars/scalar-arrays; `json.loads` to get them back |

## Usage

```python
from articling.export.neo4j import to_cypher_script, push_to_neo4j

# No connection needed — just a script you can review or run later
script = to_cypher_script(doc, with_constraints=True)
Path("report.cypher").write_text(script)

# Or push directly
push_to_neo4j(doc, uri="bolt://localhost:7687", auth=("neo4j", "password"),
               database=None)
```

`with_constraints=True` (default) emits uniqueness constraints on `id` per
label before the `CREATE` statements — safe to run once per database.

Equivalent from the CLI: `python -m articling.cli report.xlsx --format cypher -o report.cypher`.
