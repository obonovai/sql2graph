# rows2graph

**LLM-driven SQL-to-graph-database query translator with a validation feedback loop.**

`rows2graph` is a Python framework that translates SQL queries into queries
for property-graph databases (Cypher for Neo4j, AQL for ArangoDB, and
Gremlin-Groovy for Apache TinkerPop / JanusGraph / Neptune / Cosmos DB
Gremlin API) by prompting a large language model with a user-provided
relational-to-graph schema mapping. Every generated query is automatically
validated; on failure the validator's errors are fed back to the LLM for
correction, up to a configurable number of iterations.

The framework exposes both a synchronous orchestrator (`SQLTranslator`) and an
asynchronous sibling (`AsyncSQLTranslator`) so callers can pick the model that
matches their environment: sync for scripts, evaluation notebooks, and the
CLI; async for UIs and concurrent multi-translation services. Both support an
optional typed event callback that surfaces every loop milestone in real time;
the async path additionally supports token-by-token streaming.

The codebase is structured as a *framework + reference demo*:

* `src/rows2graph/`: a library exposing typed components (schema mapping,
  LLM client, target language, validator, orchestrator) connected through
  small structural Protocols. Both sync and async variants of the LLM
  client, validator, and translator ship side by side.
* `demo/cli.py`: a parametrized command-line client that exercises the
  library API. Use it directly, or copy it as a starting point for embedding
  the framework into a larger system.

## Architecture at a glance

```
                      ┌─────────────────────────┐
                      │  User: SQL query +      │
                      │        --mapping        │
                      │        --model          │
                      │        --target         │
                      │        --validation     │
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
fires at every milestone (`GeneratedEvent`, `ValidatedEvent`,
`FixGeneratedEvent`, `StalledEvent`, `MaxIterationsReachedEvent`,
`CompletedEvent`); the async path also accepts `stream_to` for
token-by-token output.

When a fix iteration makes no progress (the model repeats its previous
candidate, or the validator returns the same error signature twice running)
the loop escalates once: it re-asks from a *fresh* context (system prompt +
a single corrective turn, discarding the repetition-poisoned history) at a
higher `escalation_temperature`, and the target language can inject a
clause-specific `repair_hint` that overrides the default "don't restructure"
advice (AQL uses this for the `RETURN`-must-be-last ordering trap). If the
escalation still stalls, the translation ends early with
`status="stalled"` rather than burning the remaining iterations. This is
what stops small local models (notably `qwen3-coder`) from looping on
identical invalid output.

See `docs/ARCHITECTURE.md` for the deeper technical reference, including a
discussion of why the loop is implemented as plain Python rather than a
graph-orchestration framework, and `docs/API.md` for the library API and YAML
schema reference.

## Per-query prompt assembly

The system prompt is assembled *per query*, not once per translator. Before
the first LLM call, `detect_features` (in `src/rows2graph/sql_features.py`)
parses the SQL with sqlglot and returns a `frozenset[SqlFeature]` naming the
operation clusters present: `JOIN`, `AGGREGATION`, `LIKE`, `ORDER_LIMIT`,
`CTE`, `UNION`, `WINDOW`, `CASE`, `SUBQUERY`, `DISTINCT`, `TEMPORAL`. Both the generic
rules block and the target-language section (see
`src/rows2graph/targets/cypher.py`, `targets/aql.py`, `targets/gremlin.py`) emit only the rule
chunks corresponding to features actually in the query, so the LLM is not
distracted by, e.g., a 14-line `LIKE`/`ILIKE` mapping table on a query with
no string predicates. On any parser failure the function returns
`ALL_FEATURES`, which restores the pre-refactor "ship every rule" behaviour.
Unparseable input degrades prompt focus, never translation correctness.

The Anthropic backends (sync and async) send the assembled system block
with `cache_control: ephemeral` set, so iterations 2+ of any multi-iteration
translation read the schema + rules from Anthropic's prompt cache instead
of re-billing them as input tokens. See `src/rows2graph/llm/anthropic.py`.
The `Anthropic call:` log line reports `cache_read` and `cache_write`
counts alongside the regular input/output totals so cache hit rate is
observable per call. Those per-call counts are also accumulated across the
generate-validate-fix loop and returned on `TranslationResult.token_usage`
(a `TokenUsage` with `input_tokens`, `output_tokens`, Anthropic-only
`cache_read_tokens` / `cache_creation_tokens`, and a computed `total_tokens`),
so callers can report exactly how many tokens each translation cost. Ollama
populates `input_tokens` / `output_tokens` from `prompt_eval_count` /
`eval_count`; its cache fields stay 0.

## Install

```bash
uv sync
```

This creates `.venv/` from the pinned `uv.lock` and installs the package in
editable mode. The project uses [uv](https://github.com/astral-sh/uv) for
dependency management and [hatchling](https://hatch.pypa.io/) as the build
backend. Python 3.12+ is required.

## Quick start

The demo CLI takes two required YAML configs (a schema mapping and an LLM
model config) plus an optional server config for validation against a live
database. Under `--validation server` you either pass `--server` (your own
database) or omit it to auto-provision a throwaway one (see below).

```bash
uv run python demo/cli.py \
    --sql "SELECT name, address FROM supplier WHERE suppkey = 1337" \
    --mapping config/mappings/tpch.yaml \
    --model   config/models/ollama.yaml \
    --target  cypher \
    --validation syntax
