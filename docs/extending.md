# Extending sql2graph

**How to add a new target graph language or a new LLM provider - the full
checklist, in dependency order.**

`sql2graph` is built around three structural `Protocol`s (`TargetLanguage`,
`LLMClient`, `QueryValidator`), so most extensions need no changes to the core
loop. This guide walks the two most common extensions end to end.

## Scope

This page owns: the step-by-step recipes for adding a target language or an
LLM provider, including which steps are test-enforced. Related topics live
with their owners:

- [architecture.md](architecture.md#protocol-typed-extension-points): the
  design rationale behind the Protocols.
- [api.md](api.md): the public signatures each Protocol requires.
- [validation/syntax.md](validation/syntax.md): the reproducible recipe for a
  new grammar-based validator, referenced by the target-language checklist.

---

## Add a new target language

Say you want to add SPARQL. Work through these steps; the ones marked
**(enforced)** have a test that fails until you do them.

### 1. Implement the `TargetLanguage`

Create `src/sql2graph/targets/sparql.py` with a class satisfying the Protocol
(`src/sql2graph/targets/__init__.py`):

```python
class TargetLanguage(Protocol):
    @property
    def name(self) -> str: ...
    def system_prompt_section(self, features: frozenset[SqlFeature]) -> str: ...
    def extract_query(self, llm_response: str) -> str: ...
    def repair_hint(self, errors: list[str]) -> str | None: ...
```

Reuse the shared scaffolding in `src/sql2graph/targets/_rules.py`
(`BaseRules`, `FeatureRule`, `compose_section`, `extract_query`) exactly as
`CypherTarget`, `AqlTarget`, and `GremlinTarget` do - it gives you the uniform
five-section base block and a query extractor for free. Return `None` from
`repair_hint` unless a validator error class needs a restructure the terse
message misdirects the model away from (see `AqlTarget` for the one live use).

### 2. Provide a feature rule for *every* `SqlFeature` **(enforced)**

Each target keeps a private `_FEATURE_RULES: dict[SqlFeature, FeatureRule]`
that must be **total** over the 13-member `SqlFeature` enum
(`src/sql2graph/sql_features.py`). A parametrized parity test under
`tests/unit/targets/` fails if any target drops one. Where a feature is a
near-no-op in your language, write a chunk that says so explicitly rather than
omitting it - that keeps "deliberately not applicable" distinct from
"accidentally forgotten".

### 3. Register it in the factory

In `src/sql2graph/targets/__init__.py`, add `"sparql"` to `VALID_TARGETS` and a
branch to `make_target`.

### 4. Widen the target `Literal`

Add `"sparql"` to the `Literal["cypher", "aql", "gremlin"]` on
`TranslationState.target_language` **and** `TranslationResult.target_language`
(`src/sql2graph/engine/state.py`). This is the one non-Protocol-friendly step - a
deliberate trade-off documented under "Known limitations" in
[architecture.md](architecture.md#known-limitations).

### 5. Add a validator

A target needs at least one validation mode beyond `none`. Follow the
reproducible recipe in
[validation/syntax.md](validation/syntax.md#5-implementation-steps-reproducible-recipe):

- **Syntax (deployment-free):** vendor the engine's ANTLR grammar under
  `src/sql2graph/validators/_grammar/sources/` (record provenance in that directory's
  README), regenerate parsers with `scripts/generate_parsers.sh`, then write
  `validators/sparql/syntax.py` delegating to `parse_errors` with an
  EOF-anchored start rule. Add sync and async classes.
- **Server:** write `validators/sparql/server.py` plus a Pydantic config with a
  literal `type` discriminator; add it to the `ServerConfig` union and to
  `TARGET_SERVER_TYPE` (`src/sql2graph/validators/__init__.py`), wire branches
  in `make_validator` / `make_async_validator`, and add a `provision/` entry if
  you want auto-provisioned `managed` mode.

Then extend `valid_modes_for_target` so the new target reports the modes it
actually supports.

### 6. Export and test

Export the new public classes from `src/sql2graph/__init__.py` (`__all__`).
Add feature/extraction tests under `tests/unit/targets/` and validator tests
under `tests/unit/validators/`. Run `uv run mypy src/` and `uv run pytest`.

---

## Add a new LLM provider

The provider abstraction is a discriminated-union config plus sync/async client
classes. Using `llm/ollama.py` and `llm/anthropic.py` as templates:

### 1. Implement the clients

Satisfy `LLMClient` and `AsyncLLMClient` (`src/sql2graph/llm/__init__.py`):

```python
def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> ChatReply: ...
def close(self) -> None: ...
```

The async client's `chat` additionally takes `stream_to: StreamCallback | None`
and must invoke it per text delta while still returning the assembled result.
Both return a `ChatReply` - the assistant text plus the `TokenUsage` the call
cost (`src/sql2graph/llm/usage.py`). Populate `input_tokens` / `output_tokens`;
set the two cache fields only if your provider has a prompt cache (else leave
them `0`, as Ollama does).

### 2. Add the config to the tagged union

Write a Pydantic config carrying a literal `provider` discriminator (e.g.
`provider: Literal["vllm"]`), add it to the `ModelConfig` union, and add the
name to `VALID_PROVIDERS`.

### 3. Register in the factories

Add `isinstance` branches to `make_llm` and `make_async_llm`.

### 4. Add pricing (for the eval harness)

If you will evaluate the provider, add its per-token rates to
`eval/harness/pricing.py`. A test in `tests/eval/` enforces that every model in
the run matrix has rates, so this is not optional once the model is in
`RUN_MATRIX`.

### 5. Export and test

Export the public classes from `src/sql2graph/__init__.py`. Add client tests
under `tests/unit/llm/` (mock the provider SDK). Run the check suite.

---

Adding either extension touches no code inside the translator loop
(`engine/translator.py` / `engine/async_translator.py`): the loop consumes the Protocols, so a
conforming implementation drops in. The only exceptions are the two `Literal`
widenings in step 4 of the target walkthrough, which exist to keep typed access
to `state.target_language` precise.
