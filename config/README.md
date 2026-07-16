# `config/`: deployment configuration for sql2graph

**How a translation run is wired: which LLM translates, and which graph
database validates the result.**

This directory holds *deployment* settings: they select and tune the
machinery, they do not describe the data being translated. The translation
inputs live under [`../examples/`](../examples/README.md); the authoritative
statement of that split is the project README's
[Configuration](../README.md#configuration) section, and the field-by-field
schema reference is [`docs/configuration.md`](../docs/configuration.md).

Two categories live here, one per subdirectory:

| Subdirectory | Purpose | Pydantic model | Loader |
|---|---|---|---|
| `models/`  | LLM provider configuration. Discriminator: `provider`. | `OllamaConfig` \| `AnthropicConfig` | `sql2graph.load_model_config(path)` |
| `servers/` | Graph database connection settings (only needed for server-side validation). Discriminator: `type`. | `Neo4jConfig` \| `ArangoDBConfig` \| `GremlinConfig` | `sql2graph.load_server_config(path)` |

A translation always needs a model; it needs a server only when validating
against a live database. For a worked first run that loads these files, see
[`docs/getting-started.md`](../docs/getting-started.md).

## Secrets

Both `models/` and `servers/` YAML files support environment-variable
interpolation: a string of the form `${VAR}` is replaced by
`os.environ["VAR"]` at config-load time, and an unset variable raises
`KeyError` with a precise message. The interpolation semantics and the
canonical variable table are in
[`docs/configuration.md`](../docs/configuration.md#environment-variables).

Set the referenced variables in your shell before loading the configs:

```bash
export ANTHROPIC_API_KEY=...   # models/anthropic.yaml (the SDK reads it from the env)
export OLLAMA_HOST=...         # models/ollama.yaml (the SDK can read it from the env)
export NEO4J_PASSWORD=...      # servers/neo4j.yaml
export ARANGO_PASSWORD=...     # servers/arangodb.yaml
# export GREMLIN_PASSWORD=...  # servers/gremlin.yaml (only if you enable auth there)
```
