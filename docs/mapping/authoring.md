# Authoring a schema mapping

**How to design and hand-write (or hand-edit) a relational-to-graph schema
mapping, with worked examples from the shipped TPC-H and LDBC mappings.**

The mapping is the semantic input of every translation: it tells the LLM how
relational tables and foreign keys project onto graph nodes and edges. This
page is for writing or editing that file by hand. The
[mapping builder](builder.md) generates first drafts mechanically; this page
covers what you need to know to review those drafts or replace them entirely.

## Scope

This page owns: the design decisions behind a mapping (what becomes a node,
what becomes an edge, what a junction table turns into); worked examples from
the shipped TPC-H and LDBC mappings; the review checklist for a generated
draft; common authoring mistakes. Related topics live with their owners:

- [format.md](format.md): the YAML field reference, both property forms,
  composite keys, and list properties.
- [builder.md](builder.md): generating a first-draft mapping from `CREATE
  TABLE` DDL, and the audit artifacts (`CoverageReport`, `MappingDiff`) a
  build produces.
- [ldbc-normalization.md](ldbc-normalization.md): a case study of which of
  two normalized schemas projects to the correct graph, and why.
- [examples/README.md](../../examples/README.md): where the example DDL,
  mappings, and SQL live, and how they feed a translation.

## The mental model

Three rules cover almost everything:

1. **A table becomes a node label.** Each row of the table is one vertex; the
   node's `primary_key` identifies it.
2. **A foreign key becomes an edge type.** The edge joins the FK column(s) in
   one table to the primary key of the target node's table. A pure junction
   table produces an edge instead of a node, with its non-key columns riding
   along as edge properties.
3. **A column becomes a property.** The `properties` map assigns each graph
   property name to the SQL column that supplies its value.

Every name in the file faces one of two worlds, and the difference matters.
Graph-facing names (node labels, edge types, property keys) are yours to
choose, and they are used verbatim in generated queries: `label: "Person"`
means the LLM writes `(:Person)`. SQL-facing names (`source_table`,
`primary_key`, `source_foreign_key`, `target_primary_key`, and every property
*value*) must match the relational schema exactly, because they are what the
pre-flight coverage check compares SQL queries against.

## A minimal mapping

The smallest useful mapping is two nodes and one edge. This is the same
employees/departments example the [field reference](format.md) uses:

```yaml
nodes:
  - label: "Person"
    source_table: "employees"
    properties:
      name: "first_name"
      email: "email_address"
    primary_key: "employee_id"

  - label: "Department"
    source_table: "departments"
    properties:
      name: "dept_name"
    primary_key: "dept_id"

edges:
  - type: "WORKS_IN"
    source_node: "Person"
    target_node: "Department"
    source_table: "employees"
    source_foreign_key: "dept_id"
    target_primary_key: "dept_id"
    properties:
      since: "hire_date"
```

The file *is* the mapping: there is no outer wrapper key. See
[format.md](format.md) for every field.

## What loading enforces

`SchemaMapping.from_yaml(path)` validates strictly at load time, so a
malformed file fails fast with a precise error instead of silently misleading
the LLM (`src/sql2graph/mapping.py::SchemaMapping`). When hand-editing, these
are the rules you can trip:

- **Unknown keys are rejected** (`extra="forbid"` on every model). A typo'd
  field name raises a `ValidationError` naming the offending field rather
  than falling back to a default.
- **Identifiers must be non-blank.** An empty or whitespace-only label,
  table, column, or property name is rejected.
- **Edge endpoints must name declared labels.** Every edge's `source_node`
  and `target_node` is checked against the `label` values under `nodes`;
  an undeclared (or miscased) label fails the load.
- **Duplicate node labels are rejected**, since the label would be ambiguous.
  Fully identical edges are also rejected as a copy-paste slip; edges that
  share a `type` but differ in `source_table` or keys are allowed (that is
  the legitimate reuse pattern shown below).
- **`property_types` keys must be declared properties.** A type annotation
  for a property name that does not appear under `properties` is rejected,
  on nodes and edges alike.
- **A property name cannot be both scalar and a list.** Declaring the same
  name under `properties` and `list_properties` is rejected
  (`src/sql2graph/mapping.py::NodeMapping`).
- **Composite join keys must be length-matched.** On an edge,
  `source_foreign_key` and `target_primary_key` are matched positionally and
  must have the same number of columns
  (`src/sql2graph/mapping.py::EdgeMapping`).

## Worked example: TPC-H

