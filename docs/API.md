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


StreamCallback = Callable[[str], None]
"""Receives one text delta per call when streaming."""


class AsyncLLMClient(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream_to: StreamCallback | None = None,
    ) -> str: ...
    async def close(self) -> None: ...


ModelConfig = OllamaConfig | AnthropicConfig   # discriminated on .provider

def load_model_config(path: Path | str) -> OllamaConfig | AnthropicConfig
def make_llm(config: OllamaConfig | AnthropicConfig) -> LLMClient
def make_async_llm(config: OllamaConfig | AnthropicConfig) -> AsyncLLMClient
```

`OllamaConfig` and `AnthropicConfig` are typed Pydantic models: same
config drives both the sync and async backends. See the YAML reference
below for their field layouts; both include `max_retries: int = 3`.

Concrete classes (exported from the top-level package):
`AnthropicLLMClient` / `AsyncAnthropicLLMClient`,
`OllamaLLMClient` / `AsyncOllamaLLMClient`. Use the factories above unless
you have a reason to instantiate directly.

When `AsyncLLMClient.chat` is called with a non-None `stream_to`, the
implementation switches to its provider's streaming endpoint and invokes
the callback for each text delta as it arrives, returning the assembled
text when the stream completes. With `stream_to=None` (the default) the
call is a single-round-trip request, useful for callers that don't need
a live display.

### Target language components

```python
class TargetLanguage(Protocol):
    @property
    def name(self) -> str: ...
    def system_prompt_section(self, features: frozenset[SqlFeature]) -> str: ...
    def extract_query(self, llm_response: str) -> str: ...

def make_target(name: str) -> TargetLanguage
```

`name` ∈ `{"cypher", "aql", "gremlin"}`. `system_prompt_section` receives the
`frozenset[SqlFeature]` detected in the SQL and returns the always-on base
block plus the rule chunks gated on those features (see
[Per-query prompt assembly](#) in `docs/ARCHITECTURE.md`).

For the `"gremlin"` target the framework emits Gremlin-Groovy script form
(e.g. `g.V().hasLabel('Person').valueMap()`), portable across Apache
TinkerPop Gremlin Server / TinkerGraph (the recommended local backend),
JanusGraph, Amazon Neptune, and Azure Cosmos DB Gremlin API. Server-side
validation against schemaless TinkerGraph catches script-level parse
errors and unsupported steps but NOT label / property hallucinations.
Use JanusGraph with a registered schema for schema-aware validation
comparable to Neo4j's `EXPLAIN`.

### Validator components

```python
class QueryValidator(Protocol):
    def validate(self, query: str) -> list[str]: ...
    def close(self) -> None: ...


class AsyncQueryValidator(Protocol):
    async def validate(self, query: str) -> list[str]: ...
    async def close(self) -> None: ...


ServerConfig = Neo4jConfig | ArangoDBConfig | GremlinConfig   # discriminated on .type

