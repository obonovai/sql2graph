# API reference

This document is the canonical reference for the public Python API of
`rows2graph` and for the three YAML schemas the demo CLI consumes. For the
*why* behind the design see `ARCHITECTURE.md`; for hands-on usage see the
top-level `README.md` and `demo/README.md`.

All public symbols are re-exported from the top-level package:

```python
from rows2graph import SchemaMapping, SQLTranslator, ...
```

---

## Library public surface

### `SchemaMapping` (and `NodeMapping`, `EdgeMapping`)

```python
class SchemaMapping(StrictModel):
    nodes: list[NodeMapping]
    edges: list[EdgeMapping]

    @classmethod
    def from_yaml(cls, path: Path | str) -> SchemaMapping: ...
```

A Pydantic-validated description of how a relational schema maps to a
property-graph model. Edge `source_node` / `target_node` are checked at
load time against the declared node labels; mismatches raise
`pydantic.ValidationError`.

### LLM components

```python
class LLMClient(Protocol):
    def chat(self, messages: list[dict[str, Any]]) -> str: ...
    def close(self) -> None: ...

ModelConfig = OllamaConfig | AnthropicConfig   # discriminated on .provider

def load_model_config(path: Path | str) -> OllamaConfig | AnthropicConfig
def make_llm(config: OllamaConfig | AnthropicConfig) -> LLMClient
```

`OllamaConfig` and `AnthropicConfig` are typed Pydantic models. See the
YAML reference below for their field layouts.

### Target language components

```python
class TargetLanguage(Protocol):
    @property
    def name(self) -> str: ...
    def system_prompt_section(self) -> str: ...
    def extract_query(self, llm_response: str) -> str: ...

def make_target(name: str, *, graph_name: str | None = None) -> TargetLanguage
```

`name` ∈ `{"cypher", "aql"}`. `graph_name` is only meaningful for
`"aql"`.

### Validator components

```python
class QueryValidator(Protocol):
    def validate(self, query: str) -> list[str]: ...
    def close(self) -> None: ...

ServerConfig = Neo4jConfig | ArangoDBConfig    # discriminated on .type

def load_server_config(path: Path | str) -> Neo4jConfig | ArangoDBConfig
def make_validator(
    target: str,                                     # "cypher" | "aql"
    mode: str,                                       # "syntax" | "server" | "none"
    *,
    server_config: Neo4jConfig | ArangoDBConfig | None = None,
) -> QueryValidator
```

`make_validator` raises `ValueError` if `mode == "server"` and
`server_config` is missing, and `TypeError` if the `server_config`'s type
does not match `target`.

### Orchestrator: `SQLTranslator`

```python
class SQLTranslator:
    def __init__(
        self,
        schema_mapping: SchemaMapping,
        llm: LLMClient,
        target: TargetLanguage,
        validator: QueryValidator,
        max_iterations: int = 3,
    ) -> None: ...

    def translate(self, sql_query: str) -> TranslationResult: ...
    def close(self) -> None: ...
    def __enter__(self) -> SQLTranslator: ...
    def __exit__(self, *exc: object) -> None: ...
```

`SQLTranslator` is a context manager: use `with SQLTranslator(...)` to
ensure both the LLM client and the validator are closed even on exception.

### `TranslationResult`

```python
class TranslationResult(BaseModel):
    sql_query: str
    generated_query: str | None        # last attempt, even on failure
    target_language: Literal["cypher", "aql"]
    validation_passed: bool
    validation_errors: list[str]       # from the final iteration
    iterations_used: int               # validate calls performed
    status: str                        # "success" | "max_iterations_reached"
    duration_seconds: float
```

---

## End-to-end example (library)

```python
from rows2graph import (
    SchemaMapping, SQLTranslator,
    load_model_config, make_llm,
    make_target, make_validator,
)

mapping = SchemaMapping.from_yaml("config/mappings/tpch.yaml")
llm = make_llm(load_model_config("config/models/anthropic.yaml"))
target = make_target("cypher")
validator = make_validator("cypher", "syntax")

with SQLTranslator(
    schema_mapping=mapping,
    llm=llm,
    target=target,
    validator=validator,
    max_iterations=3,
) as translator:
    result = translator.translate("SELECT name FROM supplier WHERE suppkey = 1337")
    if result.validation_passed:
        print(result.generated_query)
    else:
        raise RuntimeError(
            f"Translation failed after {result.iterations_used} iterations: "
            f"{result.validation_errors}"
        )
```

