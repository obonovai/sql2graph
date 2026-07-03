# Tests

The suite is split into three areas, kept separate so casual development never
accidentally hits a paid API or a database.

```
tests/
  conftest.py              # repo_root / examples_dir / mappings_dir path fixtures
  unit/                    # static: no network, no real LLM, no database (runs by default)
    conftest.py            # schema builders + scripted fake-LLM factory fixtures
    _doubles.py            # in-process LLMClient / AsyncLLMClient doubles (used via fixtures)
    mapping/  config/  llm/  targets/  prompts/  validators/
    sql/  preflight/  translator/  mapping_builder/
  integration/             # real LLMs / databases (deselected by default)
    conftest.py            # anthropic_config / neo4j_config / docker_available / small_schema
  eval/                    # offline tests for the evaluation harness (runs by default)
    conftest.py            # puts eval/ on sys.path
```

The folder tree mirrors `src/rows2graph/`, so a test's home is predictable: a
change to `src/rows2graph/validators/` is exercised under `tests/unit/validators/`.

## Unit tests (`unit/`)

Run by default:

```bash
uv run pytest
```

No network, no real LLMs, no databases: every external dependency (LLM, Neo4j
driver, ArangoDB client) is mocked or swapped for an in-process double. Fast
(~5 s) and free.

### Fixtures

Shared setup lives in `conftest.py` files, not copy-pasted per module:

- **Paths** (`tests/conftest.py`): `repo_root`, `examples_dir`, `mappings_dir` -
  computed from the conftest's own location, so tests never do `__file__`
  arithmetic.
- **Schemas** (`tests/unit/conftest.py`): `person_forum_schema()` and
  `forum_no_title_schema()` are *factory* fixtures - call them to build a fresh
  `SchemaMapping`.
- **Fake LLMs** (`tests/unit/conftest.py` + `_doubles.py`): `scripted_llm([...])`
  and `scripted_async_llm([...])` return a queue-backed double for the translator
  loop; `oneshot_llm(reply)` / `oneshot_async_llm(reply)` (in
  `tests/unit/mapping_builder/conftest.py`) return a one-shot double for the
  mapping-builder refinement pass. Tests obtain them via the fixture, never by
  importing `_doubles` directly.
- **Spy** (`tests/unit/translator/conftest.py`): `spy_analyze_sql(module)`
  records the `dialect` forwarded into a translator's pre-flight parse.

## Integration tests (`integration/`)

Deselected by default (the `integration` pytest marker is excluded via
`pyproject.toml`'s `addopts`). Each file carries a module-level
`pytestmark = pytest.mark.integration`. Opt in explicitly:

```bash
uv run pytest -m integration
```

To run **everything** (all three areas):

```bash
uv run pytest -m 'integration or not integration'
```

Each test checks for its required credentials and skips itself when the
relevant env var is missing, so a partial setup (e.g. Anthropic key but
no Neo4j) still gets useful coverage on the slice it can run.

### Required env vars

| Variable          | Used by                          | Notes                                                                     |
| ----------------- | -------------------------------- | ------------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY` | every `*anthropic*` test         | The fixture uses Claude Haiku to keep cost minimal (~$0.01 per full run). |
| `NEO4J_PASSWORD`  | every `*neo4j*` test             | Required, no default. Triggers skip when unset.                           |
| `NEO4J_URI`       | every `*neo4j*` test (optional)  | Defaults to `bolt://localhost:7687`.                                      |
| `NEO4J_USERNAME`  | every `*neo4j*` test (optional)  | Defaults to `neo4j`.                                                      |
| `NEO4J_DATABASE`  | every `*neo4j*` test (optional)  | Defaults to `neo4j`.                                                      |

### Starting Neo4j locally

The repo root's `docker-compose` (under `../`) starts Neo4j for the UI; the
integration tests will reuse that instance if it's running. Otherwise the
simplest standalone:

```bash
docker run --rm -d \
  --name r2g-test-neo4j \
  -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/testpassword \
  neo4j:5
```

Then in the test shell:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export NEO4J_PASSWORD=testpassword
uv run pytest -m integration
```

### Managed-validation tests (Docker only)

`integration/test_managed.py` needs **no env vars and no pre-running database**:
it provisions throwaway Neo4j / ArangoDB / Gremlin containers via
`testcontainers` and tears them down afterwards. It requires a running **Docker
daemon** and skips itself when one is not reachable. The first run pulls the
database images (`neo4j:5.26`, `arangodb:3.11`, `tinkerpop/gremlin-server:3.8.1`),
so it is slow; later runs reuse the cached images. `testcontainers` ships with
the library, so no extra install is needed.

```bash
# Any Docker daemon running; no env vars required:
uv run pytest -m integration -k managed
```

### What gets exercised

- `test_anthropic.py`: real Anthropic round-trip + syntax validator (loop
  converges to valid Cypher), token-usage logging, and the async translator.
- `test_neo4j.py`: Neo4j `EXPLAIN` accepts/rejects, sync vs async server
  validator parity, and a full end-to-end loop against real Anthropic + Neo4j.
- `test_managed.py`: managed mode auto-provisions each engine and
  accepts/rejects; `make_validator` / `make_async_validator` drive managed mode
  end-to-end; the offline AQL grammar is cross-checked against ArangoDB's parser.

Approximate cost per full integration run: a few cents on Anthropic, no
cost on Neo4j (local Docker). Each test takes ~5-30 s depending on LLM
latency and how many fix iterations it triggers.

## Eval tests (`eval/`)

Offline unit tests for the cost/token accounting in `eval/harness`.
The `eval/conftest.py` puts `eval/` on `sys.path`, so these run under the
default `pytest` invocation without hitting any LLM.
