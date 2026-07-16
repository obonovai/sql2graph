# Configuration

**The deployment-side YAML files (LLM model, graph server) and every
environment variable the library reads.**

Configuration answers "which LLM, which database, which credentials"; it is
deliberately orthogonal to the translation inputs (mapping, SQL). The
authoritative statement of that split is the top-level
[README](../README.md#configuration); this page is the field reference for the
two config schemas plus the canonical environment-variable table.

## Scope

This page owns: the model and server YAML schemas; `${VAR}` interpolation
semantics; the environment-variable reference. Related topics live with their
owners:

- [api.md](api.md): the Python config types (`ModelConfig`, `ServerConfig`
  discriminated unions) and the factories that consume them.
- [validation/modes.md](validation/modes.md): when a server config is needed
  at all, and what happens without one.
- [getting-started.md](getting-started.md): the tutorial that walks through
  configuring a backend for a first translation.
- [config/README.md](../config/README.md): the shipped config files
  themselves.

## `config/models/<name>.yaml`: LLM model config

Discriminator: `provider`. Loaded with `load_model_config`
(`src/sql2graph/llm/__init__.py::load_model_config`) and passed to `make_llm`
or `make_async_llm`.

### Ollama (`provider: "ollama"`)

```yaml
provider: "ollama"
model: "llama3.2"                    # must be pulled on the Ollama server
# host: ...                          # optional; SDK reads OLLAMA_HOST if omitted
temperature: 0.1
num_ctx: 8192                        # context window size in tokens
# repeat_penalty: 1.1                # optional; unset means Ollama's own default.
                                     # Values above 1.0 counter the degenerate
                                     # repetition the fix loop can hit.
max_retries: 3                       # exponential backoff on connection errors
                                     # and 5xx; 4xx propagates immediately.
```

### Anthropic (`provider: "anthropic"`)

```yaml
provider: "anthropic"
# api_key: "${ANTHROPIC_API_KEY}"     # optional; SDK reads env var if omitted
model: "claude-opus-4-8"
temperature: 0.1
max_output_tokens: 4096
max_retries: 3                       # forwarded to the Anthropic SDK's
                                     # built-in retry layer (exp. backoff +
                                     # jitter on 408/409/429/5xx).
# thinking: "off"                    # "off" (default) or "adaptive" (extended
                                     # thinking; the model decides when to think)
# effort: ...                        # optional low|medium|high|xhigh|max, sent as
                                     # output_config.effort; unset means the API
                                     # default (high)
```

Authentication is via the `ANTHROPIC_API_KEY` environment variable. The
upstream SDK reads it automatically when `api_key` is omitted; that is the
recommended posture so the YAML file remains safe to commit. You may also
set `api_key` explicitly (with `${ENV_VAR}` interpolation, e.g.
`api_key: "${ANTHROPIC_API_KEY}"`). Set budget caps and usage alerts in
the [Anthropic console](https://console.anthropic.com).

## `config/servers/<name>.yaml`: graph DB connection

Discriminator: `type`. Loaded with `load_server_config`
(`src/sql2graph/validators/__init__.py::load_server_config`) and passed to
`make_validator(target, "server", server_config=...)`. A server config is only
needed for server-side validation; see
[validation/modes.md](validation/modes.md).

### Neo4j (`type: "neo4j"`)

```yaml
type: "neo4j"
uri: "bolt://localhost:7687"
username: "neo4j"
password: "${NEO4J_PASSWORD}"
database: "neo4j"
# notifications_min_severity: ...    # optional "OFF" | "INFORMATION" | "WARNING";
                                     # managed mode sets "OFF" so the empty
                                     # database's unknown-label notifications are
                                     # not reported as schema errors
```

The validator runs `EXPLAIN <query>`, which parses and plans without
executing, safe for any statement.

### ArangoDB (`type: "arangodb"`)

```yaml
type: "arangodb"
url: "http://localhost:8529"
username: "root"
password: "${ARANGO_PASSWORD}"
database: "ldbc"
# check_collections: true           # optional; cross-check referenced collections
                                    # against the database catalogue (managed mode
                                    # sets false: its throwaway database is empty)
```

The validator runs `db.aql.validate(query)`. Generated AQL uses bare
edge-collection traversals (`FOR v IN OUTBOUND <doc> <EdgeCollection>`),
so no named graph is referenced or configured.

### Gremlin (`type: "gremlin"`)

```yaml
type: "gremlin"
url: "ws://localhost:8182/gremlin"
traversal_source: "g"
# username: "..."                      # optional; required for IAM / SASL backends
# password: "${GREMLIN_PASSWORD}"
```

The validator submits each candidate script via the `gremlinpython`
`Client` and consumes the result; any parse / step-compatibility error
surfaces as an exception that is captured as a validation message.

Recommended local backend: Apache TinkerPop Gremlin Server with
TinkerGraph, runnable in one line:

```bash
docker run --rm -p 8182:8182 tinkerpop/gremlin-server
```

TinkerGraph is *schemaless*: this catches script-level parse errors and
unsupported steps but NOT label / property hallucinations. Point `url`
at JanusGraph with a registered schema for schema-aware validation
comparable to Neo4j's `EXPLAIN`. The same `type: "gremlin"`
discriminator also works for Amazon Neptune and Azure Cosmos DB Gremlin
API endpoints (set `url` and provide `username` / `password` per the
backend's auth scheme).

## `${VAR}` interpolation

Every string field in both config schemas supports `${ENV_VAR}` placeholders,
resolved at load time by `interpolate_env`
(`src/sql2graph/_env.py::interpolate_env`). Referencing a variable that is not
set raises `KeyError` with the variable named in the message, so a missing
secret fails fast at config load rather than mid-translation. Values are
substituted after YAML parsing; nothing is written back to the file, which
keeps configs safe to commit. The mapping inputs under `examples/` hold no
secrets and support no interpolation: they are deployment-invariant.

## Environment variables

The canonical table of every variable the library (and its shipped configs)
reads:

| Variable | Read by | When it is needed |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic SDK (automatic) or `${...}` in a model config | Any translation with `provider: "anthropic"`. |
| `OLLAMA_HOST` | Ollama SDK (automatic) | Only when the Ollama server is not at the default `http://localhost:11434`. |
| `NEO4J_PASSWORD` | `${...}` in `config/servers/neo4j.yaml` | Server-mode validation against Neo4j. |
| `ARANGO_PASSWORD` | `${...}` in `config/servers/arangodb.yaml` | Server-mode validation against ArangoDB. |
| `GREMLIN_PASSWORD` | `${...}` in a Gremlin server config | Only for authenticated Gremlin endpoints (IAM / SASL); the shipped config leaves it commented out. |

Test-only variables (integration-test endpoints and credentials) are
documented in [tests/README.md](../tests/README.md).
