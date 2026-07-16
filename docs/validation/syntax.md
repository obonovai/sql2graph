# Syntax validation

**How deployment-free, grammar-based query validation works, why it replaced
the original regex checks, and how to maintain it (including regenerating the
parsers).**

## Scope

This page owns: the internals of the grammar-based syntax tier; grammar
provenance per language; the parser regeneration recipe. Related topics live
with their owners:

- [modes.md](modes.md): choosing between the validation modes; this page
  covers only the `syntax` tier.
- [api.md](../api.md#validator-components): the public validator surface.
- [extending.md](../extending.md): where a new grammar-based validator fits
  in a new target language.
- [validators/_grammar/sources/README.md](../../src/sql2graph/validators/_grammar/sources/README.md):
  the vendored grammar files and their licenses.

## 1. Overview

`sql2graph` translates SQL into a graph query with an LLM, then runs a
generate-validate-fix loop: a validator inspects the generated query and returns
a list of error strings (empty means valid); any errors are fed back to the LLM
to repair the query. Choosing between the validation modes (`none`, `syntax`,
`server`, and the derived managed variant) is covered in [modes.md](modes.md).

The `syntax` tier is the **deployment-free** one: no database, no Docker,
in-process, milliseconds. It exists so the framework can run end-to-end in CI or
on a reviewer's laptop without provisioning a database. This document covers the
grammar-based implementation of that tier.

### Where it fits in the pipeline

```
SQL + mapping
   |
   v
pre-flight gate (input side, before the LLM): mapping validity + SQL parse +
   unmapped tables/columns. Rejects bad input so the LLM is never asked to
   translate against a broken schema. (See engine/preflight.py and mapping.py.)
   |
   v
LLM generates a candidate query
   |
   v
validator.validate(query)  <-- syntax (this doc) | server | none
   |
   +-- errors? --> build_fix_prompt(errors) --> LLM regenerates (loop)
   |
   +-- clean   --> return the query
```

Syntax validation is the **output side**: it checks the query the LLM produced.
The input-side mapping/SQL gate is separate (see `engine/preflight.py` and the
`SchemaMapping` validation in `mapping.py`).

## 2. Why grammar-based instead of regex

The original `syntax` validators were pure regex: they checked for an empty
query, a whitelisted start keyword, balanced bracket *counts*, and a couple of
ad-hoc rules ("a MATCH must have a RETURN"). That is too weak for the real
failure mode: the LLM emits queries that use the right features but are still
malformed. Regex could not see:

- **Clause ordering** (e.g. a clause in a position the grammar forbids).
- **Malformed patterns / traversal steps** (a half-written node pattern, an
  unterminated Gremlin step, a trailing `.`).
- **Bad expression or function-call syntax.**

It also produced **false positives**: counting `(` characters flags a perfectly
valid query that contains a bracket inside a string literal, for example
`MATCH (n {name: 'a)b'}) RETURN n`.

The fix is to parse each candidate with the **graph engine's own grammar**, so
"valid" means "the engine's parser accepts it". A real parser also yields rich
`line:col` messages, which are far better repair signal for the fix loop than
"Unbalanced parentheses".

## 3. Per-language decision

| Target  | Engine            | Syntax-tier grammar                          |
| ------- | ----------------- | -------------------------------------------- |
| Cypher  | Neo4j             | Neo4j's own `Cypher25` ANTLR grammar         |
| Gremlin | Apache TinkerPop  | TinkerPop's own `Gremlin.g4` ANTLR grammar   |
| AQL     | ArangoDB          | hand-port of ArangoDB's Flex+Bison grammar   |

Cypher and Gremlin use each engine's **own** published grammar, so they are
authoritative by construction. AQL is different: ArangoDB publishes no reusable
offline grammar (its parser is hand-written C++/Flex+Bison), so the AQL syntax
tier uses a **hand-port** of that parser (`_grammar/sources/AQL{Lexer,Parser}.g4`,
pinned to the `3.11` branch). It reproduces the grammar structure for
recognition only and is *best-effort*: it may diverge slightly from ArangoDB's
real parser (ANTLR has no `%nonassoc`, so a few non-associative chains are
over-accepted). The `server` / `managed` validator remains authoritative; the
offline check is a fast first pass. The per-target rule is centralised in
`valid_modes_for_target`:

```python
from sql2graph import valid_modes_for_target

valid_modes_for_target("cypher")   # ("none", "syntax", "server")
valid_modes_for_target("gremlin")  # ("none", "syntax", "server")
valid_modes_for_target("aql")      # ("none", "syntax", "server")
```

## 4. Architecture and layout

### The validator contract

Every validator is a structural `Protocol` (no base class to inherit), defined
in `src/sql2graph/validators/__init__.py`:

```python
class QueryValidator(Protocol):
    def validate(self, query: str) -> list[str]: ...
    def close(self) -> None: ...
```

There is an async sibling `AsyncQueryValidator` with the same shape and
`async` methods. Validators are built by a factory:

```python
make_validator(target, mode, *, server_config=None) -> QueryValidator
make_async_validator(target, mode, *, server_config=None) -> AsyncQueryValidator
```

`mode="syntax"` returns `CypherSyntaxValidator` / `GremlinSyntaxValidator` /
`AqlSyntaxValidator` (or their async siblings).

### File layout

```
src/sql2graph/validators/
  __init__.py            # QueryValidator protocol, factory, valid_modes_for_target
  _grammar/
    sources/             # ANTLR grammars (the source of truth)
      Cypher25Lexer.g4
      Cypher25Parser.g4
      Gremlin.g4
      AQLLexer.g4
      AQLParser.g4
      README.md          # provenance: upstream source, version, license, edits
    runtime.py           # shared parse routine + error listener
    generated/           # committed, machine-generated parsers (do not hand-edit)
      cypher/  Cypher25Lexer.py, Cypher25Parser.py
      gremlin/ GremlinLexer.py, GremlinParser.py
      aql/     AQLLexer.py, AQLParser.py
  cypher/syntax.py       # CypherSyntaxValidator (+ async)
  gremlin/syntax.py      # GremlinSyntaxValidator (+ async)
  aql/syntax.py          # AqlSyntaxValidator (+ async)
  cypher/server.py, aql/server.py, gremlin/server.py   # the server tier
```

The `.g4` files under `_grammar/sources/` are the source of truth; the Python parsers
under `_grammar/generated/` are produced from them by
`scripts/generate_parsers.sh` and committed so that end users and CI need only
the pure-Python `antlr4-python3-runtime` (no Java, no codegen).

### The shared parse routine

All three syntax validators delegate to one function in `_grammar/runtime.py`. It
attaches a listener that **collects** ANTLR's syntax errors (instead of printing
them) as `line:col` strings, runs the grammar's start rule, and returns the
(capped) list:

