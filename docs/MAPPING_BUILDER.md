# Mapping builder

**Bootstraps a first-draft `SchemaMapping` from `CREATE TABLE` DDL, so a reviewer edits a generated mapping instead of writing one by hand.**

Every translation `sql2graph` performs needs a relational-to-graph
[schema mapping](API.md#schemamapping-and-nodemapping-edgemapping). Authoring that
mapping by hand is the tedious part: for a schema of any size it means transcribing
every table into a node, every foreign key into an edge, and every column into a
property, keeping the SQL identifiers exact. The `mapping_builder/` subpackage
generates that mapping mechanically from the schema's DDL. The output is a *first
draft* meant to be reviewed and edited, not a final answer: it is emitted as ordinary
mapping YAML (indistinguishable from a hand-authored file, and freely editable), and
every non-obvious decision it made is recorded so the reviewer knows exactly what to
check. For the architectural *why* see [Architecture](ARCHITECTURE.md); for the output
mapping schema see the [API reference](API.md).

All public symbols named below are re-exported from the top-level package:

```python
from sql2graph import build_mapping, BuildResult, CoverageReport, project_to_mapping
```

---

## The pipeline

A build is three stages, each its own module, chained in order. The first two are
deterministic and run offline; the third is optional and calls an LLM (only when one is
supplied), fenced so it can only improve names.

```
CREATE TABLE DDL
      │
      ▼  extract   (ddl.py, via sqlglot)
RelationalSchema  ── the dependency-free IR
      │
      ▼  project   (project.py, deterministic, valid-by-construction)
SchemaMapping skeleton  +  CoverageReport
      │
      ▼  refine    (refine.py, guarded LLM naming pass)
SchemaMapping (renamed labels / edge types / property keys, or the skeleton unchanged)
```

`build_mapping` runs the deterministic stages and, when an `llm` is passed, the
refinement stage too, returning a [`BuildResult`](#buildresult). With no `llm` it is
deterministic, offline, and free. The deterministic projection on its own is also
available directly through `project_to_mapping`.

### Extract: DDL to `RelationalSchema`

`extract_schema_from_ddl(ddl, *, dialect=None)` parses one or more `CREATE TABLE`
statements with sqlglot and walks the parse tree into a
[`RelationalSchema`](#the-relationalschema-ir) - a set of frozen dataclasses
(`Table`, `Column`, `ForeignKey`) with no parser or database dependency. This is the
only module in the subpackage that imports sqlglot; every downstream heuristic
consumes the IR, never an `exp` node. Keeping the IR dependency-free is deliberate: it
is the seam where a future schema source (live-database introspection reading
`information_schema`, or SQLAlchemy reflection) could plug in and reuse every
downstream stage unchanged. See `src/sql2graph/mapping_builder/ddl.py` and
`src/sql2graph/mapping_builder/relational.py`.

The extractor also merges `ALTER TABLE ... ADD CONSTRAINT` statements into the table
they target, so primary and foreign keys declared *after* the table (the
`pg_dump` / migration style) are not lost; an `ALTER` may legally precede its table's
`CREATE` in the batch. Comparisons casefold throughout, while stored identifiers keep
their original casing.

Anything that cannot become a base table is *skipped* rather than silently dropped,
and recorded as a `SkippedObject` on `RelationalSchema.skipped_objects` so the audit
can always explain it:

| `SkippedObject` field | Meaning |
|---|---|
| `name` | Best-effort name of the skipped `CREATE` object. |
| `kind` | The lower-cased `CREATE` kind (e.g. `"view"`, `"index"`, `"table"`, `"constraint"`). |
| `reason` | Why it did not become a node (e.g. a `CREATE VIEW`, a `CREATE TABLE ... AS SELECT` with no column list, a duplicate table name, or an `ALTER` against an unknown table). |

`DdlParseError` (a subclass of `ValueError`) is raised when sqlglot cannot parse the
DDL at all - any `sqlglot.errors.SqlglotError`, including tokenizer errors. Being a
`ValueError` subclass lets callers that already funnel value errors into a user-facing
message handle it without a new branch.

### Project: `RelationalSchema` to a valid skeleton

`project_to_mapping(schema)` applies the canonical relational-to-property-graph rules
and returns a `ProjectionResult` (`.mapping` plus a `.report`). The mapping is built
straight into the Pydantic models, so it is **valid by construction**: it passes every
cross-field validator (`SchemaMapping`'s edge-reference, duplicate-label, and
duplicate-edge checks) the moment it is returned. See
`src/sql2graph/mapping_builder/project.py`.

The heuristics:

- **Table becomes a node.** Each non-junction table with at least one column becomes a
  `NodeMapping`. Its label is derived by `naming.table_to_label` (see below), made
  unique against previously assigned labels (a collision is suffixed and warned).
- **Foreign key becomes an edge.** Each foreign key on a node table becomes an
  `EdgeMapping` joining the source node's FK column(s) to the target node's key. A
  composite (multi-column) foreign key keeps **all** its columns, positionally matched
  to the referenced key (`source.fk[i] = target.pk[i]`); a foreign key whose column
  count does not match the referenced key, one referencing an unknown table, or one
  modeled as an edge, is dropped with a reason.
- **Edge direction — composition vs association.** By default an FK edge points
  *FK-holder → referenced* (child → parent), which is right for a reference /
  association. When the schema marks the foreign key as **composition** (the parent
  owns the child), the edge is reversed to *parent → child* — the join columns
  (`source_table` / `source_foreign_key` / `target_primary_key`) are unchanged, only the
  labeled `source_node` / `target_node` swap. `is_composition_fk(table, fk)` recognizes
  two structural signals: an `ON DELETE CASCADE` referential action, or an **identifying
  relationship** (the FK column is part of the table's primary key — a weak entity such
  as `lineitem(orderkey, linenumber)`). A plain foreign key (no cascade, not in the
  primary key) is never reversed, so no existing schema's edges change direction unless
  it declares one of these markers; every reversal is recorded in `warnings`. A lone 1:N
  foreign key is otherwise direction-ambiguous (it looks identical whether it means
  "belongs to" or "contains"), so this is the only structural handle on direction — see
  [LDBC_NORMALIZATION.md](LDBC_NORMALIZATION.md).
- **Junction table becomes an edge.** `is_junction_table(table, schema)` detects a
  pure association table and collapses it into a single edge carrying its non-FK
  columns as edge properties. The predicate is intentionally *strict* (precision over
  recall): it requires **exactly two** single-column foreign keys on distinct columns,
  a primary key equal to exactly those two FK columns, that nothing else references the
  table, and that both referenced tables are present. A real entity that merely happens
  to have two foreign keys is kept as a node and flagged rather than silently dissolved.
- **Value-list table becomes a list property.** `is_multivalue_property_table(table,
  schema)` detects a multi-valued attribute stored relationally as a child table —
  **exactly one** single-column foreign key to a present table, at least one non-FK value
  column, referenced by nothing, and **every column part of the primary key**
  (`pk == all columns`, so the row is a bare value with no identity of its own, e.g.
  `person_email(person_id, email)` or `person_speaks(person_id, language)`). Instead of a
  spurious node, it folds into a `ListProperty` on the *parent* node
  (`Person.email`, `Person.language`) — a multi-valued property the scalar `properties`
  map (one graph property ← one column of the node's own table) cannot express. The
  parent-table → list-properties resolution is `_resolve_list_properties`; the folded
  child tables are recorded in `report.list_property_tables`.
- **Primary-key choice.** `choose_primary_key(table)` returns `(columns, synthesized)`.
  It returns the table's full declared primary key, so a composite PK like
  `lineitem(orderkey, linenumber)` keeps **both** columns as the node's identity (even
  though `orderkey` also becomes an edge); with no declared key it falls back to the
  first column and flags the guess (`synthesized=True`).
- **Properties.** A node's non-foreign-key columns become properties (a join column
  becomes an edge, not a stored value); every primary-key column is always included,
  even a composite key's foreign-key part.
- **Naming** is delegated to `naming.py` (see [Naming](#naming)).
- **Semantic typing** is delegated to `sql_types.py` (see [Semantic typing](#semantic-typing)).

Every projection returns a `CoverageReport` alongside the mapping - the human-readable
account of what was done and what to check:

| `CoverageReport` field | Type | Contents |
|---|---|---|
| `node_tables` | `list[str]` | Tables that became node labels. |
| `edge_tables` | `list[str]` | Junction tables collapsed into edges. |
| `list_property_tables` | `list[str]` | Value-list child tables folded into node list properties (`person_email`, `person_speaks`). |
| `fk_edges` | `list[str]` | One line per emitted edge, e.g. `"Lineitem -[SUPP]-> Supplier (lineitem.suppkey)"` (deterministic labels, before refinement); a composition edge is tagged `[reversed to parent->child: …]`. |
| `dropped_objects` | `list[tuple[str, str]]` | `(name, reason)` for things that produced nothing (views, FKs to unknown tables, empty-column tables, duplicate names). |
| `synthesized_keys` | `list[str]` | Tables whose primary key had to be guessed. |
| `warnings` | `list[str]` | Soft issues a reviewer should look at (synthesized keys, foreign keys dropped for a mismatched column count, candidate association tables kept as nodes, label collisions). |

`CoverageReport.as_dict()` returns a JSON-serialisable view (adding derived
`node_count` / `edge_count`) for a UI audit panel.

### Refine: a guarded LLM naming pass

The deterministic projection gets the *structure* right but leaves the *names* clunky:
a mechanical `HAS_<TARGET>` edge type, or a label that still needs its word boundaries
fixed (`Lineitem`). `refine_mapping(skeleton, schema, llm)` asks an LLM to fix exactly
that - the graph-facing names (node **labels**, edge **types**, and property **keys**)
- and nothing else. See `src/sql2graph/mapping_builder/refine.py`.

The pass is best-effort and *cannot* corrupt the mapping, because a hard guardrail
rejects any structural change and falls back to the always-valid skeleton. The
guardrail has three checks, all run in `_parse_and_validate`:

1. **`validate_against_schema(mapping, schema)`** - proves the candidate only
   references SQL identifiers (`source_table`, `primary_key`, `source_foreign_key`,
   `target_primary_key`, and every property *value*) that actually *exist* in the
   extracted schema. It never inspects labels, edge types, or property *keys*, which
   the LLM is allowed to rewrite. (This is the inverse of
   `sql2graph.engine.preflight.find_unmapped_columns`, which checks a *query* against a
   mapping.)
2. **Preservation check** (`_preservation_violations`) - proves the SQL side was not
   merely *valid* but *preserved*. It compares a SQL-facing signature of the skeleton
   and the candidate (each node's `source_table` / `primary_key` / typed columns, each
   edge's `source_table` / foreign key / target key / typed columns, all casefolded and
   independent of graph-facing names). Any swapped identifier, repointed key, altered
   property type, or added/dropped node or edge changes the signature and is rejected.
3. **Coverage check** (`_coverage_regressions`) - flags any node table the candidate
   dropped or introduced relative to the skeleton.

If any check fails, the model gets **one repair round-trip** (configurable via
`max_repair_attempts`, default `1`) with the concrete violations fed back; if it still
fails, or the LLM errors, or the output is unparseable, the deterministic skeleton is
returned unchanged with an explanatory warning. The result is therefore always valid,
even when the model is wrong or unreachable. This mirrors the translator's own
generate → validate → fix loop: the model proposes, a deterministic check disposes.

`refine_mapping` returns a `RefinementResult`:

| `RefinementResult` field | Type | Meaning |
|---|---|---|
| `mapping` | `SchemaMapping` | The refined mapping when the output passed the guardrail, otherwise the unchanged skeleton. |
| `accepted` | `bool` | `True` only when the LLM output was validated and applied. |
| `messages` | `list[dict[str, str]]` | Full chat transcript (`system` / `user` / `assistant`, plus any repair turn). Always populated. |
| `warnings` | `list[str]` | Non-fatal explanations (a rejected suggestion, an LLM error). |

---

## `BuildResult`

`build_mapping` and `build_mapping_async` return a frozen `BuildResult` carrying
everything a build produced. Assembled in `_finalize`, its fields are:

| `BuildResult` field | Type | Meaning |
|---|---|---|
| `mapping` | `SchemaMapping` | The generated mapping - refined when the LLM output passed the guardrail, deterministic otherwise. |
| `yaml` | `str` | `mapping` serialised to canonical YAML, ready to save or load. |
| `report` | `CoverageReport` | How the schema was projected (see the table above). |
| `refined` | `bool` | `True` iff the naming pass changed the deterministic skeleton. Defaults to `False`. |
| `warnings` | `list[str]` | Non-fatal issues from projection and refinement combined (synthesized keys, dropped edges, rejected refinement). Always safe to surface. |
| `skeleton_yaml` | `str` | The deterministic mapping's YAML *before* refinement. Equals `yaml` when refinement was skipped, kept every name, or was rejected; the "original" a reviewer compares against otherwise. |
| `conversation` | `list[dict[str, str]]` | The refinement chat transcript, so a caller can show exactly what the model was asked and answered. Empty when no `llm` was supplied (deterministic build). |
| `diff` | `MappingDiff \| None` | The renames the LLM applied. `None` when refinement was skipped; otherwise present but empty when it kept every name or its output was rejected. |

The naming pass is optional: pass an `llm` to `build_mapping` to run it, or omit it for
a deterministic, offline build. When it is skipped, `conversation` is empty and `diff`
is `None`. When it runs but is rejected by the guardrail, the outcome's mapping *is* the
skeleton, so `refined` is `False` and `diff` is empty, yet the conversation still
records what was attempted.

---

## The rename diff

`diff_mappings(before, after)` returns a `MappingDiff` describing exactly what the
refinement renamed going from the deterministic skeleton to the refined mapping.
Because refinement may only change graph-facing names, a clean, small "what the AI
changed" view is possible: each entity is matched by the identifiers refinement cannot
touch, and only the differing names are reported. See
`src/sql2graph/mapping_builder/diff.py`.

Matching keys: nodes by `source_table`, edges by
`(source_table, source_foreign_key, target_primary_key)`, and properties by their SQL
*column value*. Matching is conservative - an entity whose key is not unique on either
side is skipped rather than guessed, so the diff never reports a spurious rename.

```python
class MappingDiff:
    label_renames: list[RenameDiff]
    edge_type_renames: list[RenameDiff]
    property_renames: list[RenameDiff]

    def is_empty(self) -> bool: ...
    def as_dict(self) -> dict[str, Any]: ...
```

Each entry is a `RenameDiff`:

| `RenameDiff` field | Meaning |
|---|---|
| `kind` | `"node label"`, `"edge type"`, or `"property"`. |
| `where` | Context: the source table, the join column (`table.fk`), or `Label.column`. |
| `before` | The deterministic name. |
| `after` | The refined name. |

---

## Worked example

The TPC-H DDL in `examples/ddl/tpch.sql` makes the pipeline concrete. `build_mapping`
takes the DDL and an LLM client and returns a full `BuildResult`:

```python
from pathlib import Path

from sql2graph import build_mapping, load_model_config, make_llm

ddl = Path("examples/ddl/tpch.sql").read_text()
llm = make_llm(load_model_config("config/models/anthropic.yaml"))

result = build_mapping(ddl=ddl, dialect="postgres", llm=llm)

Path("examples/mappings/tpch.generated.yaml").write_text(result.yaml)

for warning in result.warnings:
    print("!", warning)
for rename in result.diff.label_renames + result.diff.edge_type_renames:
    print(f"{rename.kind}: {rename.before} -> {rename.after}  ({rename.where})")
```

The signature is keyword-only: `build_mapping(*, ddl: str, dialect: str | None = None,
llm: LLMClient) -> BuildResult`. `dialect` is passed straight to sqlglot (e.g.
`"postgres"`, `"mysql"`); `None` uses sqlglot's dialect-neutral default. It raises
`DdlParseError` if the DDL cannot be parsed.

**Before (deterministic skeleton, in `result.skeleton_yaml`).** The projection is
correct but mechanical. For TPC-H it produces node labels `Region`, `Nation`,
`Supplier`, `Customer`, `Part`, `Order`, and `Lineitem` (a name with no underscore is
left for the LLM to split), and edge types straight off the FK columns:

```yaml
nodes:
  - label: "Lineitem"                # no underscore to split on -> left for the LLM
    source_table: "lineitem"
    properties:
      linenumber: {column: "linenumber", type: "integer"}
      quantity: {column: "quantity", type: "float"}
      shipdate: {column: "shipdate", type: "date"}
      comment: {column: "comment", type: "string"}
      orderkey: {column: "orderkey", type: "integer"}   # PK column, kept even though it also drives an edge
      # ... other non-foreign-key columns ...
    primary_key: [orderkey, linenumber]   # the full composite primary key is the node's identity
edges:
  - type: "SUPP"                     # from stripping "suppkey" -> "supp"
    source_node: "Lineitem"
    target_node: "Supplier"
    source_table: "lineitem"
    source_foreign_key: "suppkey"
    target_primary_key: "suppkey"
  - type: "HAS_REGION"              # column adds nothing beyond the target -> HAS_<TARGET>
    source_node: "Nation"
    target_node: "Region"
    source_table: "nation"
    source_foreign_key: "regionkey"
    target_primary_key: "regionkey"
```

Note that the `partsupp` table does not appear as a node: `is_junction_table` collapses
it into a single `Part -> Supplier` edge that carries `availqty`, `supplycost`, and
`comment` as edge properties.

`lineitem.orderkey` is part of `lineitem`'s composite primary key — an identifying
relationship — so `is_composition_fk` emits its edge as the composition `Order ->
LineItem` (an order contains its line items) rather than the default `LineItem ->
Order`; the join columns are identical, only the direction flips.

**After (LLM-refined, in `result.yaml`).** The naming pass keeps every SQL identifier
byte-for-byte and rewrites only the graph-facing names - `Lineitem` becomes
`LineItem`, and the mechanical edge types become idiomatic relationship names, as in
the hand-authored `examples/mappings/tpch.yaml`:

```yaml
nodes:
  - label: "LineItem"                # word boundary fixed
    source_table: "lineitem"         # SQL identifiers unchanged
    primary_key: "linenumber"
edges:
  - type: "SUPPLIED_BY"              # SUPP -> SUPPLIED_BY
    source_node: "LineItem"
    target_node: "Supplier"
    source_table: "lineitem"
    source_foreign_key: "suppkey"
    target_primary_key: "suppkey"
```

Because the guardrail forbids any SQL-side change, whatever the model returns either
improves the names or is discarded in favour of the skeleton - the mapping is never
left invalid.

### Offline (no LLM)

For the deterministic projection alone - offline, free, and the seam a future
live-database source plugs into - call the extract and project stages directly:

```python
from pathlib import Path

from sql2graph import extract_schema_from_ddl, project_to_mapping, mapping_to_yaml

schema = extract_schema_from_ddl(Path("examples/ddl/tpch.sql").read_text(), dialect="postgres")
projection = project_to_mapping(schema)

print(mapping_to_yaml(projection.mapping))
for warning in projection.report.warnings:
    print("!", warning)
```

`mapping_to_yaml(mapping, *, header=None)` renders a `SchemaMapping` to the canonical
`nodes:` / `edges:` shape; it round-trips exactly through
`SchemaMapping.from_yaml_string`, so a generated mapping is indistinguishable from a
hand-authored one. An untyped property stays a bare `name: column` string; a typed one
serialises as `name: {column: ..., type: ...}`.

---

## Sync vs async

`build_mapping` (sync) and `build_mapping_async` (async) share an identical,
synchronous extract/project stage; only the naming pass differs in how it drives the
LLM.

```python
async def build_mapping_async(
    *,
    ddl: str,
    dialect: str | None = None,
    llm: AsyncLLMClient,
    on_conversation: ConversationCallback | None = None,
) -> BuildResult: ...
```

The async variant takes an `AsyncLLMClient` and an optional `on_conversation`
callback. When set, the refinement streams the assistant turn as a growing message
snapshot (and emits a snapshot after every turn), so a caller such as the web SSE
bridge can display the chat live as the model "types". Both entry points raise
`DdlParseError` on unparseable DDL, and both return the same `BuildResult`.

---

## Relationship to `SchemaMapping.from_yaml`

The builder writes a mapping; the translator reads one. Their contract is the YAML
file: `build_mapping(...).yaml` (or `mapping_to_yaml(...)`) produces exactly the shape
`SchemaMapping.from_yaml(path)` / `from_yaml_string(text)` consumes, so a generated
mapping is a drop-in for a hand-authored one - save it, review it, edit it, and load
it back:

```python
from sql2graph import SchemaMapping

mapping = SchemaMapping.from_yaml("examples/mappings/tpch.generated.yaml")
```

From there the mapping drives a translation exactly as any hand-authored mapping does;
see the [end-to-end example](API.md#end-to-end-example-library) in the API reference.

### The `RelationalSchema` IR

The intermediate representation the extractor produces and the projection consumes,
for reference (frozen dataclasses, all tuples so instances stay hashable, identifiers
stored with original casing while comparisons casefold):

| Type | Key fields |
|---|---|
| `RelationalSchema` | `tables: tuple[Table, ...]`, `skipped_objects: tuple[SkippedObject, ...]`. |
| `Table` | `name`, `schema`, `columns`, `primary_key`, `foreign_keys`; helpers `column_names()`, `fk_columns()`, `single_column_foreign_keys()`. |
| `Column` | `name`, `data_type` (rendered SQL type, informational), `nullable`. |
| `ForeignKey` | `columns`, `ref_table`, `ref_columns`, `name`, `on_delete` (the `ON DELETE` referential action, e.g. `"CASCADE"`, or `None`; drives composition direction). |
| `SkippedObject` | `name`, `kind`, `reason`. |

### Naming

`naming.py` provides the structural, dependency-free (no inflection library) name
heuristics the projection uses. They are deterministic and correct, if sometimes
clunky - polishing them is the LLM's job.

- `table_to_label(table_name)` → singularized PascalCase, singularizing only the final
  `_`-delimited token: `line_items` → `LineItem`, `orders` → `Order`, `region` →
  `Region`. A name with no underscore is left for the LLM to split (`lineitem` →
  `Lineitem`).
- `edge_type_for_fk(fk, *, target_label)` → a relationship type from a foreign key.
  The FK column drives it with its key suffix stripped
  (`moderator_person_id` → `MODERATOR_PERSON`); when the column adds nothing beyond the
  target (`regionkey` referencing `region`) it falls back to `HAS_<TARGET>`
  (`HAS_REGION`).
- `junction_to_edge_type(junction_table)` → `SCREAMING_SNAKE_CASE` of the junction
  table name (`knows` → `KNOWS`, `forum_has_member` → `FORUM_HAS_MEMBER`).

### Semantic typing

`sql_types.semantic_type_for_sql(data_type)` collapses a column's dialect-noisy SQL
type string (e.g. `"DECIMAL(15,2)"`, `"TIMESTAMP"`) onto the small, closed
[`SemanticType`](API.md) vocabulary - `string`, `integer`, `float`, `boolean`, `date`,
`datetime`, `time`, `duration` - that the translator can surface in its prompt. It is
best-effort: a type that does not resolve to a known family (UUID, JSON, an array, an
unresolved vendor type), or a column that declared no type, returns `None` and the
property is simply left untyped rather than guessed. The result stays overridable by
hand in the emitted YAML.
