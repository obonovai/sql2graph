# Contributing to sql2graph

**Setup, the local check suite, and the one code-generation step - in one place.**

This consolidates the developer workflow that is otherwise spread across the
[README](README.md), [`tests/README.md`](tests/README.md), and
[`docs/SYNTAX_VALIDATION.md`](docs/SYNTAX_VALIDATION.md). For *what* the code
does, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); for *how to extend*
it (a new target language or LLM provider), see
[`docs/EXTENDING.md`](docs/EXTENDING.md).

## Setup

The project uses [uv](https://github.com/astral-sh/uv) for dependency
management and [hatchling](https://hatch.pypa.io/) as the build backend.
Python 3.12+ is required.

```bash
uv sync                 # runtime + dev deps, package installed editable, from uv.lock
uv sync --extra eval    # additionally installs the evaluation extra (jupyter, pandas, ...)
```

`uv sync` creates `.venv/` from the pinned `uv.lock`. The `dev` dependency
group (mypy, pytest, ruff, type stubs) is included automatically.

## The check suite

Everything below must pass. There is **no CI yet**, so these are run locally.

```bash
uv run mypy src/            # strict type checking (mypy --strict)
uv run ruff check .         # linting
uv run ruff format .        # formatting (use --check in a pre-commit hook)
uv run pytest               # static tests: mocked, no LLM or DB calls (~100 tests)
```

Additional test tiers (opt-in; see [`tests/README.md`](tests/README.md) for
env vars and Docker recipes):

```bash
uv run pytest -m integration                    # real Anthropic / Neo4j / ArangoDB / Gremlin
uv run pytest -m 'integration or not integration'   # everything
```

Integration tests skip themselves when their credentials (or Docker) are
absent, so a partial setup still yields useful coverage.

### Conventions

- **`mypy --strict`** across all source. Pydantic validates every external
  input at the boundary, so downstream code may assume validity.
- **Ruff rules** `E F I PERF ARG W UP B` (see `pyproject.toml`). Line length 120.
- **No em/en dashes** in prose or docs - use a plain hyphen `-` or a colon.
- Match the surrounding code's naming, comment density, and idiom. The public
  API is re-exported from `src/sql2graph/__init__.py`; keep its `__all__` in
  sync when you add or remove a public symbol.

## Regenerating the ANTLR parsers

The grammar-based syntax validators use committed, machine-generated parsers, so
**routine development needs neither Java nor ANTLR** - only the pure-Python
`antlr4-python3-runtime`. Regenerate **only** when bumping a vendored grammar
(`src/sql2graph/validators/grammars/`) or the ANTLR version:

```bash
scripts/generate_parsers.sh
```

This needs a JDK and the ANTLR 4.13.x "complete" jar, and the tool version must
match the `antlr4-python3-runtime` pin in `pyproject.toml`. Regeneration is
deterministic (byte-stable), so re-running on an unchanged grammar produces no
diff. Commit the regenerated parsers. Full details, including grammar
provenance, are in [`docs/SYNTAX_VALIDATION.md`](docs/SYNTAX_VALIDATION.md) and
[`src/sql2graph/validators/grammars/README.md`](src/sql2graph/validators/grammars/README.md).

## Evaluation harness

The `eval/` subsystem (an optional extra) has its own workflow - see
[`eval/README.md`](eval/README.md) for running the matrix and
[`eval/METRICS.md`](eval/METRICS.md) for how the metrics are computed. Adding a
model or dataset there is an append, not an edit; the eval tests
(`tests/eval/`) enforce that new models carry pricing rates.