```python
_MAX_ERRORS = 5  # ANTLR error recovery can cascade; the first few are the useful ones


class _CollectingErrorListener(ErrorListener):
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e) -> None:
        token = getattr(offendingSymbol, "text", None)
        near = f" near {token!r}" if token else ""
        self.errors.append(f"line {line}:{column} {msg}{near}")


def parse_errors(query, lexer_cls, parser_cls, start_rule) -> list[str]:
    listener = _CollectingErrorListener()
    lexer = lexer_cls(InputStream(query))
    lexer.removeErrorListeners(); lexer.addErrorListener(listener)
    parser = parser_cls(CommonTokenStream(lexer))
    parser.removeErrorListeners(); parser.addErrorListener(listener)
    getattr(parser, start_rule)()
    return listener.errors[:_MAX_ERRORS]
```

The start rule per language is **EOF-anchored**, so trailing garbage after an
otherwise-valid prefix is reported rather than silently accepted:

- Cypher: `statements`
- Gremlin: `queryList`
- AQL: `queryStart`

### A validator class

The validators are thin: an empty-query fast path, then delegation. Example
(`cypher/syntax.py`):

```python
_START_RULE = "statements"


def _cypher_syntax_errors(query: str) -> list[str]:
    if not query.strip():
        return ["Query is empty"]
    return parse_errors(query, Cypher25Lexer, Cypher25Parser, _START_RULE)


class CypherSyntaxValidator:
    def validate(self, query: str) -> list[str]:
        return _cypher_syntax_errors(query)

    def close(self) -> None:
        return None
```