[`examples/mappings/tpch.yaml`](../../examples/mappings/tpch.yaml) maps the
eight-table TPC-H schema
([`examples/ddl/tpch.sql`](../../examples/ddl/tpch.sql)) onto seven node
labels and eight edges. It exercises every design decision this page covers.

### Tables to nodes

Most tables map one-to-one, and the label is where you fix naming that the
relational world forced on you. TPC-H names its order table `orders` (plural)
because `ORDER` is a reserved word; the graph label restores the natural
singular, and the file records why in a comment:

```yaml
  - label: "Order"
    source_table: "orders"  # the SQL table is "orders" (plural) because ORDER is reserved
    properties:
      orderkey: "orderkey"
      orderstatus: "orderstatus"
      totalprice: "totalprice"
      orderdate: "orderdate"
      # ...
    primary_key: "orderkey"
```

A node's identity keeps every column of a composite primary key. `lineitem`
is a weak entity keyed by its parent order plus a line number, and the
mapping preserves both:

```yaml
  - label: "LineItem"
    source_table: "lineitem"
    primary_key: [orderkey, linenumber]
```

See [Composite keys](format.md#composite-keys) for the scalar-or-list rule.

### Foreign keys to edges

Each plain foreign key becomes one edge entry, named for what the
relationship *means* rather than for the join column:

```yaml
  - type: "BELONGS_TO"
    source_node: "Nation"
    target_node: "Region"
    source_table: "nation"
    source_foreign_key: "regionkey"
    target_primary_key: "regionkey"
```

Two foreign keys with the same semantics should share an edge type. Both
`supplier.nationkey` and `customer.nationkey` mean "is located in", so the
file declares two edge entries with the same `type: "LOCATED_IN"`, one from
`Supplier` and one from `Customer`, each with its own `source_table`. The
loader allows this deliberately: only fully identical edge entries are
rejected as duplicates.

### Collapsing a junction table into an edge

The `partsupp` table relates parts to suppliers: its primary key is exactly
its two foreign keys `(partkey, suppkey)`, plus three payload columns. It has
no identity of its own, so the mapping gives it **no node**. It appears only
as an edge between `Part` and `Supplier`, carrying the payload columns as
edge properties:

```yaml
  - type: "SUPPLIED_BY"
    source_node: "Part"
    target_node: "Supplier"
    source_table: "partsupp"
    source_foreign_key: "suppkey"
    target_primary_key: "suppkey"
    properties:
      availqty: "availqty"
      supplycost: "supplycost"
      comment: "comment"
```

Note the file reuses the `SUPPLIED_BY` type here: a `LineItem` is supplied by
a `Supplier` and a `Part` is supplied by a `Supplier`, and both facts read
the same way in a graph query.

### When a junction-like table stays a node

`lineitem` also sits between other tables (it holds foreign keys to `orders`,
`part`, and `supplier`), but it is not a junction: it has many attributes of
its own (quantities, prices, discount, tax, ship dates, flags) and its
primary key is not just its foreign keys. It stays a node, and its three
foreign keys become three edges:

- `PART_OF`: `LineItem` to `Order`
- `OF_PART`: `LineItem` to `Part`
- `SUPPLIED_BY`: `LineItem` to `Supplier`

The rule of thumb: collapse a table into an edge only when it is a pure
association (two foreign keys, no independent identity, payload columns at
most). A real entity that merely happens to hold several foreign keys stays a
node. The builder applies the same test mechanically; see
[builder.md](builder.md).

## Typed properties

A property can be written short (`name: "column"`) or long
(`name: {column: "column", type: "date"}`); the long form records a
`SemanticType` that is surfaced in the LLM prompt. Types are optional, and
most string and numeric columns do not need one. Temporal columns are where a
type earns its keep: without one the LLM has to guess whether a column is a
date or a datetime from the shape of a SQL literal, and a wrong guess can
make a datetime column compare against `date('...')` and silently evaluate to
null. Type your `date`, `datetime`, and `time` columns; leave the rest bare
unless a value is ambiguous. See
[Typed properties](format.md#typed-properties-optional) for the full form and
the `SemanticType` vocabulary.

## Multi-valued attributes

A multi-valued attribute (a person's email addresses, the languages they
speak) is stored relationally in a child table, but in the graph it is a list
property on the parent node, not a separate node. Declare it under
`list_properties`, as
[`examples/mappings/ldbc.yaml`](../../examples/mappings/ldbc.yaml) does for
`Person`:

```yaml
    list_properties:
      email:
        source_table: "person_email"
        foreign_key: "person_id"
        column: "email"
        type: "string"
      language:
        source_table: "person_speaks"
        foreign_key: "person_id"
        column: "language"
        type: "string"
```

The child tables (`person_email`, `person_speaks`) appear nowhere under
`nodes`; they exist only to feed the parent's list properties. See
[List properties](format.md#list-properties-optional) for the field rules.

## Worked example highlights: LDBC

[`examples/mappings/ldbc.yaml`](../../examples/mappings/ldbc.yaml) maps the
LDBC Social Network Benchmark schema (8 node labels, 23 edge entries) and
shows three things TPC-H does not.

**A self-referencing edge.** `source_node` and `target_node` may be the same
label. The friendship junction table `knows` becomes a `Person` to `Person`
edge with a typed property:

```yaml
  - type: "KNOWS"
    source_node: "Person"
    target_node: "Person"
    source_table: "knows"
    source_foreign_key: "friend_id"
    target_primary_key: "id"
    properties:
      creationDate:
        column: "creation_date"
        type: "datetime"
```

**Systematic temporal typing.** The file types `Person.birthday` as `date`,
every `creationDate` (on the `Person`, `Forum`, `Post`, and `Comment` nodes
and on the `KNOWS` and `LIKES` edges) as `datetime`, and the `HAS_MEMBER`
edge's `joinDate` as `datetime`: exactly the date-versus-datetime distinction
the previous section is about.

**Normalization came first.** The LDBC relational schema had to be normalized
(value-list child tables for the multi-valued attributes, and an ownership
marker for the one direction-ambiguous foreign key) before this format could
express it faithfully; [ldbc-normalization.md](ldbc-normalization.md) is the
worked case study.

## Editing a generated draft

The [builder](builder.md) emits ordinary mapping YAML, indistinguishable from
a hand-authored file, so editing a draft is just editing the file. The build
also hands you a review checklist; work through it in this order:

1. **`report.warnings`** in the `CoverageReport`: every soft issue the
   projection wants a human to look at (synthesized keys, foreign keys
   dropped for a mismatched column count, candidate association tables kept
   as nodes, label collisions).
2. **`report.synthesized_keys`**: tables whose primary key had to be guessed
   because the DDL declared none. Confirm or fix each guessed key.
3. **`report.dropped_objects`**: everything that produced nothing (views,
   foreign keys to unknown tables, empty-column tables), each with a reason.
   Decide whether the omission is acceptable.
4. **Candidate junction tables kept as nodes**: the junction test is
   deliberately strict, so a near-miss association table stays a node and is
   flagged in the warnings. If it really is a pure association, rewrite it as
   an edge by hand, as the `partsupp` example above shows.
5. **The rename diff** (`MappingDiff`, see
   [the rename diff](builder.md#the-rename-diff)): when the LLM naming pass
   ran, this lists every label, edge type, and property rename as
   before/after pairs. Read it as the "what the AI changed" view and adjust
   any name you dislike; graph-facing names are yours to edit freely.

## Common mistakes

- **Case matters everywhere.** Node labels and edge endpoints are
  case-sensitive: an edge whose `source_node` is `"order"` while the node
  declares `label: "Order"` fails the load with a "references undefined
  source_node" error.
- **A column missing from `properties` blocks queries that touch it.** The
  pre-flight gate compares each SQL query against the mapping before any LLM
  call; a query naming a column that a mapped table omits is rejected with
  status `unmapped_columns`. Map every column that queries will read, not
  just the interesting ones. See
  [interpreting TranslationResult.status](../troubleshooting.md#interpreting-translationresultstatus).
- **A collapsed junction table has no node label.** Once `partsupp` becomes
  an edge, there is no `PartSupp` vertex in the graph: its data lives on the
  `SUPPLIED_BY` edge, and graph queries over it must match the edge type and
  read edge properties. If you find yourself wanting to match the junction as
  a node, either the collapse was wrong (keep it as a node) or the query
  needs rethinking in edge terms.

## Caveats and limitations

- **The format expresses nodes and edges only.** There is no way to map a
  view, and property values are plain column names, so a computed or derived
  column cannot be expressed; compute it in the source schema or leave it
  out.
- **One table per label, no splitting or merging.** A node draws from exactly
  one `source_table`, and there is no row filter, so a single table cannot be
  split into multiple labels by a predicate, nor can two tables merge into
  one label.
- **The direction of a lone foreign-key edge is a modeling choice** the file
  must get right, because the same join supports both readings; see
  [ldbc-normalization.md](ldbc-normalization.md) for the worked case.