def load_server_config(path: Path | str) -> Neo4jConfig | ArangoDBConfig | GremlinConfig
def make_validator(
    target: str,                                     # "cypher" | "aql" | "gremlin"
    mode: str,                                       # "syntax" | "server" | "managed" | "none"
    *,
    server_config: Neo4jConfig | ArangoDBConfig | GremlinConfig | None = None,
) -> QueryValidator
def make_async_validator(
    target: str,
    mode: str,
    *,
    server_config: Neo4jConfig | ArangoDBConfig | GremlinConfig | None = None,
) -> AsyncQueryValidator
```

Both factories raise `ValueError` if `mode == "server"` and
`server_config` is missing, and `TypeError` if the `server_config`'s type
does not match `target`.

`mode == "managed"` needs no `server_config`: it returns a
`ManagedServerValidator` (async: `AsyncManagedServerValidator`) that
provisions a throwaway database for `target` via `testcontainers` on the
first `validate()` and tears it down on `close()`. It requires a running
Docker daemon and raises `RuntimeError` from `validate()` if none is
reachable. For Cypher, the managed Neo4j connection sets
`Neo4jConfig.notifications_min_severity="OFF"` (an optional config field) so the
empty database's advisory notifications (e.g. unknown label/property) are not
logged. This does not change which queries pass or fail.

Concrete async classes (also exported from the top-level package):
`AsyncCypherSyntaxValidator`, `AsyncAqlSyntaxValidator`,
`AsyncGremlinSyntaxValidator`, `AsyncNoopValidator`,
`AsyncCypherServerValidator` (uses `neo4j.AsyncGraphDatabase`),
`AsyncAqlServerValidator` (wraps python-arango calls in
`asyncio.to_thread`: the underlying SDK has no async driver), and
`AsyncGremlinServerValidator` (wraps `gremlinpython`'s `Client` in
`asyncio.to_thread` for the same reason: the async surface area of
`gremlinpython` is inconsistent across releases). Same constructor
signatures as their sync siblings.

### Orchestrator: `SQLTranslator` (sync) and `AsyncSQLTranslator` (async)

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

    def translate(
        self,
        sql_query: str,
        on_event: EventHandler | None = None,
    ) -> TranslationResult: ...
    def close(self) -> None: ...
    def __enter__(self) -> SQLTranslator: ...
    def __exit__(self, *exc: object) -> None: ...


class AsyncSQLTranslator:
    def __init__(
        self,
        schema_mapping: SchemaMapping,
        llm: AsyncLLMClient,
        target: TargetLanguage,
        validator: AsyncQueryValidator,
        max_iterations: int = 3,
    ) -> None: ...

    async def translate(
        self,
        sql_query: str,
        on_event: EventHandler | None = None,
        stream_to: StreamCallback | None = None,
        on_conversation: ConversationCallback | None = None,
    ) -> TranslationResult: ...
    async def close(self) -> None: ...
    async def __aenter__(self) -> AsyncSQLTranslator: ...
    async def __aexit__(self, *exc: object) -> None: ...
```

Both translators are context managers: use `with SQLTranslator(...)` or
`async with AsyncSQLTranslator(...)` to ensure the LLM client and
validator are closed even on exception.

After each `translate()` call, `translator.last_messages` holds the full
system↔LLM conversation for that call, a `list[dict[str, str]]` of
`{"role", "content"}` turns (system prompt, generate prompt, raw model
responses, and each fix prompt). It is overwritten on the next call;
`TranslationResult` itself deliberately omits the chat history.

For a *live* view, `AsyncSQLTranslator.translate(on_conversation=...)` takes a
`ConversationCallback` (`Callable[[list[dict[str, str]]], None]`) that receives the
growing message snapshot as it changes, after each prompt and per-token while an
assistant turn streams. Setting it implies streaming from the LLM even without
`stream_to`. (Only the async translator exposes it; the sync `SQLTranslator` keeps
just `last_messages`.)

Same `TranslationResult`, same iteration semantics, same prompts. See
`docs/ARCHITECTURE.md` § "Async path" for the rationale and the parity
contract.

### `TranslationResult`

```python
class TranslationResult(BaseModel):
    sql_query: str
    generated_query: str | None        # last attempt, even on failure
    target_language: Literal["cypher", "aql", "gremlin"]
    validation_passed: bool
    validation_errors: list[str]       # from the final iteration
    iterations_used: int               # validate calls performed
    status: str                        # "success" | "max_iterations_reached"
    duration_seconds: float
```

### Iteration events

Both `translate()` methods accept an optional `on_event` callback fired
at every loop milestone. Events are immutable frozen dataclasses:

```python
@dataclass(frozen=True)
class GeneratedEvent:                  # initial LLM call finished
    iteration: int                     # always 1
    query: str

@dataclass(frozen=True)
class ValidatedEvent:                  # one validate() call finished
    iteration: int
    query: str
    errors: list[str]
    passed: bool

@dataclass(frozen=True)
class FixGeneratedEvent:               # a fix LLM call finished
    iteration: int                     # the iteration that just failed
    query: str                         # candidate for iteration N+1

@dataclass(frozen=True)
class MaxIterationsReachedEvent:
    iteration: int
    errors: list[str]

@dataclass(frozen=True)
class CompletedEvent:                  # always emitted last
    result: TranslationResult

TranslationEvent = (
    GeneratedEvent | ValidatedEvent | FixGeneratedEvent
    | MaxIterationsReachedEvent | CompletedEvent
)
EventHandler = Callable[[TranslationEvent], None]
```