---

## YAML schema reference

### `config/mappings/<name>.yaml` — schema mapping

The file *is* the mapping: there is no `schema_mapping:` outer key.

```yaml
nodes:
  - label: "Person"                   # graph vertex label
    source_table: "employees"         # relational table
    properties:                       # graph_property -> sql_column
      name: "first_name"
      email: "email_address"
    primary_key: "employee_id"        # SQL column

  - label: "Department"
    source_table: "departments"
    properties:
      name: "dept_name"
    primary_key: "dept_id"

edges:
  - type: "WORKS_IN"                  # graph relationship type
    source_node: "Person"             # must match a label in nodes[]
    target_node: "Department"
    source_table: "employees"
    source_foreign_key: "dept_id"     # FK in source_table
    target_primary_key: "dept_id"     # PK in target node's table
    properties:                       # optional edge properties
      since: "hire_date"
```

Field reference:

| Node field | Type | Required | Description |
|---|---|---|---|
| `label` | string | yes | Graph node label. Used verbatim in queries. |
| `source_table` | string | yes | Relational table. |
| `properties` | `dict[str, str]` | yes | Graph property name → SQL column. |
| `primary_key` | string | yes | SQL column uniquely identifying rows. |

| Edge field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | Relationship type. Used verbatim in queries. |
| `source_node` | string | yes | A node `label`. Checked at load time. |
| `target_node` | string | yes | A node `label`. Self-references allowed. |
| `source_table` | string | yes | Table containing the FK. |
| `source_foreign_key` | string | yes | FK column in `source_table`. |
| `target_primary_key` | string | yes | PK column in target node's table. |
| `properties` | `dict[str, str]` | no | Optional edge properties. |

Loaded with `SchemaMapping.from_yaml(path)`. Strict mode (`extra="forbid"`)
rejects unknown keys.

### `config/models/<name>.yaml` — LLM model config

Discriminator: `provider`.

#### Ollama (`provider: "ollama"`)

```yaml
provider: "ollama"
model: "llama3.2"                    # must be pulled on the Ollama server
host: "http://localhost:11434"
temperature: 0.1
num_ctx: 8192                        # context window size in tokens
```

#### Anthropic (`provider: "anthropic"`)

```yaml
provider: "anthropic"
# api_key: "${ANTHROPIC_API_KEY}"     # optional; SDK reads env var if omitted
model: "claude-opus-4-7"
temperature: 0.1
max_output_tokens: 4096
```

Authentication is via the `ANTHROPIC_API_KEY` environment variable. The
upstream SDK reads it automatically when `api_key` is omitted; that is the
recommended posture so the YAML file remains safe to commit. You may also
set `api_key` explicitly (with `${ENV_VAR}` interpolation, e.g.
`api_key: "${ANTHROPIC_API_KEY}"`). Set budget caps and usage alerts in
the [Anthropic console](https://console.anthropic.com).

### `config/servers/<name>.yaml` — graph DB connection

Discriminator: `type`. Used only with `--validation server`. All string
fields support `${ENV_VAR}` interpolation; an unset variable raises
`KeyError`.

#### Neo4j (`type: "neo4j"`)

```yaml
type: "neo4j"
uri: "bolt://localhost:7687"
username: "neo4j"
password: "${NEO4J_PASSWORD}"
database: "neo4j"
```

The validator runs `EXPLAIN <query>`, which parses and plans without
executing — safe for any statement.

#### ArangoDB (`type: "arangodb"`)

```yaml
type: "arangodb"
url: "http://localhost:8529"
username: "root"
password: "${ARANGO_PASSWORD}"
database: "ldbc"
graph_name: "ldbc"                   # named graph used in AQL traversals
```

The validator runs `db.aql.validate(query)`. `graph_name` here also feeds
the AQL target-language prompt unless the demo's `--aql-graph-name` flag
overrides it.

---

## Common validation errors

```
ValidationError: Edge 'WORKS_IN' references undefined source_node 'Employee'
```
An edge's `source_node` or `target_node` does not match any `label` in
`nodes[]`. Labels are case-sensitive.

```
KeyError: Environment variable '${NEO4J_PASSWORD}' is referenced in config but not set
```
A server (or model) config references an environment variable that you
have not exported. Run `export NEO4J_PASSWORD=...` first.

```
ValidationError: extra fields not permitted
```
Typo in a field name. Pydantic reports the offending field; compare
against the schemas above.
