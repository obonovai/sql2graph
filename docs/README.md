# sql2graph documentation

`sql2graph` translates SQL queries into graph-database queries (Cypher, AQL,
Gremlin) by prompting an LLM with a relational-to-graph schema mapping and
repairing the output through a generate-validate-fix loop. These pages are
written against the source and cite the files they describe; install and quick
start live in the [project README](../README.md).

## Suggested reading order

1. [getting-started.md](getting-started.md): from a clean checkout to a first
   validated translation, and how to read the result.
2. [architecture.md](architecture.md): the mental model of the
   generate-validate-fix loop and the design decisions behind it.
3. [mapping/authoring.md](mapping/authoring.md): design and hand-write the
   schema mapping, with [mapping/format.md](mapping/format.md) as the field
   reference.
4. [validation/modes.md](validation/modes.md): choose between `none`,
   `syntax`, `server`, and managed validation.
5. [mapping/builder.md](mapping/builder.md): generate a first-draft mapping
   from `CREATE TABLE` DDL instead of writing it by hand.
6. [api.md](api.md): the full public Python surface, when you integrate the
   library.

## Map

### Start here

- **[getting-started.md](getting-started.md)**: the nine-step first-run
  tutorial; no database or Docker required.

### Foundations

- **[architecture.md](architecture.md)**: design rationale; the plain-Python
  loop, the pre-flight gate, per-query prompt assembly, the async path, known
  limitations.
- **[api.md](api.md)**: every exported symbol with signatures; protocols,
  factories, result types, iteration events.

### Schema mapping

- **[mapping/authoring.md](mapping/authoring.md)**: designing and hand-writing
  a mapping; worked TPC-H and LDBC examples; the review checklist for
  generated drafts.
- **[mapping/format.md](mapping/format.md)**: the mapping YAML field
  reference; typed properties, composite keys, list properties.
- **[mapping/builder.md](mapping/builder.md)**: the DDL-to-mapping pipeline
  (extract, project, LLM-refine) and its preservation guardrail.
- **[mapping/ldbc-normalization.md](mapping/ldbc-normalization.md)**: case
  study; which of two normalized LDBC schemas projects to the correct graph,
  and the one clause that decides edge direction.

### Validation

- **[validation/modes.md](validation/modes.md)**: the decision guide; what
  each mode checks, needs, and misses.
- **[validation/syntax.md](validation/syntax.md)**: internals of the
  grammar-based (ANTLR) syntax tier and how to regenerate the parsers.

### Operations

- **[configuration.md](configuration.md)**: the model and server config YAML
  schemas, `${VAR}` interpolation, and the environment-variable table.
- **[troubleshooting.md](troubleshooting.md)**: common failures by layer, and
  the canonical `TranslationResult.status` interpretation table.

### Extending

- **[extending.md](extending.md)**: the step-by-step recipes for a new target
  language or a new LLM provider.

## Directory guides

First-class pages that live next to what they describe:

- **[examples/README.md](../examples/README.md)**: the translation inputs
  (DDL, mappings, example SQL) and how they feed a run.
- **[config/README.md](../config/README.md)**: the shipped model and server
  config files.
- **[tests/README.md](../tests/README.md)**: test layout, markers, fixtures,
  and integration environment variables.
- **[eval/README.md](../eval/README.md)** and
  **[eval/METRICS.md](../eval/METRICS.md)**: the evaluation harness and the
  metric definitions.
- **[validators/_grammar/sources/README.md](../src/sql2graph/validators/_grammar/sources/README.md)**:
  provenance of the vendored ANTLR grammars.
