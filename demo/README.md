# `demo/` — parametrized reference client

This directory contains a runnable demonstration of the rows2graph framework.
The CLI in `cli.py` is a *reference client*: every component is constructed
through the public library API documented in `docs/API.md`. Studying
`cli.py` is the fastest path to understanding how to embed the framework in
your own code.

## Prerequisites

```bash
# From the project root:
uv sync
```

You also need *one* LLM backend reachable:

* **Ollama** (local). On macOS: `brew install ollama && ollama pull llama3.2`.
  If you tunnel to a remote GPU server, point `host` in
  `config/models/ollama.yaml` at the local end of the tunnel.
* **Anthropic (direct API)**. Create a key in the
  [Anthropic console](https://console.anthropic.com) and export it:
  `export ANTHROPIC_API_KEY="sk-ant-..."`. The SDK reads it automatically;
  no fields need to be filled into `config/models/anthropic.yaml` unless
  you want to pin a non-default model.

`--validation server` validates against a live database. Pass `--server
config/servers/<engine>.yaml` to use your own instance, or **omit `--server`
to auto-provision a throwaway one** — the library starts a disposable Neo4j /
ArangoDB / Gremlin container (via `testcontainers`) and removes it at exit.
Managed provisioning needs a running Docker daemon; `--validation syntax` and
`--validation none` need neither a database nor Docker.

## Basic invocation

```bash
uv run python demo/cli.py \
    --sql "SELECT name, address FROM supplier WHERE suppkey = 1337" \
    --mapping config/mappings/tpch.yaml \
    --model   config/models/ollama.yaml \
    --target  cypher \
    --validation syntax
```

Expected output (the generated Cypher on stdout, logs on stderr):

```cypher
MATCH (s:Supplier {suppkey: 1337})
RETURN s.name, s.address
```

The demo prints, in order: a settings header, **Input SQL**, the
**Conversation (system ↔ model)** transcript (streamed live as the model responds), the **Generated** query, and a
**Result** summary. Graph-DB driver warnings/errors are silenced from the
console; validation errors still appear in the Result panel and are fed back to
the model during the fix loop.

## Flag reference

```
Input:
  --sql STRING            SQL query, or "-" to read from stdin
  --mapping PATH          Schema-mapping YAML

LLM:
  --model PATH            Model-config YAML (provider field selects Ollama or Anthropic)

Target language:
  --target {cypher,aql,gremlin}  Default: cypher

Validation:
  --validation {syntax,server,none}  Default: syntax
  --server PATH           Server-config YAML. Optional under --validation=server:
                          omit it to auto-provision a throwaway database (needs Docker)
  --max-iterations N      Default: 3

Logging:
  -v, --verbose           DEBUG-level logging on stderr
```

## Examples

### LDBC SNB, Anthropic backend, syntax-only validation

```bash
uv run python demo/cli.py \
    --sql "SELECT p2.p_firstname FROM person p1 JOIN knows k ON k.k_person1id = p1.p_personid JOIN person p2 ON p2.p_personid = k.k_person2id WHERE p1.p_personid = 933" \
    --mapping config/mappings/ldbc.yaml \
    --model   config/models/anthropic.yaml \
    --target  cypher \
    --validation syntax
```

### TPC-H, Ollama backend, server-side validation against Neo4j

```bash
export NEO4J_PASSWORD=...
uv run python demo/cli.py \
    --sql "SELECT name FROM supplier WHERE suppkey = 1337" \
    --mapping config/mappings/tpch.yaml \
    --model   config/models/ollama.yaml \
    --target  cypher \
    --validation server \
    --server  config/servers/neo4j.yaml \
    -v
```

### TPC-H, managed validation (zero-config, auto-provisioned Neo4j)

No database setup and no `--server`: the library starts a throwaway Neo4j
container, validates against it, and tears it down. Requires a running Docker
daemon.

```bash
uv run python demo/cli.py \
    --sql "SELECT name FROM supplier WHERE suppkey = 1337" \
    --mapping config/mappings/tpch.yaml \
    --model   config/models/anthropic.yaml \
    --target  cypher \
    --validation server \
    -v
```

### LDBC SNB, AQL target, server-side validation against ArangoDB

```bash
export ARANGO_PASSWORD=...
uv run python demo/cli.py \
    --sql "SELECT f.f_title, COUNT(*) AS members FROM forum f JOIN forum_person fp ON fp.fp_forumid = f.f_forumid GROUP BY f.f_forumid, f.f_title ORDER BY members DESC LIMIT 10" \
    --mapping config/mappings/ldbc.yaml \
    --model   config/models/anthropic.yaml \
    --target  aql \
    --validation server \
    --server  config/servers/arangodb.yaml
```

### Read SQL from stdin

```bash
cat demo/queries/tpch.sql | grep -A1 '^-- Q3' | tail -n1 | \
    uv run python demo/cli.py \
        --sql - \
        --mapping config/mappings/tpch.yaml \
        --model   config/models/ollama.yaml
```

## Files in this directory

| File | Purpose |
|---|---|
| `cli.py` | The parametrized demo CLI itself. |
| `queries/tpch.sql` | A reference set of TPC-H SQL queries (Q1–Q14). |
| `queries/ldbc.sql` | A reference set of LDBC SNB SQL queries (Q1–Q14). |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Translation succeeded — validator reported no errors. |
| `1` | Validation failed after `--max-iterations` attempts. The last generated query is still printed to stdout; the error list goes to stderr. |
| `2` | Argument / config error before any LLM call. |
