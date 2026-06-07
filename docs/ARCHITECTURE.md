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
   `src/rows2graph/translator.py`. The reference project (yara-copilot,
   which inspired the loop pattern) used LangGraph, LangChain, and
   Langfuse — roughly ten heavy dependencies. For one retry edge a plain
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
| `prompts.py` | Assemble system/user/fix prompts. | `build_system_prompt`, `build_generate_prompt`, `build_fix_prompt`, `format_schema_context` |
| `_env.py` | YAML env-var interpolation helper. | `interpolate_env` (internal) |
| `llm/__init__.py` | LLM Protocol + discriminated-union config. | `LLMClient`, `ModelConfig`, `load_model_config`, `make_llm` |
| `llm/ollama.py`   | Wrap `ollama.Client`. | `OllamaConfig`, `OllamaLLMClient` |
| `llm/anthropic.py`| Wrap `anthropic.Anthropic` (direct API). | `AnthropicConfig`, `AnthropicLLMClient` |
| `targets/__init__.py` | Target-language Protocol + factory. | `TargetLanguage`, `make_target` |
| `targets/cypher.py`   | Cypher prompt + extractor. | `CypherTarget` |
| `targets/aql.py`      | AQL prompt + extractor. | `AqlTarget(graph_name)` |
| `validators/__init__.py` | Validator Protocol + discriminated-union config. | `QueryValidator`, `ServerConfig`, `load_server_config`, `make_validator` |
| `validators/noop.py` | Pass-through. | `NoopValidator` |
| `validators/cypher/syntax.py` | Regex-based Cypher validation. | `CypherSyntaxValidator` |
| `validators/cypher/server.py` | Neo4j `EXPLAIN` validation + `Neo4jConfig`. | `Neo4jConfig`, `CypherServerValidator` |
| `validators/aql/syntax.py` | Regex-based AQL validation. | `AqlSyntaxValidator` |
| `validators/aql/server.py` | ArangoDB `db.aql.validate` validation + `ArangoDBConfig`. | `ArangoDBConfig`, `AqlServerValidator` |
| `translator.py` | Orchestrate the loop. | `SQLTranslator(...)` |

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

This mirrors yara-copilot's separation of `create_rule_agent` and
`fix_rule_node` graph nodes — implemented here as plain function calls
that append to a shared list.

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
* **Synchronous flow is easier to reason about.** The
  generate–validate–fix loop is inherently sequential — no parallelism,
  no streaming, no complex I/O scheduling. Async adds complexity without
  benefit here.
* **No framework lock-in.** Adding a new provider is a ~30-line class
  against the `LLMClient` protocol, not a rewiring of a chain-of-runnables.
* **Easier to test.** Mocking `ollama.Client` or `anthropic.Anthropic` is
  trivial; mocking LangChain's full ecosystem is not.

The trade-off: if the project grows to need tool calling, multi-agent
orchestration, persistent checkpointing, or streaming UI, a framework
like LangGraph would start paying for itself. None of those are needed
here.

---

## Known limitations

* **`TranslationState.target_language` is a `Literal["cypher", "aql"]`.**
  Adding a third target language requires widening this literal (and the
  analogous `Literal` in `TranslationResult`). The `TargetLanguage`
  Protocol itself is extensible; the literal is a separate, narrower
  declaration that exists to keep typed access to `state.target_language`
  precise through the loop. This is a deliberate trade-off between
  plugin-extensibility and end-to-end type precision.
* **`TargetLanguage.system_prompt_section()` is schema-blind.** It does
  not see the schema mapping. A future target requiring schema-aware
  prompt-section generation (for example, AQL prompts that enumerate
  vertex-collection names explicitly) would need to widen the Protocol
  to accept the schema as a parameter.
* **No streaming output.** The translator waits for the full LLM
  response before extracting a query. If translation latency becomes a
  user-facing concern, a `chat_stream` method on `LLMClient` could
  surface tokens as they arrive, at the cost of an incremental
  `extract_query` implementation per target language.

---

## Comparison to yara-copilot

The reference project implements essentially the same pattern with much
more machinery. This table maps yara-copilot concepts to `rows2graph`
equivalents.

| yara-copilot concept | `rows2graph` equivalent | Notes |
|---|---|---|
| LangGraph `StateGraph` with nodes + edges | `SQLTranslator.translate()` with a `while` loop | Linear flow with one retry edge does not need a graph executor. |
| `yr_dump_agent` node | Initial-generation block of `translator.py` | Same responsibility: build prompt, call LLM, extract output. |
| `validate_rule_node` | `validator.validate()` call inside the loop | Protocol-dispatched instead of being a graph node. |
| `fix_rule_node` | Fix block of `translator.py` | Rebuilds the fix prompt with accumulated error context. |
| `should_fix_or_end` conditional edge | `while` condition + early `break` | Plain Python control flow. |
| `StateGlobal` + mixins | Single `TranslationState` Pydantic model | The state is small enough to live in one class. |
| `@dynamic_prompt` middleware | Schema passed as an argument to `build_system_prompt` | The schema is static per translator instance; no middleware needed. |
| `RuleChecker` from `yrtc` | `CypherServerValidator` / `AqlServerValidator` | Equivalent role: compile-check without executing. |
| Langfuse tracing | Standard `logging` module | Lighter-weight observability. |
| MCP knowledge base + `read_kb_resource` | Schema mapping baked into system prompt | Small fixed schema — no need for retrieval at runtime. |
| FastAPI + Uvicorn server | Demo CLI (`demo/cli.py`) | The framework is a library + reference demo, not a web service. |

The shared structural idea: *"generate, then validate with a
compiler-like tool, then retry with the errors as context"*. This pattern
generalises beyond YARA rules and graph queries — to SQL migrations,
config files, regex patterns, or any domain where a deterministic
validator exists alongside an LLM.
