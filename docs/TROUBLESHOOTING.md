# Troubleshooting

**Common failures and how to resolve them, grouped by where they surface.**

For the API these reference, see [API.md](API.md); for validation modes, see
[SYNTAX_VALIDATION.md](SYNTAX_VALIDATION.md).

## LLM backends

### Ollama: connection refused / model not found

`OllamaLLMClient` retries connection errors and HTTP 5xx with exponential
backoff (1s, 2s, 4s, …), but a 4xx propagates immediately - an unknown model is
a 404 the client will not retry. Make sure the daemon is up and the model is
pulled:

```bash
ollama serve
ollama pull qwen3-coder:30b        # the exact model in your config/models/*.yaml
```

Set `OLLAMA_HOST` (or `host:` in the model YAML) for a non-default endpoint.

### Anthropic: authentication error

The SDK reads `ANTHROPIC_API_KEY` from the environment when the model YAML omits
`api_key` (the recommended posture, so the file stays safe to commit). Export it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Set budget caps and alerts in the [Anthropic console](https://console.anthropic.com).

## Validation

### Managed validation fails with `RuntimeError` about Docker

`make_validator(target, "server")` with **no** `server_config` resolves to
*managed* mode, which provisions a throwaway database via `testcontainers`. Its
`validate()` raises `RuntimeError` when no Docker daemon is reachable. Start
Docker, or pass an explicit `server_config` to validate against a database you
run yourself. The first managed run also pulls the database images, so it is
slow; later runs reuse the cache.

### Gremlin server validation misses bad labels/properties

Server validation against schemaless **TinkerGraph** catches script-level parse
errors and unsupported steps but **not** label / property hallucinations. Point
`url` at JanusGraph with a registered schema for schema-aware validation
comparable to Neo4j's `EXPLAIN`. (This is a property of the backend, not a bug.)

## Configuration

### `KeyError` about a `${VAR}` reference

Model and server YAML files interpolate `${VAR}` from the environment at load
time; an unset variable raises `KeyError` with the offending name. Export it
first:

```bash
export NEO4J_PASSWORD=...       # servers/neo4j.yaml
export ARANGO_PASSWORD=...      # servers/arangodb.yaml
```

### `ValidationError` loading a schema mapping

`SchemaMapping.from_yaml` validates strictly (`extra="forbid"`):

- *"references undefined source_node / target_node"* - an edge's `source_node`
  or `target_node` does not match any node `label`. Labels are case-sensitive.
- *"extra fields not permitted"* - a typo'd field name; compare against the
  schema in [API.md](API.md#yaml-schema-reference).

## Translation outcomes

`TranslationResult.status` tells you why a translation ended (full table in
[API.md](API.md#translationresult)):

### `status="unmapped_tables"` or `"unmapped_columns"`

The input-side pre-flight gate rejected the SQL before any LLM call because it
reads a table (or names a column of a mapped table) absent from the mapping. The
offending names are in `result.unmapped_tables` / `result.unmapped_columns`.
Fix the mapping, or - if the omission is intentional - construct the translator
with `unmapped_tables_action=PreflightAction.WARN` (or `IGNORE`) to translate
anyway. See [Pre-flight gate](ARCHITECTURE.md#pre-flight-gate-input-side).

### `status="parse_error"`

Only occurs when the translator was constructed with
`parse_error_action=PreflightAction.REJECT`; the default is `WARN`, which
translates anyway (sqlglot can false-fail on valid-but-exotic SQL). If you did
not set `reject`, you will not see this status.

### `status="stalled"`

The loop detected no progress (the model repeated a candidate, or drew the same
validator error twice), escalated once with a fresh-context, higher-temperature
retry, and still could not advance - common with small local models. Options:
raise `escalation_temperature`, increase `max_iterations`, or switch to a
stronger model.

## Evaluation

### AQL execution metrics error or return nothing

No ArangoDB setup step is required: the eval harness rewrites the gold and
candidate AQL's unified SCREAMING_SNAKE edge names (`KNOWS`, `HAS_CREATOR`,
`HAS_TAG`, ...) to graphonauts's split snake_case collections at query time
(`eval/harness/arango_edges.expand_unified_edges`, applied in `run_aql`), so the
database is never modified. If the traversals still return nothing, confirm LDBC
SF1 is actually loaded into ArangoDB database `graphonauts` (graphonauts's split
edge collections such as `knows`, `post_has_creator`, `forum_has_tag`).

See [`eval/README.md`](../eval/README.md) for the full execution-metrics setup
and [`eval/METRICS.md`](../eval/METRICS.md) for what each metric measures.
