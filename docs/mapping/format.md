# Mapping file format

**The YAML reference for the schema mapping: every field, both property
forms, composite keys, and multi-valued list properties.**

The mapping file is the semantic input of a translation: it tells the LLM how
the relational schema projects onto a property graph. This page is the field
reference for that file.

## Scope

This page owns: the mapping YAML schema; field-level validation rules; the
short and long property forms; composite keys; list properties. Related topics
live with their owners:

- [authoring.md](authoring.md): how to design and hand-write a mapping, with
  worked examples.
- [builder.md](builder.md): generating a first-draft mapping from `CREATE
  TABLE` DDL.
- [api.md](../api.md): the Python classes behind this file
  (`SchemaMapping`, `NodeMapping`, `EdgeMapping`) and the rest of the public
  surface.
- [ldbc-normalization.md](ldbc-normalization.md): a case study of which of
  two normalized schemas projects to the correct graph, and why.

## The file shape

The file *is* the mapping: there is no `schema_mapping:` outer key.

```yaml
nodes:
  - label: "Person"                   # graph vertex label
    source_table: "employees"         # relational table
    properties:                       # graph_property -> sql_column
      name: "first_name"
      email: "email_address"
    primary_key: "employee_id"        # SQL column

  - label: "Department"
    source_table: "departments"
    properties:
      name: "dept_name"
    primary_key: "dept_id"

edges:
  - type: "WORKS_IN"                  # graph relationship type
    source_node: "Person"             # must match a label in nodes[]
    target_node: "Department"
    source_table: "employees"
    source_foreign_key: "dept_id"     # FK in source_table
    target_primary_key: "dept_id"     # PK in target node's table
    properties:                       # optional edge properties
      since: "hire_date"
```

Loaded with `SchemaMapping.from_yaml(path)`
(`src/sql2graph/mapping.py::SchemaMapping`). Strict mode (`extra="forbid"`)
rejects unknown keys, and edge `source_node` / `target_node` are checked
against the declared node labels at load time.

## Field reference

Node fields (`src/sql2graph/mapping.py::NodeMapping`):

| Node field | Type | Required | Description |
|---|---|---|---|
| `label` | string | yes | Graph node label. Used verbatim in queries. |
| `source_table` | string | yes | Relational table. |
| `properties` | `dict[str, str]` | yes | Graph property name → SQL column. |
| `property_types` | `dict[str, SemanticType]` | no | Optional per-property semantic type (see "Typed properties" below). |
| `list_properties` | `dict[str, ListProperty]` | no | Multi-valued attributes sourced from a child table (see "List properties" below). |
| `primary_key` | string or list of strings | yes | SQL column(s) uniquely identifying rows (see "Composite keys" below). |

Edge fields (`src/sql2graph/mapping.py::EdgeMapping`):

| Edge field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | Relationship type. Used verbatim in queries. |
| `source_node` | string | yes | A node `label`. Checked at load time. |
| `target_node` | string | yes | A node `label`. Self-references allowed. |
| `source_table` | string | yes | Table containing the FK. |
| `source_foreign_key` | string or list of strings | yes | FK column(s) in `source_table`. |
| `target_primary_key` | string or list of strings | yes | PK column(s) in target node's table. |
| `properties` | `dict[str, str]` | no | Optional edge properties. |
| `property_types` | `dict[str, SemanticType]` | no | Optional per-property semantic type (see "Typed properties" below). |

## Composite keys

Every key field accepts either a scalar string or a list of columns
(`src/sql2graph/mapping.py::_as_key_list`); the scalar form is shorthand for a
one-element list. A composite key keeps all its columns so a node's identity
is not lossily reduced to one column:

```yaml
  - label: "LineItem"
    source_table: "lineitem"
    primary_key: [orderkey, linenumber]
```

On an edge, `source_foreign_key` and `target_primary_key` may both be lists
for a composite join. They are matched positionally
(`source.fk[i] = target.pk[i]`) and must have the same length; a mismatch is
rejected at load time (`src/sql2graph/mapping.py::EdgeMapping`).

## Typed properties (optional)

A property value may be written two ways. The short form maps a graph property
straight to a SQL column:

```yaml
properties:
  name: "first_name"
```

The long form additionally records a `SemanticType`, surfaced in the prompt so
the LLM does not have to guess the value's type:

```yaml
properties:
  name: {column: "first_name", type: "string"}
  joined: {column: "hire_date", type: "date"}
```

Loading normalises the long form into `properties` (`{name: column}`) plus a
parallel `property_types` (`{name: type}`); the two forms may be mixed in one
mapping. `SemanticType` (`src/sql2graph/mapping.py::SemanticType`) is one of
`string`, `integer`, `float`, `boolean`, `date`, `datetime`, `time`,
`duration`. Types are optional and best-effort: the mapping builder assigns
them where it can (see [builder.md](builder.md)), and an untyped property is
left as a bare string.

## List properties (optional)

A multi-valued attribute (a person's email addresses, the languages they
speak) is stored relationally in a dedicated child table keyed by the parent's
id. In the property graph it is a list property on the parent node, not a
separate node, so the scalar `properties` shape cannot express it. A node
declares one entry per multi-valued attribute under `list_properties`
(`src/sql2graph/mapping.py::ListProperty`), as
[`examples/mappings/ldbc.yaml`](../../examples/mappings/ldbc.yaml) does for
`Person`:

```yaml
  - label: "Person"
    source_table: "person"
    # ...scalar properties...
    list_properties:
      email:
        source_table: "person_email"   # child table holding the values
        foreign_key: "person_id"       # child column referencing the parent key
        column: "email"                # child column holding each element
        type: "string"                 # optional SemanticType of each element
```

The join is `source_table.foreign_key = <parent>.primary_key`. A graph
property name must be either scalar or a list, never both; the clash is
rejected at load time (`src/sql2graph/mapping.py::NodeMapping`).
