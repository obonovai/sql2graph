# `examples/`: translation inputs for sql2graph

**The translation inputs: a relational schema, its graph mapping, and the
example SQL that exercises them.**

This directory holds the **inputs** a translation operates on. They describe
*what* is being translated, not *how* the translation runs; the operational
configuration (which LLM, which database) lives under
[`../config/`](../config/README.md), and the authoritative statement of that
split is the project README's [Configuration](../README.md#configuration)
section.

| Subdirectory | Contents | Model / loader |
|---|---|---|
| `ddl/`      | Raw `CREATE TABLE` schema; the source a mapping is generated from. | parsed to `RelationalSchema` by `build_mapping(ddl=...)` |
| `mappings/` | Relational-to-graph schema mapping (nodes + edges). | `sql2graph.SchemaMapping` via `SchemaMapping.from_yaml(path)` |
| `sql/`      | Example SQL queries to translate, grouped by dataset. | plain `.sql` text passed to `translator.translate(...)` |

## `ddl/`: where mappings come from

A mapping YAML need not be written from scratch: `build_mapping` turns the
DDL here into a first-draft mapping for review (see
[`docs/mapping/builder.md`](../docs/mapping/builder.md)). `ddl/tpch.sql` is
the raw TPC-H schema behind `mappings/tpch.yaml`; `ddl/ldbc.sql` and
`ddl/ldbc_naive.sql` are the two normalized LDBC schemas compared in
[`docs/mapping/ldbc-normalization.md`](../docs/mapping/ldbc-normalization.md).

## `mappings/`: the translation input

A mapping deserialises directly into `sql2graph.SchemaMapping` and is the
first argument to `SQLTranslator`:

```python
mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")
```

The full first-run walkthrough is
[`docs/getting-started.md`](../docs/getting-started.md). To design or edit a
mapping by hand, see
[`docs/mapping/authoring.md`](../docs/mapping/authoring.md) (which walks both
shipped mappings); the field reference is
[`docs/mapping/format.md`](../docs/mapping/format.md).

## `sql/`: example queries

`sql/<dataset>/*.sql` are a small, illustrative selection of source queries
spanning easy/medium/hard difficulty: plain `SELECT` statements ready to
translate. The exhaustive set, paired with gold Cypher/AQL/Gremlin translations,
lives in [`../eval/gold/`](../eval/gold) (`ldbc.yaml`)
and drives the evaluation harness.
