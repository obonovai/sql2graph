# Validation modes

**Which validation mode to run: `none`, `syntax`, `server`, or the derived
managed variant, and exactly how the factories resolve them.**

The validator is the feedback half of the generate-validate-fix loop: it
inspects each candidate query and returns a list of error strings (empty means
valid), and any errors are fed back to the LLM for repair. This page owns the
decision of which mode to run and the rules by which a mode string becomes a
concrete validator.

## Scope

This page owns: choosing a validation mode; the mode-resolution rules
(including how "managed" is derived); per-mode capabilities, infrastructure
requirements, and caveats. Related topics live with their owners:

- [syntax.md](syntax.md): internals of the grammar-based syntax tier, grammar
  provenance, and parser regeneration.
- [configuration.md](../configuration.md): the server config YAML files
  (`config/servers/<name>.yaml`) that server mode consumes.
- [api.md](../api.md#validator-components): factory signatures, config
  classes, and the full list of validator classes.
- [troubleshooting.md](../troubleshooting.md#validation): symptom-first fixes
  for validation failures (Docker errors, TinkerGraph misses).
- [tests/README.md](../../tests/README.md#managed-validation-tests-docker-only):
  the managed-mode integration tests and how to run them.

## The four modes at a glance

| Mode | What it checks | Infrastructure | Catches | Misses | Typical use |
|---|---|---|---|---|---|
| `none` | Nothing; always passes | None | Nothing | Everything | Benchmark baseline: raw single-shot LLM quality |
| `syntax` | Grammar (ANTLR parse), in-process | None | Structural and grammar errors, in milliseconds | Schema-level mistakes (nonexistent labels, properties, collections) | CI; laptop iteration |
| `server` | Parse/plan on a live database, plus schema cross-checks where the backend supports them | Your own running Neo4j / ArangoDB / Gremlin Server | Syntax errors plus schema hallucinations (populated Neo4j and ArangoDB) | Label/property hallucinations on schemaless TinkerGraph | Production-like accuracy against your own database |
| `managed` (derived) | Same server check, against a throwaway empty database | A running Docker daemon (`testcontainers`) | Parse/plan errors | Schema hallucinations (the database is empty, so that reporting is suppressed) | Laptop iteration with Docker; integration tests |

## How a mode is resolved

The user-facing mode set is `("none", "syntax", "server")`
(`VALID_VALIDATION_MODES`, src/sql2graph/validators/__init__.py:103), and
`valid_modes_for_target` returns those same three modes for all three targets
(src/sql2graph/validators/__init__.py::valid_modes_for_target). `managed` is
derived, never chosen directly: `resolve_validation_mode` returns `"managed"`
when the requested mode is `"server"` and no `server_config` was supplied, and
passes every other combination through unchanged
(src/sql2graph/validators/__init__.py::resolve_validation_mode).

The factories `make_validator` and `make_async_validator` accept four literal
mode strings: `"none"`, `"syntax"`, `"server"`, and `"managed"`
(src/sql2graph/validators/__init__.py::make_validator). The division of labour
is subtle and worth stating precisely:

- **Callers that resolve first get managed.** The supported flow is to call
  `resolve_validation_mode(mode, server_config=...)` and hand the result to
  the factory; the rule is centralised in that one function so every caller
  shares it. A user request of "server" with no config therefore reaches the
  factory as the literal `"managed"`, which needs no `server_config` (any
  config passed is intentionally ignored; managed mode provisions its own
  database).
- **Calling the factory directly with `"server"` and no config raises.**
  `make_validator(target, "server")` with `server_config=None` raises
  `ValueError`; the factory never silently derives managed on its own.
- **Config type must match the target.** A `server_config` whose class does
  not match the target raises `TypeError`. Which database type each target
  validates against is recorded in `TARGET_SERVER_TYPE`: `cypher` maps to
  `neo4j`, `aql` to `arangodb`, and `gremlin` to `gremlin`
  (src/sql2graph/validators/__init__.py:106).

## Mode: none

`NoopValidator` returns the empty error list for every input, so the
generate-validate-fix loop exits after the first iteration without a second
LLM call (src/sql2graph/validators/noop.py::NoopValidator). This is the mode
for measuring raw single-shot LLM quality: with the fix loop disabled,
results reflect the LLM alone, which makes `none` the evaluation baseline.
It is also useful during prompt engineering when you want to inspect
unrepaired output.

## Mode: syntax

The syntax tier is grammar-based (ANTLR), deployment-free, and in-process:
no database, no Docker, results in milliseconds
(src/sql2graph/validators/__init__.py::make_validator builds
`CypherSyntaxValidator`, `GremlinSyntaxValidator`, or `AqlSyntaxValidator`).
Cypher and Gremlin use each engine's own published grammar. AQL has no
reusable offline grammar, so its validator is a hand-port of ArangoDB's
Flex+Bison parser: the check is best-effort and server mode remains
authoritative for AQL. Grammar provenance, the parse pipeline, and how to
regenerate the parsers are covered in [syntax.md](syntax.md).

## Mode: server

Server mode delegates validation to a live graph database
(src/sql2graph/validators/__init__.py::make_validator). Each backend has its
own validation endpoint:

| Target | Backend | Endpoint | Executes the query? |
|---|---|---|---|
| `cypher` | Neo4j | `EXPLAIN <query>` (src/sql2graph/validators/cypher/server.py::CypherServerValidator) | No: parses and plans only |
| `aql` | ArangoDB | `db.aql.validate(query)` (src/sql2graph/validators/aql/server.py::AqlServerValidator) | No: parses only |
| `gremlin` | Gremlin Server | Script submission via the `gremlinpython` `Client` (src/sql2graph/validators/gremlin/server.py::GremlinServerValidator) | Yes: the script runs on the server (see caveats) |

Against a populated database, server mode also catches schema-level mistakes
that no grammar can see: Neo4j's `EXPLAIN` emits `UNRECOGNIZED` notifications
for labels, relationship types, and properties the database lacks, and the
ArangoDB validator cross-checks the collections a query names against the
database catalogue. The Gremlin path is the exception: TinkerGraph, the
recommended local backend, is schemaless, so it catches parse and
step-compatibility errors but not label or property hallucinations
(point the config at JanusGraph with a registered schema for that). The
connection config files (`config/servers/<name>.yaml`, with `${VAR}`
interpolation for passwords) are documented in
[configuration.md](../configuration.md).

## Mode: managed

Managed mode is server validation without a server to configure: the library
starts a disposable database container via `testcontainers`, points the
matching server validator at it, and owns its lifecycle
(src/sql2graph/validators/provision/__init__.py::ManagedServerValidator).
Practical properties:

- **Requires a running Docker daemon.** If none is reachable, `validate()`
  raises `RuntimeError` with a message pointing at Docker
  (src/sql2graph/validators/provision/__init__.py::_provision).
- **Lazy lifecycle.** The container is provisioned on the first `validate()`
  call and torn down on `close()`; a `warmup()` method exists so callers can
  pay the startup cost before timing a loop.
- **Slow first run.** The database images (Neo4j for `cypher`, ArangoDB for
  `aql`, Gremlin Server for `gremlin`; one provisioning module per engine in
  `validators/provision/`) are pulled on first use; later runs reuse the
  Docker image cache.
- **Suppressed Cypher notifications.** The managed Neo4j connection sets
  `notifications_min_severity="OFF"` so the server never sends the advisory
  notifications the Cypher server validator would otherwise surface as
  schema errors (src/sql2graph/validators/provision/neo4j.py::start). On the
  empty managed database those notifications are guaranteed noise: every
  label and property would look unknown and fail every query. Parse and plan
  errors, which the driver raises rather than reports as notifications, are
  unaffected.

Because the provisioned database is empty, the schema-hallucination reporting
described under server mode is deliberately switched off on both backends
that have it: Neo4j via the notification suppression above, ArangoDB via
`check_collections=False` in the provisioned config
(src/sql2graph/validators/provision/arango.py:34). Managed validation is
therefore the parse/plan check alone.

## Async parity

`make_async_validator` parallels `make_validator` with the same
target/mode/server_config contract and the same resolution rules, including
the derived `"managed"` mode
(src/sql2graph/validators/__init__.py::make_async_validator). The async
classes mirror their sync siblings one-to-one: `AsyncNoopValidator`, the three
`Async*SyntaxValidator` classes, `AsyncCypherServerValidator` (built on
`neo4j.AsyncGraphDatabase`), `AsyncAqlServerValidator` and
`AsyncGremlinServerValidator` (which wrap their synchronous SDKs in
`asyncio.to_thread`), and `AsyncManagedServerValidator` (which additionally
uses an `asyncio.Lock` so concurrent `validate()` calls start only one
container). Constructor signatures match the sync classes; see
[api.md](../api.md#validator-components) for the full listing.

## Choosing for common scenarios

- **CI**: `syntax`. Deployment-free and fast; no Docker daemon or database
  service to arrange on the runner.
- **Laptop iteration**: `syntax` for the quickest loop, or managed (request
  `server` with no config) when Docker is already running and you want the
  database's own parser as the judge.
- **Production-like accuracy**: `server` against your own populated database.
  This is the only mode that catches schema hallucinations, and it validates
  against exactly the engine version you deploy.
- **Benchmark baseline**: `none`, to measure raw single-shot LLM quality with
  the fix loop disabled.

## Caveats and limitations

- **Syntax mode cannot catch schema-level mistakes.** A query naming a
  nonexistent label, property, or collection parses cleanly; only server mode
  against a populated database reports those.
- **The AQL syntax tier is best-effort.** It is a hand-port of ArangoDB's
  parser rather than a published grammar, so the server remains authoritative
  for AQL.
- **TinkerGraph misses hallucinations.** Gremlin server validation against
  schemaless TinkerGraph accepts `.hasLabel('Doesnotexist')` on an empty graph
  without complaint; use JanusGraph with a registered schema for
  schema-aware Gremlin validation.
- **Gremlin server validation executes the script.** Unlike Neo4j's `EXPLAIN`
  and ArangoDB's `db.aql.validate`, which parse without executing, the Gremlin
  validator submits the script and consumes the result, so the traversal
  actually runs on the server
  (src/sql2graph/validators/gremlin/server.py::GremlinServerValidator).
  Against a throwaway or empty TinkerGraph this is harmless; against your own
  populated server, be aware that a generated query with side effects would
  really execute.
- **Managed first runs are slow.** The first managed validation pulls the
  database image before starting the container; subsequent runs reuse the
  cache. `warmup()` lets you provision ahead of any timed loop.
