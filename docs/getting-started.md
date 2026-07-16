# Getting started

**From a clean checkout to a first validated graph query: install, read the
shipped TPC-H mapping, configure an LLM backend, and translate SQL to Cypher.**

This tutorial walks a new user through the shortest end-to-end path: nine
numbered steps, no database, no Docker. At the end you will have run the
generate-validate-fix loop against a real mapping, inspected a
`TranslationResult`, and watched the loop's events fire.

## Scope

This page owns: the numbered first-run tutorial; the minimal install and
backend setup; the first read of a `TranslationResult`. Related topics live
with their owners:

- [architecture.md](architecture.md): why the loop is built the way it is,
  including per-query prompt assembly.
- [mapping/authoring.md](mapping/authoring.md): designing and hand-writing a
  schema mapping for your own database.
- [validation/modes.md](validation/modes.md): choosing between the `none`,
  `syntax`, `server`, and managed validation modes.
- [configuration.md](configuration.md): the model and server YAML field
  reference and the environment-variable table.
- [api.md](api.md): the full public Python surface and result types.
- [troubleshooting.md](troubleshooting.md): common failures and the canonical
  `status` interpretation table.

## Prerequisites

- Python 3.12 or newer (the floor pinned in `pyproject.toml`).
- [uv](https://github.com/astral-sh/uv) for dependency management.
- ONE working LLM backend: either a local [Ollama](https://ollama.com)
  install, or an Anthropic API key. Step 4 covers both.
- Docker is NOT needed for this tutorial. It only becomes relevant later, for
  managed validation (auto-provisioning a throwaway graph database).

## 1. Install

```bash
uv sync
```

This creates `.venv/` from the pinned `uv.lock` and installs the package in
editable mode.

## 2. The two kinds of files a run needs

A run combines translation inputs with deployment configuration, kept in
separate top-level directories. The schema mapping and the SQL queries are
inputs and live under `examples/`: they describe what is translated. The LLM
model config (and, later, any graph-server config) is deployment and lives
under `config/`: it describes how the translation runs. The authoritative
statement of this split is the [README](../README.md#configuration); the field
references are [mapping/format.md](mapping/format.md) for the mapping file and
[configuration.md](configuration.md) for the config files.

## 3. Read the mapping you will use

This tutorial uses the shipped TPC-H mapping,
[`examples/mappings/tpch.yaml`](../examples/mappings/tpch.yaml). A mapping is
nodes plus edges, nothing else. Here is its `Supplier` node, verbatim:

```yaml
  - label: "Supplier"
    source_table: "supplier"
    properties:
      suppkey: "suppkey"
      name: "name"
      address: "address"
      phone: "phone"
      acctbal: "acctbal"
      comment: "comment"
    primary_key: "suppkey"
```

Each node maps one relational table (`source_table`) to one graph label
(`label`) and lists its properties as graph property name to SQL column pairs.
Edges turn foreign keys into relationships; this one connects `Supplier` to
`Nation` through the `nationkey` foreign key:

```yaml
  - type: "LOCATED_IN"
    source_node: "Supplier"
    target_node: "Nation"
    source_table: "supplier"
    source_foreign_key: "nationkey"
    target_primary_key: "nationkey"
```

The graph-facing names (`Supplier`, `LOCATED_IN`, the property names on the
left of each pair) are what the LLM sees and uses verbatim in the generated
query, so choose them as you want them to appear in the graph. The full field
reference is in [mapping/format.md](mapping/format.md).

## 4. Configure an LLM backend

Pick one of the two shipped model configs.

### Option A: Ollama

The shipped config
[`config/models/ollama.yaml`](../config/models/ollama.yaml) names the model
`qwen3-coder:30b`. Start the server and pull that exact model:

```bash
ollama serve                  # skip if Ollama already runs as a service
ollama pull qwen3-coder:30b
```

No environment variable is needed for the default local setup; set
`OLLAMA_HOST` only when the Ollama server is not at the default
`http://localhost:11434`.

### Option B: Anthropic

Export your API key and use
[`config/models/anthropic.yaml`](../config/models/anthropic.yaml) as the model
config in step 6:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

The shipped YAML omits `api_key` on purpose: the SDK reads the environment
variable automatically, so the file stays safe to commit.

Every field of both config schemas, plus the canonical environment-variable
table, is documented in [configuration.md](configuration.md).

## 5. Pick a validation mode

Every generated query is checked by a validator, and its errors drive the fix
loop. Start with `syntax`: it is the deployment-free mode (grammar-based,
in-process, milliseconds, no database or Docker), which is exactly right for a
first run. The other modes (`none`, `server` against your own database, and
the managed variant that auto-provisions a throwaway one) are compared in
[validation/modes.md](validation/modes.md).

## 6. Translate your first query

A translation needs four things: a schema mapping, an LLM client, a target
language, and a validator. Build them from the YAML files above and hand them
to `SQLTranslator`. Save this at the repository root (both paths are relative
to it) and run it with `uv run python`:

```python
from sql2graph import (
    SchemaMapping, SQLTranslator,
    load_model_config, make_llm, make_target, make_validator,
)

mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")
llm = make_llm(load_model_config("config/models/ollama.yaml"))
target = make_target("cypher")
validator = make_validator("cypher", "syntax")

with SQLTranslator(mapping, llm, target, validator) as translator:
    result = translator.translate(
        "SELECT name, address FROM supplier WHERE suppkey = 1337"
    )
    print(result.generated_query)
```

For Anthropic, swap the config path to `config/models/anthropic.yaml`; nothing
else changes. LLM output varies run to run, but for this query the output will
resemble:

```cypher
MATCH (s:Supplier {suppkey: 1337})
RETURN s.name, s.address
```

## 7. Read the `TranslationResult`

`translate()` returns a typed `TranslationResult`. Check these fields first:

- `generated_query`: the final query text (the last attempt, even on loop
  failure; `None` when a pre-flight check rejects the SQL before generation).
- `validation_passed`: whether the final iteration validated cleanly.
- `iterations_used`: how many validate calls the loop performed.
- `token_usage`: the tokens billed across the whole loop
  (`token_usage.total_tokens` sums input, output, and cache counts).
- `status`: why the translation ended.

`status` is one of `"success"`, `"max_iterations_reached"`, `"stalled"`,
`"parse_error"`, `"unmapped_tables"`, or `"unmapped_columns"`. The canonical
table (what each value means, which fields to inspect, what to do) is in
[troubleshooting.md](troubleshooting.md#interpreting-translationresultstatus).

## 8. Translate a file from examples/

The repository ships example queries under `examples/sql/tpch/`. Feed one to
the same translator instead of an inline string:

```python
from pathlib import Path

sql = Path("examples/sql/tpch/q06.sql").read_text()

with SQLTranslator(mapping, llm, target, validator) as translator:
    result = translator.translate(sql)
    print(result.status)
    print(result.generated_query)
```

[`q06.sql`](../examples/sql/tpch/q06.sql) joins `supplier` to `customer` on
their shared `nationkey`, aliases the output columns, and filters both
`comment` columns with `LIKE` patterns. It is a good second query because the
mapping has no direct Supplier-to-Customer edge: both nodes reach `Nation`
through `LOCATED_IN`, so a faithful translation has to route the join through
a shared `Nation` node. The features detected in the SQL (here `JOIN` and
`LIKE`) gate which target-language rule chunks enter the prompt, so the LLM
only sees the rules this query needs; see
[architecture.md](architecture.md#per-query-prompt-assembly).

## 9. Watch the loop work

Both translators accept an optional `on_event` callback on `translate()` that
fires at every loop milestone. The smallest useful handler prints the event
class names:

```python
from sql2graph import TranslationEvent

def on_event(event: TranslationEvent) -> None:
    print(type(event).__name__)

with SQLTranslator(mapping, llm, target, validator) as translator:
    result = translator.translate(sql, on_event=on_event)
```

For a run whose first candidate fails validation once, the output will
resemble:

```
GeneratedEvent
ValidatedEvent
FixGeneratedEvent
ValidatedEvent
CompletedEvent
```

`CompletedEvent` always fires last and carries the `TranslationResult`. The
full event union, iteration numbering, and handler semantics are in
[api.md](api.md#iteration-events).

## Next steps

- Map your own schema: [mapping/authoring.md](mapping/authoring.md) for
  hand-writing a mapping, [mapping/builder.md](mapping/builder.md) for
  generating a first draft from `CREATE TABLE` DDL.
- Validate against a real database:
  [validation/modes.md](validation/modes.md) covers server and managed
  validation.
- Go async, with token streaming and concurrent translations:
  [api.md](api.md).
- Measure translation quality with the evaluation harness:
  [eval/README.md](../eval/README.md).