`gremlin/syntax.py` is identical except for the lexer/parser and
`_START_RULE = "queryList"`. The async siblings call the same sync function
inline: parsing is pure CPU and microsecond-fast, so a thread pool would add
scheduling overhead without unblocking the event loop.

### How errors reach the LLM

`validate()` returns a `list[str]`. The translator stores it on the state and,
when non-empty, passes it to `build_fix_prompt(...)` (see `engine/prompts.py`), so the
LLM sees messages like:

```
line 1:23 mismatched input 'n' expecting {':', '$', IS, '{', ')', WHERE} near 'n'
```

That precise, located message is the main advantage over the old regex strings.

## 5. Implementation steps (reproducible recipe)

1. **Vendor the grammars.** Copy each engine's official ANTLR grammar into
   `src/sql2graph/validators/_grammar/sources/` and record provenance (upstream URL,
   version, license, any edits) in `_grammar/sources/README.md`. Both grammars are
   Apache-2.0 and vendored verbatim. See section 9 for sources.
2. **Get the toolchain.** Regeneration (dev-time only) needs a JDK and the ANTLR
   `4.13.2` "complete" jar. Practical notes from doing this:
   - Nothing needs Java at *runtime*; only `antlr4-python3-runtime` is imported.
   - `antlr4-tools` can fail to auto-resolve the version (its Sonatype lookup
     returned HTTP 400); pin the version with `-v 4.13.2`, or download the jar
     directly, or use a portable JDK. The jar is cached at
     `~/.m2/repository/org/antlr/antlr4/4.13.2/antlr4-4.13.2-complete.jar`.
3. **Generate the Python parsers** with `scripts/generate_parsers.sh`. It runs
   ANTLR with `-Dlanguage=Python3 -no-listener -no-visitor` (only the lexer and
   parser are needed), generates from inside the sources directory with bare
   filenames so the generated header stays a relative path (no local absolute
   path leaks into a committed file), deletes the `.interp` / `.tokens` dev
   artifacts, and **prepends `from __future__ import annotations`** to each
   generated file. That shim matters: the grammars carry a few Java-typed rule
   arguments (e.g. `parameter[String paramType]`), which the ANTLR Python target
   emits as real annotations (`paramType:String`). Python 3.12 evaluates
   parameter annotations at definition time, so `String` raises `NameError`;
   3.14 does not. PEP 563 makes every annotation a lazy string, so the undefined
   Java types are never evaluated. The generated parsers are then **committed**.
4. **Add the shared plumbing** `_grammar/runtime.py` (`_CollectingErrorListener`
   + `parse_errors`), as shown in section 4.
5. **Write the validator classes** `cypher/syntax.py`, `gremlin/syntax.py`, and
   `aql/syntax.py`: keep the empty-query fast path, delegate to `parse_errors`
   with the language's generated lexer/parser and start rule, and provide async
   siblings that call the same function inline.
6. **AQL syntax tier.** AQL uses a hand-ported grammar (see section 3), so
   `make_validator` / `make_async_validator` return `AqlSyntaxValidator` for
   `("aql", "syntax")`, and `valid_modes_for_target("aql")` reports
   `("none", "syntax", "server")` -- the same modes as the other targets.
7. **Packaging and typing** (`pyproject.toml`):
   - dependency `antlr4-python3-runtime>=4.13,<4.14` (must match the tool version),
   - mypy override `antlr4.*` -> `ignore_missing_imports`, and
     `sql2graph.validators._grammar.generated.*` -> `ignore_errors` (generated
     code is not strict-clean),
   - ruff `extend-exclude = ["src/sql2graph/validators/_grammar/generated"]`.
