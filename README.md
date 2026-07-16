# sql2graph

**LLM-driven SQL-to-graph-database query translator with a validation feedback loop.**

`sql2graph` is a Python framework that translates SQL queries into queries
for property-graph databases (Cypher for Neo4j, AQL for ArangoDB, and
Gremlin-Groovy for Apache TinkerPop / JanusGraph / Neptune / Cosmos DB
Gremlin API) by prompting a large language model with a user-provided
relational-to-graph schema mapping. Every generated query is automatically
validated; on failure the validator's errors are fed back to the LLM for
correction, up to a configurable number of iterations.

The framework exposes both a synchronous orchestrator (`SQLTranslator`) and an
asynchronous sibling (`AsyncSQLTranslator`) so callers can pick the model that
matches their environment: sync for scripts and evaluation notebooks; async
for UIs and concurrent multi-translation services. Both support an
optional typed event callback that surfaces every loop milestone in real time;
the async path additionally supports token-by-token streaming.

The library lives under `src/sql2graph/`: it exposes typed components
(schema mapping, LLM client, target language, validator, orchestrator)
connected through small structural Protocols. Both sync and async variants
of the LLM client, validator, and translator ship side by side. A mapping
builder (`build_mapping`) can also bootstrap the schema mapping itself from
SQL `CREATE TABLE` DDL, so it need not be written by hand; see
[`docs/mapping/builder.md`](docs/mapping/builder.md).

## Architecture at a glance

```
                      ┌─────────────────────────┐
                      │  SQLTranslator(         │
                      │    schema_mapping,      │
                      │    llm, target,         │
                      │    validator)           │
                      │  .translate(sql_query)  │
                      └────────────┬────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────┐
                      │  detect_features(sql)   │  ◄─── sqlglot AST
                      │  → {SqlFeature, ...}    │       (parser fail-open)
                      └────────────┬────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────┐
                      │  build_system_prompt    │  ◄─── SchemaMapping
                      │  build_generate_prompt  │       (nodes + edges)
                      │  (rules gated by        │       + detected features
                      │   detected features)    │
                      └────────────┬────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────┐
                      │     LLMClient.chat()    │  ◄─── Ollama or
                      │   (pluggable backend)   │       Anthropic (direct API)
                      └────────────┬────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────┐
                      │  target.extract_query() │
                      └────────────┬────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────┐
              ┌──────►│   validator.validate()  │  ◄─── syntax | server | none
              │       └────────────┬────────────┘
              │                    │
              │        errors?  ───┴─── no errors
              │           │                │
              │           ▼                ▼
              │    ┌──────────────┐   ┌────────┐
              │    │ build_fix_   │   │ return │
              │    │ prompt()     │   │ result │
              │    └──────┬───────┘   └────────┘
              │           │
              │           ▼
              │   ┌──────────────┐
              └───┤  LLM.chat()  │  (messages accumulate: full history)
                  └──────────────┘
```

`AsyncSQLTranslator` mirrors this flow step-for-step (same prompts, same
state, same iteration semantics) with `await` at the LLM and validator
call sites. Both translators accept an optional `on_event` callback that
fires at every milestone (`ParseFailedEvent`, `UnmappedTablesEvent`,
`UnmappedColumnsEvent`, `GeneratedEvent`, `ValidatedEvent`, `FixGeneratedEvent`,
`StalledEvent`, `MaxIterationsReachedEvent`, `CompletedEvent`); the async path
also accepts `stream_to` for token-by-token output.

When a fix iteration makes no progress (the model repeats its previous
candidate, or the validator returns the same error signature twice running)
the loop escalates once, re-asking from a fresh context at a higher
`escalation_temperature`. If the escalation still stalls, the translation
ends early with `status="stalled"` rather than burning the remaining
iterations. The full mechanism is in
[`docs/architecture.md`](docs/architecture.md).

