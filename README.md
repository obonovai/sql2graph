# rows2graph

**LLM-driven SQL-to-graph-database query translator with a validation feedback loop.**

`rows2graph` is a Python framework that translates SQL queries into queries
for property-graph databases (Cypher for Neo4j, AQL for ArangoDB) by
prompting a large language model with a user-provided relational-to-graph
schema mapping. Every generated query is automatically validated; on failure
the validator's errors are fed back to the LLM for correction, up to a
configurable number of iterations.

The codebase is structured as a *framework + reference demo*:

* `src/rows2graph/` — a library exposing typed components (schema mapping,
  LLM client, target language, validator, orchestrator) connected through
  small structural Protocols.
* `demo/cli.py` — a parametrized command-line client that exercises the
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
                      │  build_system_prompt    │  ◄─── SchemaMapping
                      │  build_generate_prompt  │       (nodes + edges)
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

See `docs/ARCHITECTURE.md` for the deeper technical reference, including a
discussion of why the loop is implemented as plain Python rather than a
graph-orchestration framework, and `docs/API.md` for the library API and YAML
schema reference.

## Install

```bash
uv sync
```

This creates `.venv/` from the pinned `uv.lock` and installs the package in
editable mode. The project uses [uv](https://github.com/astral-sh/uv) for
dependency management and [hatchling](https://hatch.pypa.io/) as the build
backend. Python 3.12+ is required.

## Quick start

The demo CLI takes three YAML config files: a schema mapping, an LLM model
config, and (optionally) a server config for validation against a live
database.

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
AQL/ArangoDB, server-side validation, stdin input).

## Configuration

YAML configs live under `config/`, split by concern:

| Subdirectory | What it is | When you need it |
|---|---|---|
| `config/mappings/` | Schema mapping (nodes + edges). | Always — one per relational schema. |
| `config/models/`   | LLM provider config (`provider: ollama` or `anthropic`). | Always — one per backend. |
| `config/servers/`  | Graph DB connection (`type: neo4j` or `arangodb`). | Only when `--validation server`. |

These categories are orthogonal: the same mapping can be paired with any
model, the same model drives any mapping, the same server config validates
any run against that database. Two mappings, two models, two servers ship in
`config/` — copy and adapt as needed.

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
```

## Development

```bash
uv run mypy src/                 # Strict type checking
uv run ruff check .              # Linting
uv run ruff format .             # Formatting
uv run pytest                    # Tests (mocked — no LLM or DB calls)
```

The project enforces `mypy --strict` across all source files and the same
ruff lint rules as the original Poetry-based ancestor (`E F I PERF ARG W UP B`).

## Acknowledgments

The generate–validate–fix loop pattern was inspired by the
[yara-copilot](https://github.com/gendigitalinc/yara-copilot) project, which
implements the same pattern with LangGraph for YARA rule generation.
`rows2graph` adapts the core pattern with a plain-Python loop (no LangGraph
dependency) and targets SQL-to-graph query translation.
