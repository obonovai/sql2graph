# Tests

Two suites live here, kept separate so casual development never accidentally
hits a paid API or a database.

## Static tests (`test_static.py`)

Run by default:

```bash
uv run pytest
```

No network, no real LLMs, no databases: every external dependency is mocked
or replaced with an in-process double. Fast (~2 s) and free.

## Integration tests (`test_integration.py`)

Deselected by default (the `integration` pytest marker is excluded via
`pyproject.toml`'s `addopts`). Opt in explicitly:

```bash
uv run pytest -m integration
```

To run **everything** (both suites):

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

The `test_managed_*` tests need **no env vars and no pre-running database**: they
provision throwaway Neo4j / ArangoDB / Gremlin containers via `testcontainers`
and tear them down afterwards. They require a running **Docker daemon** and skip
themselves when one is not reachable. The first run pulls the database images
(`neo4j:5.26`, `arangodb:3.11`, `tinkerpop/gremlin-server:3.8.1`), so it is slow;
later runs reuse the cached images. `testcontainers` ships with the library, so
no extra install is needed.

```bash
# Any Docker daemon running; no env vars required:
uv run pytest -m integration -k managed
```

### What gets exercised

- `test_real_anthropic_translates_simple_select_to_cypher`: Anthropic
  round-trip + syntax validator; loop converges and produces valid Cypher.
- `test_real_anthropic_logs_token_usage`: the `"Anthropic call:"` log
  line fires with non-zero input/output token counts.
- `test_real_anthropic_async_translates_simple_select`: the async
  translator produces an equivalent-shaped result for the same input.
- `test_real_neo4j_server_validator_rejects_known_bad_query`: Neo4j
  `EXPLAIN` rejects a malformed query.
- `test_real_neo4j_server_validator_accepts_well_formed_query`:
  Neo4j `EXPLAIN` accepts a trivially valid one.
- `test_real_neo4j_async_server_validator_matches_sync`: async server
  validator returns the same shape of result as the sync sibling.
- `test_real_full_loop_anthropic_with_neo4j_server_validation`: full
  end-to-end against real Anthropic and real Neo4j.
- `test_managed_cypher_validator_accepts_and_rejects` /
  `test_managed_aql_validator_accepts_and_rejects` /
  `test_managed_gremlin_validator_accepts_and_rejects`: managed mode
  auto-provisions each engine, accepts a valid query and rejects a bad one.
- `test_managed_validator_via_factory` /
  `test_managed_async_validator_matches_sync`: `make_validator` /
  `make_async_validator` drive managed mode end-to-end (sync and async).

Approximate cost per full integration run: a few cents on Anthropic, no
cost on Neo4j (local Docker). Each test takes ~5-30 s depending on LLM
latency and how many fix iterations it triggers.
