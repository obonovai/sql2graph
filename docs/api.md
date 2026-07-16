# API reference

**The public Python API surface: every exported symbol, with signatures.**

This document is the canonical reference for the public Python API of
`sql2graph`.

## Scope

This page owns: every exported symbol, its signature, and its contract;
the result and event types; the end-to-end library examples. Related topics
live with their owners:

- [architecture.md](architecture.md): the *why* behind this surface.
- [mapping/format.md](mapping/format.md): the schema-mapping YAML these
  classes load.
- [configuration.md](configuration.md): the model and server config YAML.
- [validation/syntax.md](validation/syntax.md): internals of the syntax
  validators listed here.
- [mapping/builder.md](mapping/builder.md): the mapping-builder pipeline
  behind `build_mapping`.
- [troubleshooting.md](troubleshooting.md): the canonical
  `TranslationResult.status` table and symptom-first fixes.

All public symbols are re-exported from the top-level package:

```python
from sql2graph import SchemaMapping, SQLTranslator, ...
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
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
    ) -> ChatReply: ...
    def close(self) -> None: ...


StreamCallback = Callable[[str], None]
"""Receives one text delta per call when streaming."""


class AsyncLLMClient(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream_to: StreamCallback | None = None,
        temperature: float | None = None,
    ) -> ChatReply: ...
    async def close(self) -> None: ...


ModelConfig = OllamaConfig | AnthropicConfig   # discriminated on .provider

def load_model_config(path: Path | str) -> OllamaConfig | AnthropicConfig
def make_llm(config: OllamaConfig | AnthropicConfig) -> LLMClient
def make_async_llm(config: OllamaConfig | AnthropicConfig) -> AsyncLLMClient
```

`OllamaConfig` and `AnthropicConfig` are typed Pydantic models: same
config drives both the sync and async backends. See
[configuration.md](configuration.md) for their field layouts; both include
`max_retries: int = 3`.

Concrete classes (exported from the top-level package):
`AnthropicLLMClient` / `AsyncAnthropicLLMClient`,
`OllamaLLMClient` / `AsyncOllamaLLMClient`. Use the factories above unless
you have a reason to instantiate directly.