Iteration numbering: `iteration=N` refers to validation pass N.
`FixGeneratedEvent.iteration=N` means "the fix produced after iteration N
failed; this candidate will be validated as iteration N+1." A handler is
typically a `match` over the union. See the end-to-end example below.

Handler exceptions are caught and logged at WARNING by the translator;
they cannot abort a translation. Consumers should treat the handler as
an observer hook, not a control point.

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

### Async variant with events and streaming

```python
import asyncio
from rows2graph import (
    AsyncSQLTranslator, SchemaMapping,
    GeneratedEvent, ValidatedEvent, FixGeneratedEvent,
    MaxIterationsReachedEvent, CompletedEvent, TranslationEvent,
    load_model_config, make_async_llm, make_async_validator, make_target,
)


def on_event(event: TranslationEvent) -> None:
    match event:
        case GeneratedEvent(iteration=i, query=q):
            print(f"[iter {i}] generated: {q!r}")
        case ValidatedEvent(iteration=i, passed=True):
            print(f"[iter {i}] ✓ validation passed")
        case ValidatedEvent(iteration=i, errors=errs, passed=False):
            print(f"[iter {i}] ✗ {len(errs)} validation error(s)")
        case FixGeneratedEvent(iteration=i, query=q):
            print(f"[iter {i + 1}] fix: {q!r}")
        case MaxIterationsReachedEvent(iteration=i):
            print(f"gave up at iter {i}")
        case CompletedEvent(result=r):
            print(f"done in {r.duration_seconds:.2f}s")


async def main() -> None:
    mapping = SchemaMapping.from_yaml("config/mappings/tpch.yaml")
    llm = make_async_llm(load_model_config("config/models/anthropic.yaml"))
    target = make_target("cypher")
    validator = make_async_validator("cypher", "syntax")

    async with AsyncSQLTranslator(mapping, llm, target, validator) as translator:
        result = await translator.translate(
            "SELECT name FROM supplier WHERE suppkey = 1337",
            on_event=on_event,
            stream_to=lambda delta: print(delta, end="", flush=True),
        )

    if not result.validation_passed:
        raise RuntimeError(
            f"Translation failed after {result.iterations_used} iterations: "
            f"{result.validation_errors}"
        )


asyncio.run(main())
```

---

## YAML schema reference

### `config/mappings/<name>.yaml`: schema mapping

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

### `config/models/<name>.yaml`: LLM model config

Discriminator: `provider`.

#### Ollama (`provider: "ollama"`)

```yaml
provider: "ollama"
model: "llama3.2"                    # must be pulled on the Ollama server
host: "http://localhost:11434"
temperature: 0.1
num_ctx: 8192                        # context window size in tokens
max_retries: 3                       # exponential backoff on connection errors
                                     # and 5xx; 4xx propagates immediately.
```

#### Anthropic (`provider: "anthropic"`)

```yaml
provider: "anthropic"
# api_key: "${ANTHROPIC_API_KEY}"     # optional; SDK reads env var if omitted
model: "claude-opus-4-7"
temperature: 0.1
max_output_tokens: 4096
max_retries: 3                       # forwarded to the Anthropic SDK's
                                     # built-in retry layer (exp. backoff +
                                     # jitter on 408/409/429/5xx).
```

Authentication is via the `ANTHROPIC_API_KEY` environment variable. The
upstream SDK reads it automatically when `api_key` is omitted; that is the
recommended posture so the YAML file remains safe to commit. You may also
set `api_key` explicitly (with `${ENV_VAR}` interpolation, e.g.
`api_key: "${ANTHROPIC_API_KEY}"`). Set budget caps and usage alerts in
the [Anthropic console](https://console.anthropic.com).

### `config/servers/<name>.yaml`: graph DB connection

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
executing, safe for any statement.

#### ArangoDB (`type: "arangodb"`)

```yaml
type: "arangodb"
url: "http://localhost:8529"
username: "root"
password: "${ARANGO_PASSWORD}"
database: "ldbc"
```

The validator runs `db.aql.validate(query)`. Generated AQL uses bare
edge-collection traversals (`FOR v IN OUTBOUND <doc> <EdgeCollection>`),
so no named graph is referenced or configured.

#### Gremlin (`type: "gremlin"`)

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
