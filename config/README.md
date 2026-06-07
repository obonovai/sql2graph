# `config/` — runtime inputs for the demo CLI

This directory holds the YAML files that the demo CLI takes as parameters.
Three orthogonal categories live here, one per subdirectory:

| Subdirectory | Purpose | Pydantic model | Loader |
|---|---|---|---|
| `mappings/` | Relational-to-graph schema mapping (nodes + edges). | `rows2graph.SchemaMapping` | `SchemaMapping.from_yaml(path)` |
| `models/`   | LLM provider configuration. Discriminator: `provider`. | `OllamaConfig` \| `AnthropicConfig` | `rows2graph.load_model_config(path)` |
| `servers/`  | Graph database connection settings (only needed for `--validation server`). Discriminator: `type`. | `Neo4jConfig` \| `ArangoDBConfig` | `rows2graph.load_server_config(path)` |

These categories are intentionally *orthogonal*. The same `tpch.yaml`
mapping can be paired with any model and any server; the same
`anthropic.yaml` model config drives any mapping; the same `neo4j.yaml`
server is used by any Cypher run regardless of mapping or model.

The demo CLI selects one of each (mapping always; model always; server
only when validating against a live database):

```bash
uv run python demo/cli.py \
    --sql "SELECT name FROM supplier WHERE suppkey = 1337" \
    --mapping config/mappings/tpch.yaml \
    --model   config/models/anthropic.yaml \
    --target  cypher \
    --validation syntax
```

## Secrets

Both `models/` and `servers/` YAML files support environment-variable
interpolation. A string of the form `${VAR}` is replaced by
`os.environ["VAR"]` at config-load time; if `VAR` is unset, the loader
raises `KeyError` with a precise message. Mapping YAMLs do **not** support
interpolation — they hold no secrets and are deployment-invariant.

Recommended pattern:

```bash
export NEO4J_PASSWORD=...
export ARANGO_PASSWORD=...
export GCP_PROJECT_ID=...    # if your anthropic.yaml references it
uv run python demo/cli.py ...
```
