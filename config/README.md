# `config/`: deployment configuration for rows2graph

This directory holds the operational configuration the `rows2graph` library
loads: which LLM to translate with, and which graph database to validate
against. These are *deployment* settings - they select and tune the machinery,
they do not describe the data being translated. The relational-to-graph
**inputs** (schema mappings, DDL, example SQL) are not configuration and live
under [`../examples/`](../examples/README.md).

Two categories live here, one per subdirectory:

| Subdirectory | Purpose | Pydantic model | Loader |
|---|---|---|---|
| `models/`  | LLM provider configuration. Discriminator: `provider`. | `OllamaConfig` \| `AnthropicConfig` | `rows2graph.load_model_config(path)` |
| `servers/` | Graph database connection settings (only needed for server-side validation). Discriminator: `type`. | `Neo4jConfig` \| `ArangoDBConfig` \| `GremlinConfig` | `rows2graph.load_server_config(path)` |

The two are orthogonal to each other and to the mapping input: the same
`anthropic.yaml` model config drives any mapping against any server, and the
same `neo4j.yaml` server validates any Cypher run regardless of mapping or
model. A translation always needs a model; it needs a server only when
validating against a live database.

```python
from rows2graph import (
    SchemaMapping, SQLTranslator, load_model_config,
    make_llm, make_target, make_validator,
)

# The mapping is a translation INPUT (see ../examples/), not configuration.
mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")

# The model config IS deployment configuration (it lives here).
llm = make_llm(load_model_config("config/models/anthropic.yaml"))
target = make_target("cypher")
validator = make_validator("cypher", "syntax")

with SQLTranslator(mapping, llm, target, validator) as translator:
    result = translator.translate("SELECT name FROM supplier WHERE suppkey = 1337")
    print(result.generated_query)
```

## Secrets

Both `models/` and `servers/` YAML files support environment-variable
interpolation. A string of the form `${VAR}` is replaced by
`os.environ["VAR"]` at config-load time; if `VAR` is unset, the loader raises
`KeyError` with a precise message. (The mapping inputs under `../examples/` hold
no secrets and support no interpolation: they are deployment-invariant.)

Set the referenced variables in your shell before loading the configs:

```bash
export ANTHROPIC_API_KEY=...   # models/anthropic.yaml (the SDK reads it from the env)
export OLLAMA_HOST=...         # models/ollama.yaml (the SDK can read it from the env)
export NEO4J_PASSWORD=...      # servers/neo4j.yaml
export ARANGO_PASSWORD=...     # servers/arangodb.yaml
# export GREMLIN_PASSWORD=...  # servers/gremlin.yaml (only if you enable auth there)
```

For the mappings, DDL, and example SQL queries that a translation consumes, see
[`../examples/`](../examples/README.md).
