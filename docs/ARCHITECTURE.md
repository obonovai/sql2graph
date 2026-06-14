# Architecture

This document explains the *why* behind the design decisions in `rows2graph`.
For setup and usage see `README.md`; for the library API and YAML schemas
see `API.md`.

---

## Design goals

The framework deliberately optimises for four properties, in this order:

1. **Separability of concerns at the configuration boundary.** The three
   external resources the framework depends on — a relational-to-graph
   *mapping*, an *LLM provider*, and (optionally) a *graph database server*
   for validation — are orthogonal. A schema mapping is deployment-invariant;
   an LLM provider is mapping-agnostic; a server is LLM-agnostic. The
   framework reflects this by giving each its own typed configuration
   class and its own YAML subdirectory (`config/mappings/`,
   `config/models/`, `config/servers/`), and by combining them through
   explicit CLI flags rather than a single conflated config blob. An
   earlier revision of the project used one Pydantic super-config
   (`AppConfig`) that fanned out all three concerns into one file, which
   required N × M × K example files to demonstrate the cross-product of
   options. The current layout demonstrates the cross-product at the
   directory level instead.

2. **Simplicity over framework ceremony.** The full generate–validate–fix
   loop is implemented as a single `while` loop in
   `src/rows2graph/translator.py`. Comparable LLM-feedback-loop projects
   in this space typically reach for LangGraph + LangChain + a tracing
   layer — easily ten heavy dependencies. For one retry edge a plain
   `while` loop with explicit state is shorter, easier to trace through,
   and has an order of magnitude less import surface.

3. **Extensibility via `Protocol`, not inheritance.** Python's
   `typing.Protocol` (PEP 544) gives structural subtyping without forcing
   implementations to import a base class. A future `GremlinTarget` or
   `SparqlValidator` can live in a separate package without any import
   coupling to `rows2graph` core.

4. **Strict typing end-to-end.** `mypy --strict` is enforced in
   `pyproject.toml`. Pydantic validates every external input (mapping,
   model config, server config) at the boundary; downstream code may
   assume validity. This is what makes the discriminated-union dispatch
   pattern (see below) sound: the literal field that selects the subclass
   is checked at load time, so subsequent `isinstance` branches are
   exhaustive without runtime defensive code.

---

## The orthogonality commitment

The clearest statement of the framework's central design choice is this:

> A schema mapping says *what your data looks like*. A model config says
> *who will translate it*. A server config says *which deployment will
> validate the result*. These three answers are independent of each
> other, and the framework keeps them in three separate places.