8. **Tests** in `tests/unit/validators/test_syntax.py`: assert valid queries pass
   and malformed ones fail (including the regression that
   `MATCH (n {name:'a)b'}) RETURN n` now passes, which the old bracket-counting
   rejected). `tests/unit/validators/test_factory.py` asserts that
   `make_validator("aql","syntax")` returns `AqlSyntaxValidator` and that
   `valid_modes_for_target` is correct. `tests/integration/test_managed.py`
   cross-checks the offline AQL grammar against ArangoDB's own `db.aql.validate`
   so the hand-port can't silently drift.
9. **Update downstream consumers:** any consumer that offers a mode choice
   (a CLI, a UI such as the separately maintained sql2graph-web) should derive
   it from `valid_modes_for_target` rather than hardcoding the modes, so a new
   target's syntax tier appears everywhere the moment the factory knows it.

## 6. Regenerating the parsers

Only needed when bumping a grammar or the ANTLR version. The committed parsers
are otherwise authoritative.

```bash
# Uses the jar in ~/.m2 and `java` on PATH by default:
scripts/generate_parsers.sh

# Or point at a specific jar / JDK:
ANTLR_JAR=/path/to/antlr-4.13.2-complete.jar JAVA=/path/to/bin/java scripts/generate_parsers.sh
```

The ANTLR tool version **must match** the `antlr4-python3-runtime` pin in
`pyproject.toml` (4.13.x). Regeneration is deterministic: re-running on an
unchanged grammar produces byte-identical output (the generated header is a
relative path with no timestamp), so a clean diff is the expected result. To
upgrade a grammar, replace the `.g4` under `_grammar/sources/`, update
`_grammar/sources/README.md` (version + any edits), regenerate, and run the tests.

## 7. Usage

```python
from sql2graph import make_validator

v = make_validator("cypher", "syntax")
v.validate("MATCH (n:Person) RETURN n.name")          # []  (valid)
v.validate("MATCH (n)-[r]-( RETURN n")                # ["line 1:.. ..."]  (malformed)
v.validate("MATCH (n {name:'a)b'}) RETURN n")         # []  (valid; regex used to reject this)
```

What `syntax` does and does not catch:

- **Catches:** structural / grammar errors: clause shape and ordering, malformed
  node/relationship patterns and traversal steps, unterminated strings, stray
  tokens, bad expression/function-call syntax.
- **Does not catch:** schema-level mistakes such as non-existent labels,
  relationship types, or properties. Those are caught by the `server` tier
  (against a schema-aware backend) on the output side, and the input-side
  pre-flight gate already blocks SQL that references unmapped tables/columns.

Mode availability is uniform across targets: `cypher`, `gremlin`, and `aql` all
support `none`/`syntax`/`server` (see `valid_modes_for_target`). AQL's `syntax`
mode uses the hand-ported ArangoDB grammar described in section 3; it is
best-effort, and the `server` / `managed` validator remains authoritative.

## 8. Testing and verification

- Unit tests live under `tests/unit/` (no network, no DB) -- notably
  `tests/unit/validators/test_syntax.py` and `tests/unit/sql/`. Run them with
  `uv run pytest -m "not integration"`.
- Type and lint gates: `uv run mypy src tests/unit` (strict; the generated
  package is excluded via the override) and `uv run ruff check .` /
  `uv run ruff format --check` (the generated dir is excluded).
- Regeneration check: run `scripts/generate_parsers.sh` and confirm the
  committed parsers are unchanged (byte-stable).

## 9. References

- Cypher grammar: Neo4j `cypher-language-support`,
  `packages/language-support/src/antlr-grammar/Cypher25{Lexer,Parser}.g4`,
  Apache-2.0. <https://github.com/neo4j/cypher-language-support>
- Gremlin grammar: Apache TinkerPop `gremlin-language`,
  `gremlin-language/src/main/antlr4/Gremlin.g4`, Apache-2.0.
  <https://github.com/apache/tinkerpop>
- ANTLR Python target runtime: `antlr4-python3-runtime` (4.13.x).
- See also: `src/sql2graph/validators/_grammar/sources/README.md` (provenance),
  [api.md](../api.md) (public validator surface), and [architecture.md](../architecture.md)
  (the generate-validate-fix loop and module responsibilities).

