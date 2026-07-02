# `config/`: example configuration for rows2graph

This directory holds the example YAML and DDL inputs the `rows2graph`
library loads. Four categories live here, one per subdirectory:

| Subdirectory | Purpose | Pydantic model | Loader |
|---|---|---|---|
| `mappings/` | Relational-to-graph schema mapping (nodes + edges). | `rows2graph.SchemaMapping` | `SchemaMapping.from_yaml(path)` |
| `models/`   | LLM provider configuration. Discriminator: `provider`. | `OllamaConfig` \| `AnthropicConfig` | `rows2graph.load_model_config(path)` |
| `servers/`  | Graph database connection settings (only needed for server-side validation). Discriminator: `type`. | `Neo4jConfig` \| `ArangoDBConfig` | `rows2graph.load_server_config(path)` |
| `ddl/`      | Raw `CREATE TABLE` schema; the source a mapping is generated from. | (parsed to `RelationalSchema`) | `build_mapping(ddl=...)` |

The first three categories are intentionally *orthogonal*. The same
`tpch.yaml` mapping can be paired with any model and any server; the same
`anthropic.yaml` model config drives any mapping; the same `neo4j.yaml`
server is used by any Cypher run regardless of mapping or model.

A translation loads one of each (mapping always; model always; server only
when validating against a live database):

```python
from rows2graph import (
    SchemaMapping, SQLTranslator, load_model_config,
    make_llm, make_target, make_validator,
)

mapping = SchemaMapping.from_yaml("config/mappings/tpch.yaml")
llm = make_llm(load_model_config("config/models/anthropic.yaml"))
target = make_target("cypher")
validator = make_validator("cypher", "syntax")

with SQLTranslator(mapping, llm, target, validator) as translator:
    result = translator.translate("SELECT name FROM supplier WHERE suppkey = 1337")
    print(result.generated_query)
```

## `ddl/`: where mappings come from

A mapping YAML is not written from scratch. `config/ddl/tpch.sql` is the
raw TPC-H relational schema, and `build_mapping` turns that DDL into a
first-draft `config/mappings/tpch.yaml` for review. The two are kept in
sync by `tests/test_mapping_builder.py`.

```python
from pathlib import Path
from rows2graph import build_mapping, load_model_config, make_llm

llm = make_llm(load_model_config("config/models/anthropic.yaml"))
result = build_mapping(ddl=Path("config/ddl/tpch.sql").read_text(), llm=llm)
Path("config/mappings/tpch.yaml").write_text(result.yaml)
```

## Secrets

Both `models/` and `servers/` YAML files support environment-variable
interpolation. A string of the form `${VAR}` is replaced by
`os.environ["VAR"]` at config-load time; if `VAR` is unset, the loader
raises `KeyError` with a precise message. Mapping YAMLs do **not** support
interpolation: they hold no secrets and are deployment-invariant.

Set the referenced variables in your shell before loading the configs:

```bash
export ANTHROPIC_API_KEY=...   # models/anthropic.yaml (the SDK reads it from the env)
export OLLAMA_HOST=...         # models/ollama.yaml (the SDK can read it from the env)
export NEO4J_PASSWORD=...      # servers/neo4j.yaml
export ARANGO_PASSWORD=...     # servers/arangodb.yaml
```