See [`docs/architecture.md`](docs/architecture.md) for the deeper technical
reference, including a discussion of why the loop is implemented as plain Python
rather than a graph-orchestration framework, and [`docs/api.md`](docs/api.md) for
the library API. The full doc index and suggested reading order are in
[`docs/README.md`](docs/README.md).

## Per-query prompt assembly

The system prompt is assembled *per query*, not once per translator: the SQL
is parsed for the operation clusters it contains (`JOIN`, `AGGREGATION`,
`LIKE`, ...), and only the target-language rule chunks matching those
features enter the prompt. The Anthropic backends additionally send the
assembled system block with `cache_control: ephemeral`, so iterations 2+ of a
multi-iteration translation read the schema and rules from the prompt cache
instead of re-billing them; the per-call counts accumulate on
`TranslationResult.token_usage`. The full treatment is in
[`docs/architecture.md`](docs/architecture.md#per-query-prompt-assembly) and
[Anthropic prompt caching](docs/architecture.md#anthropic-prompt-caching).

## Install

```bash
uv sync
```

This creates `.venv/` from the pinned `uv.lock` and installs the package in
editable mode. The project uses [uv](https://github.com/astral-sh/uv) for
dependency management and [hatchling](https://hatch.pypa.io/) as the build
backend. Python 3.12+ is required.

## Quick start

A translation needs four things: a schema mapping, an LLM client, a target
language, and a validator. Build them from the shipped YAML configs and hand
them to `SQLTranslator`:

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

Expected output (the generated Cypher):

```cypher
MATCH (s:Supplier {suppkey: 1337})
RETURN s.name, s.address
```

To run against Claude via the direct Anthropic API instead, set
`ANTHROPIC_API_KEY` and use `config/models/anthropic.yaml` (`provider:
anthropic`) as the model config. See [Configuration](#configuration) for the
YAML files, and [As a library](#as-a-library) below for server-side
validation, the async variant, event callbacks, and token streaming.

For the guided version of this first run (reading the mapping, picking a
backend and a validation mode, interpreting the result), follow
[`docs/getting-started.md`](docs/getting-started.md).

## Configuration

Two kinds of files drive a run, kept in separate top-level directories.
**Inputs** live under `examples/` (the schema mapping, plus the DDL and example
SQL it derives from): they describe *what* is translated. **Configuration**
lives under `config/` (the LLM and graph-database settings): it describes *how*
the translation runs.

| Directory | What it is | When you need it |
|---|---|---|
| `examples/mappings/` | Schema mapping (nodes + edges); a translation *input*. | Always: one per relational schema. |
| `config/models/`   | LLM provider config (`provider: ollama` or `anthropic`). | Always: one per backend. |
| `config/servers/`  | Graph DB connection (`type: neo4j`, `arangodb`, or `gremlin`). | Only for `server` validation against *your own* database (`make_validator(target, "server", server_config=...)`); omit the server config to auto-provision a throwaway one via managed mode (needs Docker). |

These are orthogonal: the same mapping can be paired with any model and
validated against any server, and the same model drives any mapping. Two
mappings ship under `examples/`; two models and three servers ship under
`config/`. Copy and adapt as needed.

The field references: [`docs/mapping/format.md`](docs/mapping/format.md) for
the mapping file, [`docs/configuration.md`](docs/configuration.md) for the
model and server configs (including the environment-variable table), plus the
directory guides [`examples/README.md`](examples/README.md) and
[`config/README.md`](config/README.md).

## As a library

The [Quick start](#quick-start) above is the sync library usage; a fuller
worked example (constructor knobs, error handling) is the
[end-to-end example](docs/api.md#end-to-end-example-library) in the API
reference.

### Async variant

For UI integration, concurrent translations, or token streaming, use
`AsyncSQLTranslator`. The same configs construct the async stack via
`make_async_llm` and `make_async_validator`; the loop, prompts, events,
and result type are identical to the sync path.

```python
import asyncio
from sql2graph import (
    AsyncSQLTranslator, SchemaMapping, TranslationEvent,
    load_model_config, make_async_llm, make_async_validator, make_target,
)


def on_event(event: TranslationEvent) -> None:
    print(f"  → {type(event).__name__}")


async def main() -> None:
    mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")
    llm = make_async_llm(load_model_config("config/models/anthropic.yaml"))
    target = make_target("cypher")
    validator = make_async_validator("cypher", "syntax")

    async with AsyncSQLTranslator(mapping, llm, target, validator) as translator:
        result = await translator.translate(
            "SELECT name FROM supplier WHERE suppkey = 1337",
            on_event=on_event,
            stream_to=lambda chunk: print(chunk, end="", flush=True),
        )
    print(f"\n→ {result.status} in {result.duration_seconds:.2f}s, {result.token_usage.total_tokens} tokens")


asyncio.run(main())
```

## Development

```bash
uv run mypy src/                             # Strict type checking
uv run ruff check .                          # Linting
uv run ruff format .                         # Formatting
uv run pytest                                # Static tests (mocked, no LLM or DB calls)
uv run pytest -m integration                 # Integration tests (real Anthropic + Neo4j)
uv run python scripts/check_doc_refs.py      # Validate doc citations and links
```

The static suite (400+ tests) is what runs by default; the `integration`
marker is excluded via `pyproject.toml`. Integration tests gracefully skip
when their credentials are absent. See `tests/README.md` for the env-var
reference and a `docker run` recipe for Neo4j.

The project enforces `mypy --strict` across all source files and the same
ruff lint rules as the original Poetry-based ancestor (`E F I PERF ARG W UP B`).

## Documentation

| Document | What's in it |
|---|---|
| [`docs/getting-started.md`](docs/getting-started.md) | The nine-step first-run tutorial: install, read the shipped mapping, configure a backend, translate, interpret the result. |
| [`docs/architecture.md`](docs/architecture.md) | Design rationale: the plain-Python loop, `Protocol` extension points, the pre-flight gate, per-query prompt assembly, the async path. |
| [`docs/api.md`](docs/api.md) | The public Python API surface: every exported symbol with signatures. |
| [`docs/mapping/authoring.md`](docs/mapping/authoring.md) | Designing and hand-writing a schema mapping, with worked TPC-H and LDBC examples. |
| [`docs/mapping/format.md`](docs/mapping/format.md) | The mapping YAML field reference: typed properties, composite keys, list properties. |
| [`docs/mapping/builder.md`](docs/mapping/builder.md) | Generating a first-draft schema mapping from SQL DDL (extract, project, LLM-refine). |
| [`docs/validation/modes.md`](docs/validation/modes.md) | Choosing a validation mode: what `none`, `syntax`, `server`, and managed check, need, and miss. |
| [`docs/validation/syntax.md`](docs/validation/syntax.md) | The deployment-free, grammar-based (ANTLR) syntax validators and how to regenerate the parsers. |
| [`docs/configuration.md`](docs/configuration.md) | The model and server config YAML schemas, `${VAR}` interpolation, and the environment-variable table. |
| [`docs/extending.md`](docs/extending.md) | How to add a new target language or LLM provider. |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common failures and the canonical `TranslationResult.status` interpretation table. |
| [`eval/README.md`](eval/README.md) · [`eval/METRICS.md`](eval/METRICS.md) | The evaluation harness (how to run) and the metric definitions (how F1, tree-edit distance, execution accuracy, etc. are computed). |

Directory READMEs: [`config/`](config/README.md), [`examples/`](examples/README.md),
[`tests/`](tests/README.md). Full map and suggested reading order:
[`docs/README.md`](docs/README.md).

## Acknowledgments

The generate-validate-fix loop pattern was inspired by prior work on LLM
code generation paired with deterministic-validator feedback loops, where
a generated artifact is checked by a compiler-like tool and validator
errors are fed back as additional context for retry. `sql2graph` adapts
that core pattern with a plain-Python loop (no graph-orchestration
framework) and targets SQL-to-graph query translation.

## License

Released under the [MIT License](LICENSE) © 2026 Ivona Oboňová.
