# Troubleshooting

**Common failures and how to resolve them, grouped by where they surface.**

Each entry pairs a symptom with its cause and the shortest fix, ordered by
the layer where the symptom appears: LLM backend, validation, configuration,
translation outcome, evaluation.

## Scope

This page owns: symptom-first fixes for runtime failures, and the canonical
`TranslationResult.status` interpretation table. Related topics live with
their owners:

- [getting-started.md](getting-started.md): the guided first run these fixes
  assume.
- [validation/modes.md](validation/modes.md): what each validation mode
  checks and needs.
- [configuration.md](configuration.md): the config YAML schemas and the
  environment-variable table.
- [api.md](api.md): the result and error types these fixes reference.
- [eval/README.md](../eval/README.md): the evaluation-harness setup behind
  the eval symptoms.

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
  schema in [mapping/format.md](mapping/format.md).

## Interpreting `TranslationResult.status`

`TranslationResult.status` tells you why a translation ended. This is the
canonical table; [api.md](api.md#translationresult) documents the surrounding
result fields. (`"pending"` is an internal sentinel and never appears on a
returned result.)

| `status` | What happened | Fields to inspect | What to do |
|---|---|---|---|
| `"success"` | Validation passed on some iteration. | `generated_query`, `iterations_used`, `token_usage` | Nothing: use the query. |
| `"max_iterations_reached"` | The loop hit `max_iterations` without producing a valid query. | `generated_query` (the last attempt), `validation_errors` (from the final iteration) | Raise `max_iterations`, switch to a stronger model, or fix the reported errors by hand. |
| `"stalled"` | The loop detected no progress (the model repeated a candidate, or drew the same validator error twice), escalated once with a fresh-context, higher-temperature retry, and still could not advance; common with small local models. | `generated_query`, `validation_errors` | Raise `escalation_temperature`, increase `max_iterations`, or switch to a stronger model. |
| `"parse_error"` | The pre-flight gate could not parse the SQL and the translator was constructed with `parse_error_action=PreflightAction.REJECT`. The default is `WARN`, which translates anyway (sqlglot can false-fail on valid-but-exotic SQL), so this status only appears when you opted in. | `validation_errors` | Check the SQL, or keep the default `WARN` action. |
| `"unmapped_tables"` | The pre-flight gate rejected the SQL before any LLM call: it reads a table absent from the mapping. `generated_query` is `None` and `token_usage` is zero. | `unmapped_tables` | Add the table to the mapping, or construct the translator with `unmapped_tables_action=PreflightAction.WARN` (or `IGNORE`) if the omission is intentional. |
| `"unmapped_columns"` | The pre-flight gate rejected the SQL before any LLM call: it names a column that a mapped table omits. | `unmapped_columns` (as `"table.column"` refs) | Add the column to the node's `properties`, or lower `unmapped_columns_action`. |

The two `unmapped_*` statuses and `parse_error` come from the input-side
pre-flight gate that runs before any LLM call; see
[Pre-flight gate](architecture.md#pre-flight-gate-input-side).

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
