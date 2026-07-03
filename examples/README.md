# `examples/`: translation inputs for rows2graph

This directory holds the **inputs** a translation operates on: the relational
schema, its graph mapping, and example SQL queries. These are *not*
configuration. They describe *what* is being translated, not *how* the
translation runs. The operational configuration (which LLM, which database)
lives under [`../config/`](../config/README.md).

rows2graph does two things, and the files here feed both:

1. **Builds a schema mapping** from your relational schema (the `CREATE TABLE`
   DDL), via `build_mapping`.
2. **Translates SQL queries** into graph queries (Cypher / AQL / Gremlin) using
   that schema mapping, via `SQLTranslator`.

A rows2graph translation has two inputs:

1. a **schema mapping** (`mappings/`), describing how relational tables and
   foreign keys map to graph nodes and edges, and
2. a **SQL query** to translate.

The mapping is the semantic input the framework interprets: the library
serialises it into the LLM system prompt as content (via `build_system_prompt`),
just as the SQL query is the per-call input to `translate()`. The mapping is
bound once when you build a `SQLTranslator`; the SQL query varies per call. Both
are orthogonal to the model and server *configuration* in `config/`: any mapping
can be translated by any model and validated against any server.

| Subdirectory | Contents | Model / loader |
|---|---|---|
| `ddl/`      | Raw `CREATE TABLE` schema; the source a mapping is generated from. | parsed to `RelationalSchema` by `build_mapping(ddl=...)` |
| `mappings/` | Relational-to-graph schema mapping (nodes + edges). | `rows2graph.SchemaMapping` via `SchemaMapping.from_yaml(path)` |
| `sql/`      | Example SQL queries to translate, grouped by dataset. | plain `.sql` text passed to `translator.translate(...)` |

## `ddl/`: where mappings come from

A mapping YAML is not written from scratch. `ddl/tpch.sql` is the raw TPC-H
relational schema, and `build_mapping` turns that DDL into a first-draft
`mappings/tpch.yaml` for review. The two are kept in sync by
`tests/test_mapping_builder.py`.

```python
from pathlib import Path
from rows2graph import build_mapping, load_model_config, make_llm

llm = make_llm(load_model_config("config/models/anthropic.yaml"))
result = build_mapping(ddl=Path("examples/ddl/tpch.sql").read_text(), llm=llm)
Path("examples/mappings/tpch.yaml").write_text(result.yaml)
```

Only `tpch.sql` ships as DDL; `mappings/ldbc.yaml` was authored directly (there
is no `ldbc.sql`).

## `mappings/`: the translation input

A mapping deserialises directly into `rows2graph.SchemaMapping` and is the first
argument to `SQLTranslator`:

```python
from pathlib import Path
from rows2graph import (
    SchemaMapping, SQLTranslator, load_model_config,
    make_llm, make_target, make_validator,
)

mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")   # the input
llm = make_llm(load_model_config("config/models/anthropic.yaml"))  # the config
target = make_target("cypher")
validator = make_validator("cypher", "syntax")

with SQLTranslator(mapping, llm, target, validator) as translator:
    sql = Path("examples/sql/tpch/q06.sql").read_text()
    result = translator.translate(sql)
    print(result.generated_query)
```

## `sql/`: example queries

`sql/<dataset>/*.sql` are a small, illustrative selection of source queries
spanning easy/medium/hard difficulty: plain `SELECT` statements ready to
translate. The exhaustive set, paired with gold Cypher/AQL/Gremlin translations,
lives in [`../eval/gold/`](../eval/gold) (`ldbc.yaml`)
and drives the evaluation harness.