The library mirrors this commitment in code: there is no single
`AppConfig`-like object. The three concerns are loaded by three different
functions (`SchemaMapping.from_yaml`, `load_model_config`,
`load_server_config`), instantiate three different families of components
(`SchemaMapping`, `LLMClient`, `QueryValidator`), and meet only inside
`SQLTranslator.__init__`. Removing the super-config gives each piece
independent testability: a unit test can construct a `SQLTranslator`
against an in-memory fake LLM and a real syntax validator with no YAML
files involved (see `tests/test_static.py`'s `_FakeLLM`).

---

## Module responsibilities

| Module | Purpose | Key public API |
|---|---|---|
| `mapping.py` | Parse and validate the schema mapping YAML. | `SchemaMapping.from_yaml(path) -> SchemaMapping` |
| `state.py`   | Loop-internal state + public result. | `TranslationState`, `TranslationResult` |
| `events.py`  | Typed iteration events emitted by the loop. | `GeneratedEvent`, `ValidatedEvent`, `FixGeneratedEvent`, `MaxIterationsReachedEvent`, `CompletedEvent`, `TranslationEvent`, `EventHandler` |
| `prompts.py` | Assemble system/user/fix prompts. | `build_system_prompt`, `build_generate_prompt`, `build_fix_prompt`, `format_schema_context` |
| `sql_features.py` | Parse SQL with sqlglot and detect which operation clusters it uses. | `SqlFeature`, `ALL_FEATURES`, `detect_features` |
| `_env.py` | YAML env-var interpolation helper. | `interpolate_env` (internal) |
| `llm/__init__.py` | LLM Protocols + discriminated-union config. | `LLMClient`, `AsyncLLMClient`, `StreamCallback`, `ModelConfig`, `load_model_config`, `make_llm`, `make_async_llm` |
| `llm/ollama.py`   | Wrap `ollama.Client` and `ollama.AsyncClient`. | `OllamaConfig`, `OllamaLLMClient`, `AsyncOllamaLLMClient` |
| `llm/anthropic.py`| Wrap `anthropic.Anthropic` and `anthropic.AsyncAnthropic` (direct API). | `AnthropicConfig`, `AnthropicLLMClient`, `AsyncAnthropicLLMClient` |
| `targets/__init__.py` | Target-language Protocol + factory. | `TargetLanguage`, `make_target` |
| `targets/cypher.py`   | Cypher prompt + extractor. | `CypherTarget` |
| `targets/aql.py`      | AQL prompt + extractor. | `AqlTarget(graph_name)` |
| `validators/__init__.py` | Validator Protocols + discriminated-union config. | `QueryValidator`, `AsyncQueryValidator`, `ServerConfig`, `load_server_config`, `make_validator`, `make_async_validator` |
| `validators/noop.py` | Pass-through (sync + async). | `NoopValidator`, `AsyncNoopValidator` |
| `validators/cypher/syntax.py` | Regex-based Cypher validation (sync + async). | `CypherSyntaxValidator`, `AsyncCypherSyntaxValidator` |
| `validators/cypher/server.py` | Neo4j `EXPLAIN` validation (sync + async) + `Neo4jConfig`. | `Neo4jConfig`, `CypherServerValidator`, `AsyncCypherServerValidator` |
| `validators/aql/syntax.py` | Regex-based AQL validation (sync + async). | `AqlSyntaxValidator`, `AsyncAqlSyntaxValidator` |
| `validators/aql/server.py` | ArangoDB `db.aql.validate` validation (sync + async) + `ArangoDBConfig`. | `ArangoDBConfig`, `AqlServerValidator`, `AsyncAqlServerValidator` |
| `translator.py` | Orchestrate the loop (sync). | `SQLTranslator(...)` |
| `async_translator.py` | Async sibling of `translator.py`. | `AsyncSQLTranslator(...)` |

---

## State lifecycle

All state lives in `TranslationState` (`state.py`). Here is how fields
mutate through a run.

### Successful run (no fixes needed)

| Step | `messages` | `generated_query` | `validation_iteration` | `validation_passed` | `final_status` |
|---|---|---|---|---|---|
| 0. Init                       | `[]`                  | `None`        | `0` | `False` | `"pending"` |
| 1. After system prompt        | `[sys]`               | `None`        | `0` | `False` | `"pending"` |
| 2. After user prompt          | `[sys, user]`         | `None`        | `0` | `False` | `"pending"` |
| 3. After LLM call + extract   | `[sys, user, asst]`   | `"MATCH ..."` | `0` | `False` | `"pending"` |
| 4. Iter 1: validate OK        | `[sys, user, asst]`   | `"MATCH ..."` | `1` | `True`  | `"success"` |

### Failed run (reaches max iterations)

With `max_iterations = 3`:

| Step | `messages` length | `validation_iteration` | `validation_errors` | `final_status` |
|---|---|---|---|---|
| Iter 1 validate: fails | 3 | 1 | `[err_a]` | `"pending"` |
| Iter 1 fix + regen     | 5 | 1 | `[err_a]` | `"pending"` |
| Iter 2 validate: fails | 5 | 2 | `[err_b]` | `"pending"` |
| Iter 2 fix + regen     | 7 | 2 | `[err_b]` | `"pending"` |
| Iter 3 validate: fails | 7 | 3 | `[err_c]` | `"max_iterations_reached"` |

The loop uses one iteration *slot* per `validate` call, so N iterations
correspond to N `validate` calls and (N − 1) fix attempts. The final
iteration does not get a fix pass because there is no point retrying
without validating it again.

---

## Async path

`AsyncSQLTranslator` lives next to `SQLTranslator` in
`src/rows2graph/async_translator.py` and exposes the same surface — same
constructor parameters, same `translate()` return type, same iteration
semantics — with `await` at the LLM and validator call sites. Both
translators co-exist; the sync path stays the default for scripts,
notebooks, and the CLI.

**Why bother, given that the loop is sequential per translation?** A
single translation will not finish faster under async — each iteration's
`chat → validate → fix` chain genuinely depends on the previous result.
The wins are elsewhere:

- **UI integration.** Streamlit and other event-loop-based frontends
  cannot show progress while a blocking LLM call holds the event loop.
  An async translator yields between calls, so the UI thread keeps
  painting.
- **Concurrent translations.** A service that fields several translations
  in parallel costs one event-loop task per translation, not one thread.
- **Streaming.** Token-by-token output requires an async generator
  somewhere; the async path makes that a small extension (see "Streaming"
  below) rather than a rewrite.
- **Cancellation.** `asyncio.CancelledError` propagates cleanly through
  `await self._llm.chat(...)` if the caller decides to abandon a
  translation.

**Structural mirroring, not divergence.** The async translator's
`translate()` body is a copy of the sync one with `await` added at the
right places. The events emitted, the iteration numbering, the
`TranslationResult` produced, and even the log lines are identical. Tests
in `tests/test_static.py` assert event-sequence parity between the two
paths against the same fake LLM.

**Shared helpers prevent silent divergence in the interesting logic.**
`llm/anthropic.py` factors `_build_anthropic_kwargs`, `_log_anthropic_usage`,
and `_extract_anthropic_text` out of both sync and async `chat()`
implementations, so prompt caching, usage logging, and response extraction
have one definition each. The translator loop body is the one place that
genuinely duplicates — kept structurally identical, watched by parity
tests.

**Streaming.** When `AsyncLLMClient.chat(messages, stream_to=cb)` receives
a non-None callback, the implementation switches to the SDK's streaming
endpoint (`anthropic.messages.stream(...)` or
`ollama.AsyncClient.chat(..., stream=True)`) and calls `cb(delta)` for
every text chunk while still returning the assembled string at the end.
`AsyncSQLTranslator.translate(sql, stream_to=...)` forwards the callback
to each LLM call in the loop. Ollama deliberately falls back to
non-streaming on retries so the caller's UI buffer does not get
contaminated by deltas from a discarded prior attempt.

---

## Iteration events

The loop emits a typed event at every milestone. The contract is in
`src/rows2graph/events.py`:

```python
@dataclass(frozen=True)
class GeneratedEvent:           # initial LLM call finished
    iteration: int              # always 1
    query: str

@dataclass(frozen=True)
class ValidatedEvent:           # one validate() call finished
    iteration: int
    query: str
    errors: list[str]
    passed: bool

@dataclass(frozen=True)
class FixGeneratedEvent:        # a fix LLM call finished
    iteration: int              # the iteration that just failed
    query: str                  # candidate for iteration N+1

@dataclass(frozen=True)
class MaxIterationsReachedEvent:
    iteration: int
    errors: list[str]

@dataclass(frozen=True)
class CompletedEvent:           # always emitted last
    result: TranslationResult

TranslationEvent = (
    GeneratedEvent | ValidatedEvent | FixGeneratedEvent
    | MaxIterationsReachedEvent | CompletedEvent
)
EventHandler = Callable[[TranslationEvent], None]
```

Both `SQLTranslator.translate` and `AsyncSQLTranslator.translate` accept
an optional `on_event: EventHandler | None = None` parameter. Iteration
numbering matches the existing log lines exactly: `iteration=N` always
refers to validation pass N, so `FixGeneratedEvent.iteration=N` means
"the fix produced after iteration N failed; this candidate will be
validated as iteration N+1". This is the convention to internalise once
when consuming events.

**Handler exceptions are isolated.** Both translators wrap each handler
invocation in `_emit(handler, event)` (defined at the bottom of
`translator.py` / `async_translator.py`), which catches all exceptions,
logs them at WARNING, and returns. A misbehaving handler cannot abort a
user's translation. The trade-off is that handler bugs surface in logs
rather than as direct exceptions — acceptable for an observer pattern.

**Why typed events instead of subscribing to logger output?** The Streamlit
UI's earlier revision parsed library log strings ("Initial query
generated", "Validation passed on iteration N", etc.) to reconstruct
iteration progress for its timeline. That contract was brittle: a tweak
to a log format would silently break the UI's progress display. Typed
events make the contract explicit, statically checked, and trivially
testable with `events.append` as a handler.

The existing log lines stay emitted unchanged so any other code that
already depended on them continues to work; events are an additive
overlay.

---

## Prompt strategy

Three distinct prompts, built by separate functions in `prompts.py`:

1. **System prompt** (`build_system_prompt`). Establishes the LLM's role,
   embeds the full schema mapping as structured text via
   `format_schema_context`, enumerates translation rules, and constrains
   output format ("ONLY valid query code, no markdown, no explanations").
2. **Generate prompt** (`build_generate_prompt`). Short user-turn:
   "Translate this SQL query: ...". The schema and rules already live in
   the system prompt; this turn carries only the per-call input.
3. **Fix prompt** (`build_fix_prompt`). Appended only after a validation
   failure. Contains the original SQL, the failing query, and the
   bulleted error list. Instructs the model to fix *only* those errors
   (without this constraint, low-temperature models tend to restructure
   the entire query on each retry).

### Why three prompts, not one

The system prompt should stay constant so the LLM has a stable "mental
model" of the schema; the fix prompt needs fresh per-iteration error
context. Keeping them separate prevents accidental context drift.
Conversation history accumulates: by the second fix iteration the
`messages` list contains `[sys, user, asst_1, fix_1, asst_2]`. The LLM
sees every prior attempt and every prior error in a single chat context.
If we built one giant prompt per call instead, we would lose that
accumulated context.

The three prompt builders are plain function calls that append to a
shared `messages` list — no graph nodes, no middleware, no separate
agents.

### Anthropic prompt caching

The Anthropic backends — both `AnthropicLLMClient` and
`AsyncAnthropicLLMClient` — emit the assembled system block with
`cache_control: {"type": "ephemeral"}` set. The schema + rules block is
identical across all iterations of a single translation, so iterations 2+
read it from Anthropic's prompt cache instead of paying for it as input
tokens. With a 1024-token minimum (2048 on Haiku) and a 5-minute TTL, a
3-iteration multi-iteration translation typically lands ~90% of its
system-prompt cost on cache reads.

Cache hit/write counts surface in the per-call `Anthropic call:` INFO log
line so consumers can observe cache effectiveness directly (see
`_log_anthropic_usage` in `src/rows2graph/llm/anthropic.py`). The
single-shot first call necessarily writes; every subsequent retry within
the TTL reads.

---

## Per-query prompt assembly

The system prompt is rebuilt for every translation, not cached once per
translator. The motivation is empirical: small models lose accuracy when
the system prompt contains rules irrelevant to the input — for example,
Cypher's 14-line `LIKE`/`ILIKE` mapping table on a query with no string
predicates is pure noise. The framework strips those rules out by
detecting which SQL operation clusters the input uses and emitting only
the corresponding rule chunks.

### The detector

`detect_features` (in `src/rows2graph/sql_features.py`) calls
`sqlglot.parse_one`, walks the resulting AST with `find_all`, and returns
a `frozenset[SqlFeature]` naming the clusters present. The enum has ten
members:

| `SqlFeature` | sqlglot nodes that light it up |
|---|---|
| `LIKE` | `exp.Like`, `exp.ILike` |
| `JOIN` | `exp.Join` |
| `AGGREGATION` | `exp.Group`, `exp.Having`, `exp.AggFunc` |
| `ORDER_LIMIT` | `exp.Order`, `exp.Limit`, `exp.Offset` |
| `CTE` | `exp.CTE` |
| `UNION` | `exp.Union`, `exp.Intersect`, `exp.Except` |
| `WINDOW` | `exp.Window` |
| `CASE` | `exp.Case` |
| `SUBQUERY` | `exp.Subquery`, `exp.Exists` (CTEs are excluded — they get their own bucket) |
| `DISTINCT` | `exp.Distinct` |

On any `sqlglot.errors.ParseError` the function returns `ALL_FEATURES`,
which restores the pre-refactor "ship every rule" behaviour. That
fail-open is load-bearing: a silently-stripped rule would be a
translation-quality regression, while a few extra tokens on an
unparseable query is harmless.

### How the rules are gated

The feature set flows through two layers:

1. **Generic rules in `prompts.py`.** `_GENERIC_FEATURE_RULES` (a small
   `dict[SqlFeature, str]` near the top of the module) holds the
   one-line, target-agnostic rules — currently `JOIN` and `AGGREGATION`.
   `build_system_prompt` iterates `SqlFeature` in declaration order and
   emits each line only if its feature is in the detected set.
2. **Target-specific rule chunks.**
   `TargetLanguage.system_prompt_section` now takes the feature set as
   an argument. `CypherTarget`, `AqlTarget`, and `GremlinTarget` each
   keep a private `_FEATURE_RULES: dict[SqlFeature, str]` mapping (see
   `targets/cypher.py`, `targets/aql.py`, `targets/gremlin.py`) holding
   the multi-line rule chunks per operation, and append only the chunks
   for features present. The always-on base block (`MATCH`/`CREATE`/
   keyword list for Cypher, the `FOR ... GRAPH` traversal idiom for AQL,
   the `g.V()` / `.hasLabel(...)` / `.out(...)` traversal idiom for
   Gremlin) is emitted unconditionally.

### Trade-off

The mechanism requires the AST detectors and the per-target rule chunks
to stay in sync. Adding a rule chunk without a matching detector means
the chunk never fires; adding a detector without a chunk is harmless but
pointless. Whenever a new operation cluster is supported, the change is
three-touch: a `SqlFeature` enum member, a detector branch in
`detect_features`, and one entry in each target's `_FEATURE_RULES` dict.

---

## Discriminated-union configs

Both model configs and server configs form Pydantic discriminated unions:

```python
ModelConfig  = Annotated[OllamaConfig  | AnthropicConfig, Field(discriminator="provider")]
ServerConfig = Annotated[Neo4jConfig   | ArangoDBConfig,  Field(discriminator="type")]
```

A YAML file with `provider: "ollama"` deserialises to `OllamaConfig`; one
with `provider: "anthropic"` to `AnthropicConfig`. The loader functions
(`load_model_config`, `load_server_config`) return the precise subtype, so
the downstream factories (`make_llm`, `make_validator`) dispatch via a
single `isinstance` check — the same factory-by-tag pattern as the
original design, but with the tag validated by Pydantic at load time
rather than carried in a separate field of a larger config blob.

This pattern is what lets the demo CLI accept arbitrary `--model PATH`
without needing to know in advance whether the path points to an Ollama
or Anthropic config: it loads the file once, lets Pydantic pick the right
subclass, and the rest of the program is statically typed against the
union.

Both `AnthropicConfig` and `OllamaConfig` expose a `max_retries: int = 3`
field (validated `>= 0`). `AnthropicConfig` forwards it to the SDK's
built-in retry layer, which already does exponential backoff with jitter
on 408/409/429/5xx and connection errors. `OllamaConfig` is consumed by a
handwritten exponential-backoff loop inside
`OllamaLLMClient.chat()` / `AsyncOllamaLLMClient.chat()` — the `ollama`
SDK has no native retry — that retries on `RequestError` (connection
issues) and `ResponseError` with `status_code >= 500`, sleeping 1s, 2s,
4s, …. 4xx responses propagate immediately on the assumption that they
signal client-side mistakes a retry cannot fix (unknown model, malformed
request).

---

## Protocol-typed extension points

Three Protocols define the extension surface:

```python
class LLMClient(Protocol):
    def chat(self, messages: list[dict[str, Any]]) -> str: ...
    def close(self) -> None: ...

class TargetLanguage(Protocol):
    @property
    def name(self) -> str: ...
    def system_prompt_section(self) -> str: ...
    def extract_query(self, llm_response: str) -> str: ...

class QueryValidator(Protocol):
    def validate(self, query: str) -> list[str]: ...
    def close(self) -> None: ...
```

### Why `Protocol`, not `ABC`?

* **Zero coupling.** Implementations do not need to `import` anything
  from `rows2graph`. A third-party Gremlin validator in a separate pip
  package can satisfy the protocol without touching `rows2graph`
  internals.
* **Duck-typed, mypy-verified.** `mypy --strict` checks that returned
  instances match the protocol shape.
* **No diamond-inheritance risk** if a future implementation needs to
  compose with another base class (caching adapter, metrics decorator).

An ABC would work but introduces a required import dependency for every
implementation.

---

## Why direct SDKs, not LangChain

* **Fewer dependencies.** `ollama` is a thin HTTP wrapper; `anthropic` is
  a focused Anthropic API client. LangChain pulls in hundreds of packages
  transitively.
* **The sync loop is easy to read and modify.** It is one screen of
  Python in `translator.py` with no framework machinery hiding control
  flow. The async path (`async_translator.py`) is a structural mirror,
  not a separate framework — `await` at the same call sites, same
  prompts, same events, same `TranslationResult`. The trade-off is two
  near-identical loop bodies; the upside is that neither path imports
  any orchestration layer.
* **No framework lock-in.** Adding a new provider is a ~30-line class
  against the `LLMClient` (or `AsyncLLMClient`) protocol, not a rewiring
  of a chain-of-runnables.
* **Easier to test.** Mocking `ollama.Client` or `anthropic.Anthropic` is
  trivial; mocking LangChain's full ecosystem is not. The
  `_FakeLLM` / `_FakeAsyncLLM` doubles in `tests/test_static.py` are a
  dozen lines each.

The trade-off: if the project grows to need tool calling, multi-agent
orchestration, or persistent checkpointing, a framework like LangGraph
would start paying for itself. Streaming and async are already covered
by the existing protocols, so those alone do not justify the framework
overhead.

---

## Known limitations

* **`TranslationState.target_language` is a `Literal["cypher", "aql", "gremlin"]`.**
  Adding a further target language requires widening this literal (and
  the analogous `Literal` in `TranslationResult`). The `TargetLanguage`
  Protocol itself is extensible; the literal is a separate, narrower
  declaration that exists to keep typed access to `state.target_language`
  precise through the loop. This is a deliberate trade-off between
  plugin-extensibility and end-to-end type precision.
* **`TargetLanguage.system_prompt_section()` is schema-blind.** It does
  not see the schema mapping. A future target requiring schema-aware
  prompt-section generation (for example, AQL prompts that enumerate
  vertex-collection names explicitly) would need to widen the Protocol
  to accept the schema as a parameter.
* **Sync and async translator bodies duplicate the loop.**
  `SQLTranslator.translate` and `AsyncSQLTranslator.translate` are kept
  structurally identical by hand. Logic that lives entirely *inside*
  either method (state transitions, event-emission ordering, iteration
  numbering) has no shared definition. The mitigations are (a) shared
  helpers for the interesting non-loop logic (Anthropic kwargs builder,
  usage logger, response extractor), and (b) explicit parity tests in
  `tests/test_static.py` that drive both translators with the same fake
  LLM and assert identical event sequences. If a future change
  introduces a real algorithmic divergence between the two, this would
  be the place to factor a shared helper out — but trying to share the
  loop bodies themselves would force one path to compromise (probably
  the sync one — a sync function that wraps an async coroutine via
  `asyncio.run` is awkward and erases the sync path's "no event loop"
  property).
* **Streaming is async-only.** The `stream_to` callback is on
  `AsyncLLMClient.chat`, not `LLMClient.chat`. A sync streaming variant
  would block while consuming the stream, which defeats the point of
  streaming (live UI feedback); the sync path is for callers that just
  want a final string back.

---

## Generalising the pattern

The structural idea behind `rows2graph` is independent of both SQL and
graph databases: *generate an artifact with an LLM, validate it with a
deterministic compiler-like tool, then retry with the validator's errors
as additional context*. Any domain that pairs an LLM-generatable
artifact with a fast, programmatic validator fits the same shape — SQL
migrations checked by a planner, configuration files checked by a schema
validator, regex patterns checked against a sample corpus, infra
manifests checked by `terraform validate`, security rules checked by
their respective compilers.

What this project demonstrates is that the pattern does not require a
graph-orchestration runtime to express. The loop is a `while` block, the
state is one Pydantic model, the extension points are three Protocols,
and the per-query prompt is rebuilt from feature-gated chunks. A reader
adapting the pattern to a new domain mostly replaces the schema mapping,
the target language module, and the validator — the orchestrator stays
unchanged.