```

Expected output (the generated Cypher on stdout, logs on stderr):

```cypher
MATCH (s:Supplier {suppkey: 1337})
RETURN s.name, s.address
```

For Claude via the direct Anthropic API, swap the model config:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."    # one-time, persist in ~/.zshrc
uv run python demo/cli.py \
    --sql "..." \
    --mapping config/mappings/tpch.yaml \
    --model   config/models/anthropic.yaml \
    --target  cypher
```

See `demo/README.md` for the full flag reference and more examples (LDBC SNB,
AQL/ArangoDB, server-side and zero-config managed validation, stdin input).

## Configuration

YAML configs live under `config/`, split by concern:

| Subdirectory | What it is | When you need it |
|---|---|---|
| `config/mappings/` | Schema mapping (nodes + edges). | Always: one per relational schema. |
| `config/models/`   | LLM provider config (`provider: ollama` or `anthropic`). | Always: one per backend. |
| `config/servers/`  | Graph DB connection (`type: neo4j`, `arangodb`, or `gremlin`). | Only for `--validation server` against *your own* database; omit `--server` to auto-provision a throwaway one (needs Docker). |

These categories are orthogonal: the same mapping can be paired with any
model, the same model drives any mapping, the same server config validates
any run against that database. Two mappings, two models, two servers ship in
`config/`. Copy and adapt as needed.

See `config/README.md` and `docs/API.md` for the YAML schemas.

## As a library

```python
from rows2graph import (
    SchemaMapping, SQLTranslator,
    load_model_config, make_llm,
    make_target, make_validator,
)

mapping = SchemaMapping.from_yaml("config/mappings/tpch.yaml")
llm = make_llm(load_model_config("config/models/anthropic.yaml"))
target = make_target("cypher")
validator = make_validator("cypher", "syntax")

with SQLTranslator(mapping, llm, target, validator) as translator:
    result = translator.translate("SELECT name FROM supplier WHERE suppkey = 1337")
    if result.validation_passed:
        print(result.generated_query)
    else:
        print(f"Failed after {result.iterations_used} iterations: {result.validation_errors}")
    print(f"Consumed {result.token_usage.total_tokens} tokens")
```

### Async variant

For UI integration, concurrent translations, or token streaming, use
`AsyncSQLTranslator`. The same configs construct the async stack via
`make_async_llm` and `make_async_validator`; the loop, prompts, events,
and result type are identical to the sync path.

```python
import asyncio
from rows2graph import (
    AsyncSQLTranslator, SchemaMapping, TranslationEvent,
    load_model_config, make_async_llm, make_async_validator, make_target,
)


def on_event(event: TranslationEvent) -> None:
    print(f"  → {type(event).__name__}")


async def main() -> None:
    mapping = SchemaMapping.from_yaml("config/mappings/tpch.yaml")
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
uv run mypy src/                  # Strict type checking
uv run ruff check .               # Linting
uv run ruff format .              # Formatting
uv run pytest                     # Static tests (mocked, no LLM or DB calls)
uv run pytest -m integration      # Integration tests (real Anthropic + Neo4j)
```

The static suite (~100 tests) is what runs by default; the `integration`
marker is excluded via `pyproject.toml`. Integration tests gracefully skip
when their credentials are absent. See `tests/README.md` for the env-var
reference and a `docker run` recipe for Neo4j.

The project enforces `mypy --strict` across all source files and the same
ruff lint rules as the original Poetry-based ancestor (`E F I PERF ARG W UP B`).

## Acknowledgments

The generate-validate-fix loop pattern was inspired by prior work on LLM
code generation paired with deterministic-validator feedback loops, where
a generated artifact is checked by a compiler-like tool and validator
errors are fed back as additional context for retry. `rows2graph` adapts
that core pattern with a plain-Python loop (no graph-orchestration
framework) and targets SQL-to-graph query translation.