Both `chat` methods return a `ChatReply` - the assistant turn's text
(`.text`) plus the `TokenUsage` it cost (`.usage`). The optional
`temperature` overrides the backend's configured sampling temperature for a
single call; the translator loop uses it to raise entropy on a stall-breaking
escalation retry. See [Token usage](#token-usage) below for `TokenUsage`.

When `AsyncLLMClient.chat` is called with a non-None `stream_to`, the
implementation switches to its provider's streaming endpoint and invokes
the callback for each text delta as it arrives, returning the assembled
`ChatReply` when the stream completes. With `stream_to=None` (the default)
the call is a single-round-trip request, useful for callers that don't need
a live display.

### Target language components

```python
class TargetLanguage(Protocol):
    @property
    def name(self) -> str: ...
    def system_prompt_section(self, features: frozenset[SqlFeature]) -> str: ...
    def extract_query(self, llm_response: str) -> str: ...
    def repair_hint(self, errors: list[str]) -> str | None: ...

def make_target(name: str) -> TargetLanguage
```

`name` ∈ `{"cypher", "aql", "gremlin"}`. `system_prompt_section` receives the
`frozenset[SqlFeature]` detected in the SQL and returns the always-on base
block plus the rule chunks gated on those features (see
[Per-query prompt assembly](architecture.md#per-query-prompt-assembly)).
`repair_hint` lets a target inject clause-specific fix guidance for a class of
validator errors - `AqlTarget` uses it for the `RETURN`-must-be-last ordering
trap - and returns `None` when the default "fix only the reported errors,
don't restructure" instruction should stand.

For the `"gremlin"` target the framework emits Gremlin-Groovy script form
(e.g. `g.V().hasLabel('Person').valueMap()`), portable across Apache
TinkerPop Gremlin Server / TinkerGraph (the recommended local backend),
JanusGraph, Amazon Neptune, and Azure Cosmos DB Gremlin API. Server-side
validation against schemaless TinkerGraph catches script-level parse
errors and unsupported steps but NOT label / property hallucinations.
Use JanusGraph with a registered schema for schema-aware validation
comparable to Neo4j's `EXPLAIN`.

### Validator components

> Deep dive (implementation, grammar provenance, and how to regenerate the
> parsers): see [validation/syntax.md](validation/syntax.md).

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

`mode == "syntax"` builds the deployment-free, grammar-based validator (ANTLR).
Cypher and Gremlin use each engine's own published grammar; AQL uses a hand-port
of ArangoDB's Flex+Bison grammar (ArangoDB publishes no reusable offline
grammar), so the AQL syntax check is best-effort and the `server` / `managed`
validator remains authoritative. `valid_modes_for_target(target)` returns
`("none", "syntax", "server")` for all three targets, so downstream callers can
offer the right choices without hardcoding the rule.

`mode == "managed"` needs no `server_config`: it returns a
`ManagedServerValidator` (async: `AsyncManagedServerValidator`) that
provisions a throwaway database for `target` via `testcontainers` on the
first `validate()` and tears it down on `close()`. It requires a running
Docker daemon and raises `RuntimeError` from `validate()` if none is
reachable. For Cypher, the managed Neo4j connection sets
`Neo4jConfig.notifications_min_severity="OFF"` (an optional config field) so the
server never sends the advisory notifications the validator would otherwise
surface as schema errors. On the empty managed database those are guaranteed
noise (every label and property would look unknown and fail every query);
parse and plan errors are unaffected. See
[validation/modes.md](validation/modes.md#mode-managed).

Concrete async classes (also exported from the top-level package):
`AsyncCypherSyntaxValidator`, `AsyncGremlinSyntaxValidator`,
`AsyncAqlSyntaxValidator`, `AsyncNoopValidator`,
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
        escalation_temperature: float = 0.6,
        fix_temperature: float | None = None,
        parse_error_action: PreflightAction = PreflightAction.WARN,
        unmapped_tables_action: PreflightAction = PreflightAction.REJECT,
        unmapped_columns_action: PreflightAction = PreflightAction.REJECT,
        dialect: str | None = None,
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
        escalation_temperature: float = 0.6,
        fix_temperature: float | None = None,
        parse_error_action: PreflightAction = PreflightAction.WARN,
        unmapped_tables_action: PreflightAction = PreflightAction.REJECT,
        unmapped_columns_action: PreflightAction = PreflightAction.REJECT,
        dialect: str | None = None,
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

Beyond the four components and `max_iterations`, the constructor exposes the
stall-escalation and input-gate knobs: `escalation_temperature` (default `0.6`)
and `fix_temperature` (default: the backend temperature) tune retry sampling;
`parse_error_action`, `unmapped_tables_action`, and `unmapped_columns_action`
(each a `PreflightAction`, defaulting to `WARN` / `REJECT` / `REJECT`) set the
input-side pre-flight policy; and `dialect` selects the sqlglot dialect used for
input analysis only (it never enters the LLM prompt). See
[Pre-flight and unmapped-input handling](#pre-flight-and-unmapped-input-handling)
below.

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
`docs/architecture.md` § "Async path" for the rationale and the parity
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
    status: str                        # see the status values below
    unmapped_tables: list[str] = []    # set on a pre-flight tables reject
    unmapped_columns: list[str] = []   # set on a pre-flight columns reject
    duration_seconds: float = 0.0
    token_usage: TokenUsage = TokenUsage()   # tokens billed across the loop
```

`status` is one of `"success"`, `"max_iterations_reached"`, `"stalled"`,
`"parse_error"`, `"unmapped_tables"`, or `"unmapped_columns"`. The canonical
interpretation table (what happened, which fields to inspect, what to do) is in
[troubleshooting.md](troubleshooting.md#interpreting-translationresultstatus).
(`"pending"` is an internal sentinel and never appears on a returned result.)

### Token usage

```python
class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0          # Anthropic prompt cache; 0 for Ollama
    cache_creation_tokens: int = 0      # Anthropic prompt cache; 0 for Ollama
    @property
    def total_tokens(self) -> int: ...  # sum of the four: every billed token
```

`TranslationResult.token_usage` accumulates the per-call `TokenUsage` across the
whole generate-validate-fix loop. For Anthropic, `input_tokens` counts the
*uncached* prompt portion only; tokens served from or written to the prompt
cache are reported in `cache_read_tokens` / `cache_creation_tokens`. Ollama
populates `input_tokens` / `output_tokens` from the response's
`prompt_eval_count` / `eval_count`; it has no prompt cache, so its two cache
fields stay `0`. `ChatReply` (returned by
`LLMClient.chat` / `AsyncLLMClient.chat`) bundles the assistant text with the
`TokenUsage` for a single call; `TokenUsage` instances are additive with `+`.

### Iteration events

Both `translate()` methods accept an optional `on_event` callback fired
at every loop milestone. Events are immutable frozen dataclasses:

```python
# Pre-flight (input-side) events -- at most one fires, before generation:
@dataclass(frozen=True)
class ParseFailedEvent:                # the SQL did not parse
    message: str

@dataclass(frozen=True)
class UnmappedTablesEvent:             # SQL reads tables absent from the mapping
    tables: list[str]
    message: str

@dataclass(frozen=True)
class UnmappedColumnsEvent:            # SQL uses columns a mapped table omits
    columns: list[str]
    message: str

# Loop events:
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
class StalledEvent:                    # no-progress escalation (fresh-context retry)
    iteration: int
    query: str
    errors: list[str]

@dataclass(frozen=True)
class MaxIterationsReachedEvent:
    iteration: int
    errors: list[str]

@dataclass(frozen=True)
class CompletedEvent:                  # always emitted last
    result: TranslationResult

TranslationEvent = (
    ParseFailedEvent | UnmappedTablesEvent | UnmappedColumnsEvent
    | GeneratedEvent | ValidatedEvent | FixGeneratedEvent
    | StalledEvent | MaxIterationsReachedEvent | CompletedEvent
)
EventHandler = Callable[[TranslationEvent], None]
```

Iteration numbering: `iteration=N` refers to validation pass N.
`FixGeneratedEvent.iteration=N` means "the fix produced after iteration N
failed; this candidate will be validated as iteration N+1." A handler is
typically a `match` over the union. See the end-to-end example below.

The three pre-flight events (`ParseFailedEvent`, `UnmappedTablesEvent`,
`UnmappedColumnsEvent`) fire at most once each, before `GeneratedEvent`, when
the corresponding input-gate check trips; `StalledEvent` fires at most once when
the loop escalates on no progress. See
[Pre-flight and unmapped-input handling](#pre-flight-and-unmapped-input-handling)
below.

Handler exceptions are caught and logged at WARNING by the translator;
they cannot abort a translation. Consumers should treat the handler as
an observer hook, not a control point.

### Mapping builder

Generate a *first-draft* `SchemaMapping` from SQL `CREATE TABLE` DDL, so the
mapping need not be written by hand. The full reference - the three-stage
pipeline, the refinement guardrail, and a worked example - is in
[mapping/builder.md](mapping/builder.md).

```python
def build_mapping(*, ddl: str, dialect: str | None = None, llm: LLMClient | None = None) -> BuildResult
async def build_mapping_async(
    *,
    ddl: str,
    dialect: str | None = None,
    llm: AsyncLLMClient | None = None,
    on_conversation: ConversationCallback | None = None,
) -> BuildResult

def extract_schema_from_ddl(ddl: str, *, dialect: str | None = None) -> RelationalSchema
def project_to_mapping(schema: RelationalSchema) -> ProjectionResult   # deterministic, offline
def mapping_to_yaml(mapping: SchemaMapping, *, header: str | None = None) -> str
def diff_mappings(before: SchemaMapping, after: SchemaMapping) -> MappingDiff
```

The naming-refinement pass runs only when an `llm` is supplied; with
`llm=None` (the default) `build_mapping` is deterministic, offline, and free.
The pass is guarded, so if the model errors, is unreachable, or would violate
the preservation guardrail, the deterministic mapping is kept and the reason is
added to `BuildResult.warnings` (the result is always valid). The projection
stage alone is also available directly as `project_to_mapping`; serialise
either result with `mapping_to_yaml`. `build_mapping` raises `DdlParseError`
if the DDL cannot be parsed.

`BuildResult` fields:

| Field | Type | Description |
|---|---|---|
| `mapping` | `SchemaMapping` | The generated mapping (refined, or deterministic on guardrail rejection). |
| `yaml` | `str` | `mapping` serialised to canonical YAML, ready to save or `SchemaMapping.from_yaml`. |
| `report` | `CoverageReport` | How the relational schema was projected (and anything dropped). |
| `refined` | `bool` | `True` iff the naming pass changed the deterministic skeleton. |
| `warnings` | `list[str]` | Non-fatal issues (synthesized keys, dropped edges, rejected refinement). |
| `skeleton_yaml` | `str` | The deterministic YAML before refinement, for side-by-side review. |
| `conversation` | `list[dict[str, str]]` | The refinement chat transcript (system / user / assistant). |
| `diff` | `MappingDiff \| None` | The renames the LLM applied (labels, edge types, property keys). |

Also exported: `RelationalSchema`, `CoverageReport`, `MappingDiff`, `RenameDiff`,
and `DdlParseError`.

### Pre-flight and unmapped-input handling

Before any LLM call, both translators run an input-side pre-flight gate
(`src/sql2graph/engine/preflight.py`): it checks that the SQL parses, that every table
it reads is in the mapping, and that every column it names on a mapped table is
exposed. See [Pre-flight gate](architecture.md#pre-flight-gate-input-side) for
the design and defaults.

Each check's policy is a `PreflightAction`, passed to the translator constructor
(`parse_error_action`, `unmapped_tables_action`, `unmapped_columns_action`):

```python
class PreflightAction(StrEnum):
    IGNORE = "ignore"   # do nothing
    WARN   = "warn"     # emit the event, translate anyway
    REJECT = "reject"   # emit the event, skip the LLM, return a terminal result
```

On a `REJECT` the returned `TranslationResult` carries the matching `status`
(`parse_error` / `unmapped_tables` / `unmapped_columns`), lists the offending
names in `unmapped_tables` / `unmapped_columns`, and has `generated_query=None`
with zero `token_usage`. On a `WARN` the corresponding event
(`ParseFailedEvent` / `UnmappedTablesEvent` / `UnmappedColumnsEvent`) fires and
the flagged names still surface on the result.

The gate is fed by `analyze_sql`, which parses the SQL once with sqlglot:

```python
def analyze_sql(sql_query: str, *, dialect: str | None = None) -> SqlAnalysis

@dataclass(frozen=True)
class SqlAnalysis:
    features: frozenset[SqlFeature]          # ALL_FEATURES on a parse failure
    source_tables: frozenset[str]            # real tables read (CTEs/aliases excluded)
    parse_ok: bool
    column_refs: frozenset[tuple[str, str]]  # (table, column) pairs, where attributable
```

To run the coverage checks directly (e.g. to preview unmapped input in a UI
without invoking a translator), two helpers are exported:

```python
def find_unmapped_tables(sql_tables: frozenset[str], mapping: SchemaMapping) -> list[str]
def find_unmapped_columns(column_refs: frozenset[tuple[str, str]], mapping: SchemaMapping) -> list[str]
```

Both take an `analyze_sql(...)` result's `source_tables` / `column_refs` and
return the offending names (case-insensitive comparison, sorted) — the same
checks the pre-flight gate applies, decoupled from its `PreflightAction` policy.

### Constants and helpers

Canonical name sets and mode helpers, exported so callers don't hardcode them:

| Name | Value / purpose |
|---|---|
| `VALID_TARGETS` | `("cypher", "aql", "gremlin")` |
| `VALID_PROVIDERS` | `("ollama", "anthropic")` |
| `VALID_VALIDATION_MODES` | `("none", "syntax", "server")` (user-facing; `managed` is derived) |
| `TARGET_SERVER_TYPE` | `{"cypher": "neo4j", "aql": "arangodb", "gremlin": "gremlin"}` |
| `valid_modes_for_target(target)` | Modes available for a target - `("none", "syntax", "server")` for all three. |
| `resolve_validation_mode(mode, *, server_config)` | `"server"` with no `server_config` resolves to `"managed"` (auto-provisioned); otherwise passthrough. |

`SemanticType` (also exported) is the enum of property semantic types the mapping
builder assigns from SQL column types.

---

## End-to-end example (library)

```python
from sql2graph import (
    SchemaMapping, SQLTranslator,
    load_model_config, make_llm,
    make_target, make_validator,
)

mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")
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
from sql2graph import (
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
    mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")
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

## YAML file formats

The three YAML schemas the library consumes are documented on their own pages:

- [mapping/format.md](mapping/format.md): the schema mapping
  (`examples/mappings/<name>.yaml`), including typed properties, composite
  keys, and list properties.
- [configuration.md](configuration.md): the LLM model config
  (`config/models/<name>.yaml`) and the graph server config
  (`config/servers/<name>.yaml`), plus `${VAR}` interpolation and the
  canonical environment-variable table.
