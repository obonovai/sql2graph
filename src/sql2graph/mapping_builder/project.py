"""Project a :class:`~sql2graph.mapping_builder.relational.RelationalSchema`
onto a :class:`~sql2graph.mapping.SchemaMapping` skeleton.

This is the deterministic core of the builder: given the extracted relational
structure it applies the canonical relational-to-property-graph rules - table
becomes node, foreign key becomes edge, association table becomes edge with
properties - and emits a mapping that is **valid by construction** (it is built
straight into the Pydantic models, so it passes every cross-field validator the
moment it is returned).

The output is intended as a reviewable first draft, not a final answer: every
non-obvious decision (a synthesized key, a dropped edge, a table that *might*
be an association) is recorded in the :class:`CoverageReport` so the user (or
the LLM pass) knows exactly what to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sql2graph.mapping import EdgeMapping, ListProperty, NodeMapping, SchemaMapping, SemanticType
from sql2graph.mapping_builder.naming import edge_type_for_fk, junction_to_edge_type, table_to_label
from sql2graph.mapping_builder.relational import ForeignKey, RelationalSchema, Table
from sql2graph.mapping_builder.sql_types import semantic_type_for_sql


@dataclass
class CoverageReport:
    """A human-readable account of how the schema was projected.

    Every list is ordered for stable, snapshot-friendly output. ``warnings``
    collects the soft issues a reviewer should look at (synthesized keys,
    foreign keys dropped for a mismatched column count, candidate association
    tables kept as nodes); ``dropped_objects`` lists things that produced nothing
    at all (views, FKs to unknown tables).
    """

    node_tables: list[str] = field(default_factory=list)
    edge_tables: list[str] = field(default_factory=list)  # junction tables collapsed to edges
    list_property_tables: list[str] = field(default_factory=list)  # child tables folded into list props
    fk_edges: list[str] = field(default_factory=list)  # "Label -[TYPE]-> Label (table.col)"
    dropped_objects: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    synthesized_keys: list[str] = field(default_factory=list)  # tables whose PK was guessed
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view, for the web API's audit panel."""
        return {
            "node_tables": list(self.node_tables),
            "edge_tables": list(self.edge_tables),
            "list_property_tables": list(self.list_property_tables),
            "fk_edges": list(self.fk_edges),
            "dropped_objects": [{"name": n, "reason": r} for n, r in self.dropped_objects],
            "synthesized_keys": list(self.synthesized_keys),
            "warnings": list(self.warnings),
            "node_count": len(self.node_tables),
            "edge_count": len(self.fk_edges),
        }


@dataclass(frozen=True)
class ProjectionResult:
    """The deterministic skeleton plus the audit explaining how it was built."""

    mapping: SchemaMapping
    report: CoverageReport


def project_to_mapping(schema: RelationalSchema) -> ProjectionResult:
    """Project *schema* into a valid :class:`SchemaMapping` skeleton + audit."""
    report = CoverageReport()
    for obj in schema.skipped_objects:
        report.dropped_objects.append((obj.name, obj.reason))

    junctions = {t.name.casefold() for t in schema.tables if is_junction_table(t, schema)}
    # Value-list child tables (person_email, person_speaks) fold into list
    # properties on their parent node rather than becoming nodes or edges; compute
    # them up front so they are excluded everywhere below.
    multivalue = {t.name.casefold() for t in schema.tables if is_multivalue_property_table(t, schema)}

    # Assign a unique label to every node (non-junction, non-multivalue, non-empty)
    # table first; edges and list properties reference these labels, so they must
    # all exist before those build.
    label_for: dict[str, str] = {}
    used_labels: set[str] = set()
    node_tables: list[Table] = []
    for t in schema.tables:
        if t.name.casefold() in junctions or t.name.casefold() in multivalue:
            continue
        if not t.columns:
            report.dropped_objects.append((t.name, "table declares no columns; cannot become a node"))
            continue
        if t.name.casefold() in label_for:
            # Two tables fold to the same bare name (e.g. sales.orders + archive.orders);
            # the mapping keys on the bare name, so the second cannot be represented.
            report.dropped_objects.append((t.name, "duplicate table name; kept the first definition as the node"))
            continue
        label = _unique_label(t.name, used_labels)
        if label != table_to_label(t.name):
            report.warnings.append(f"Label collision: '{t.name}' labelled '{label}' to stay unique")
        label_for[t.name.casefold()] = label
        used_labels.add(label)
        node_tables.append(t)

    list_props_for = _resolve_list_properties(schema, multivalue, label_for, report)
    nodes = [
        _build_node(t, label_for[t.name.casefold()], list_props_for.get(t.name.casefold(), {}), report)
        for t in node_tables
    ]

    edges: list[EdgeMapping] = []
    seen_types: dict[str, set[str]] = {}  # source_table.casefold() -> assigned types
    seen_edges: set[tuple[Any, ...]] = set()

    for t in node_tables:
        for fk in t.foreign_keys:
            _append_fk_edge(t, fk, schema, junctions, label_for, edges, report, seen_types, seen_edges)

    for t in schema.tables:
        if t.name.casefold() in junctions:
            _append_junction_edge(t, schema, label_for, edges, report, seen_types, seen_edges)

    for t in node_tables:
        if len(t.foreign_keys) == 2 and len(t.single_column_foreign_keys()) == 2:
            report.warnings.append(
                f"Table '{t.name}' has two foreign keys but its own primary key; kept as a node "
                "(it may be an association table you want to model as an edge)"
            )

    mapping = SchemaMapping(nodes=nodes, edges=edges)
    report.node_tables = [t.name for t in node_tables]
    return ProjectionResult(mapping=mapping, report=report)


def is_junction_table(table: Table, schema: RelationalSchema) -> bool:
    """Return ``True`` if *table* is a pure association table (becomes an edge).

    The predicate is strict on purpose (precision over recall): a real entity
    that merely happens to have two foreign keys is kept as a node and flagged,
    rather than silently dissolved into an edge.

    All must hold: exactly two foreign keys, both single-column and on distinct
    columns; the primary key is exactly those two FK columns; nothing else
    references this table (a referenced table must stay a node); and both
    referenced tables are present in the schema.
    """
    if len(table.foreign_keys) != 2:
        return False
    sfks = table.single_column_foreign_keys()
    if len(sfks) != 2:
        return False
    c0, c1 = sfks[0].columns[0], sfks[1].columns[0]
    if c0.casefold() == c1.casefold():
        return False
    pk = {c.casefold() for c in table.primary_key}
    if not pk or pk != {c0.casefold(), c1.casefold()}:
        return False
    if table.name.casefold() in schema.referenced_tables():
        return False
    return all(schema.table(fk.ref_table) is not None for fk in sfks)


def is_multivalue_property_table(table: Table, schema: RelationalSchema) -> bool:
    """Return ``True`` if *table* is a value-list child of a parent node.

    LDBC's multi-valued attributes live in a child table keyed entirely by the
    parent's id plus the value: ``person_email(person_id, email, PRIMARY KEY
    (person_id, email))``. Such a table has no identity of its own - it is a bag
    of values attached to one parent row - so it folds into a **list property**
    on the parent node instead of becoming its own node (the default for a
    one-foreign-key table) or an edge (which needs two).

    The predicate is strict, mirroring :func:`is_junction_table`: exactly one
    single-column foreign key to a table present in the schema; at least one
    non-foreign-key value column; nothing else references this table; and **every
    column participates in the primary key** (``pk == all columns``). That last
    clause is what separates a value-list table from a real entity that merely
    has one foreign key and a surrogate id (e.g. ``post``), whose id is *not* the
    foreign key - such a table keeps being a node.
    """
    sfks = table.single_column_foreign_keys()
    if len(table.foreign_keys) != 1 or len(sfks) != 1:
        return False
    fk = sfks[0]
    if schema.table(fk.ref_table) is None:
        return False
    if table.name.casefold() in schema.referenced_tables():
        return False
    fk_col = fk.columns[0].casefold()
    value_cols = [c for c in table.columns if c.name.casefold() != fk_col]
    if not value_cols:
        return False
    pk = {c.casefold() for c in table.primary_key}
    all_cols = {c.name.casefold() for c in table.columns}
    return bool(pk) and pk == all_cols


def is_composition_fk(table: Table, fk: ForeignKey) -> bool:
    """Return ``True`` if *fk* expresses composition (the parent owns the child).

    A composition foreign key is directed **parent -> child** in the graph (e.g.
    ``Forum -[:CONTAINER_OF]-> Post``), the reverse of the builder's default
    ``FK-holder -> referenced`` direction. Direction is otherwise *semantic*, not
    structural - a lone 1:N foreign key looks identical whether it means "belongs
    to" or "contains" - so the flip is opt-in, taken only when the schema encodes
    ownership through one of two standard signals:

      * ``ON DELETE CASCADE`` - deleting the parent deletes the child, so the
        parent owns the child's lifecycle;
      * an **identifying relationship** - the FK column is part of *table*'s
        primary key, so the child is existence-dependent on the parent (a weak
        entity, e.g. ``lineitem(orderkey, linenumber)``).

    A plain foreign key (no cascade, not in the primary key) is treated as a
    reference/association and keeps the default child -> parent direction, so an
    existing schema's edges never flip unless it declares one of these signals.
    """
    if fk.on_delete == "CASCADE":
        return True
    pk = {c.casefold() for c in table.primary_key}
    return any(c.casefold() in pk for c in fk.columns)


def choose_primary_key(table: Table) -> tuple[list[str], bool]:
    """Pick the identity columns for a node and whether they had to be synthesized.

    Returns the table's full declared primary key, so a composite key keeps all
    its columns (``lineitem(orderkey, linenumber)`` yields
    ``[orderkey, linenumber]``, the node's true identity, even though ``orderkey``
    also becomes an edge). With no declared key it falls back to the first column
    and flags the guess.
    """
    if table.primary_key:
        return (list(table.primary_key), False)
    return ([table.columns[0].name], True)


def _build_node(
    table: Table,
    label: str,
    list_properties: dict[str, ListProperty],
    report: CoverageReport,
) -> NodeMapping:
    """Build the :class:`NodeMapping` for one node table, using the resolved *label*.

    The label is passed in (not recomputed) so it stays identical to the one the
    edges reference - they share the single disambiguated ``label_for`` map.
    *list_properties* carries the multi-valued attributes folded in from child
    tables (empty for a plain single-table node).
    """
    pk_cols, synthesized = choose_primary_key(table)
    if synthesized:
        report.synthesized_keys.append(table.name)
        report.warnings.append(f"Table '{table.name}' has no primary key; using column '{pk_cols[0]}' as the node key")
    fk_cols = {c.casefold() for c in table.fk_columns()}
    # Non-FK columns become properties; every primary-key column is always included
    # too, even when it is also a foreign-key column (composite/identifying keys),
    # so the node exposes its whole identity.
    properties: dict[str, str] = {c.name: c.name for c in table.columns if c.name.casefold() not in fk_cols}
    for col in pk_cols:
        properties.setdefault(col, col)
    return NodeMapping(
        label=label,
        source_table=table.name,
        properties=properties,
        property_types=_property_types_for(table, properties),
        list_properties=list_properties,
        primary_key=pk_cols,
    )


def _resolve_list_properties(
    schema: RelationalSchema,
    multivalue: set[str],
    label_for: dict[str, str],
    report: CoverageReport,
) -> dict[str, dict[str, ListProperty]]:
    """Fold each value-list child table into list properties on its parent node.

    Returns a map ``parent_table.casefold() -> {property_name: ListProperty}``.
    A child whose parent did not become a node (e.g. an empty-column table that
    was dropped) is recorded in ``dropped_objects`` rather than attached.
    """
    out: dict[str, dict[str, ListProperty]] = {}
    for t in schema.tables:
        if t.name.casefold() not in multivalue:
            continue
        fk = t.single_column_foreign_keys()[0]
        parent = schema.table(fk.ref_table)
        if parent is None or parent.name.casefold() not in label_for:
            report.dropped_objects.append(
                (t.name, f"multi-valued attribute table references '{fk.ref_table}', which is not a node; skipped")
            )
            continue
        fk_col = fk.columns[0]
        props = out.setdefault(parent.name.casefold(), {})
        for col in t.columns:
            if col.name.casefold() == fk_col.casefold():
                continue
            name = _list_property_name(col.name, parent, props)
            props[name] = ListProperty(
                source_table=t.name,
                foreign_key=fk_col,
                column=col.name,
                type=semantic_type_for_sql(col.data_type),
            )
        report.list_property_tables.append(t.name)
    return out


def _list_property_name(column: str, parent: Table, assigned: dict[str, ListProperty]) -> str:
    """A list-property name unique against the parent's own columns and prior lists.

    The value column name is used as-is (``email``, ``language``); on the rare
    clash with a scalar column of the parent or an already-assigned list, the
    child value column disambiguates, keeping every graph property name distinct
    (the ``NodeMapping`` validator forbids a name being both scalar and list).
    """
    taken = {c.name.casefold() for c in parent.columns} | {a.casefold() for a in assigned}
    if column.casefold() not in taken:
        return column
    candidate = f"{column}_{parent.name}"
    i = 2
    while candidate.casefold() in taken:
        candidate = f"{column}_{i}"
        i += 1
    return candidate


def _property_types_for(table: Table, properties: dict[str, str]) -> dict[str, SemanticType]:
    """Derive a :class:`SemanticType` per property from its source column's SQL type.

    Keyed by the graph property name; the type is read from the mapped SQL column
    (the property *value*). Columns whose declared SQL type does not resolve to a
    known family - or that declared none - are omitted, leaving that property
    untyped rather than guessed. Foreign-key/join columns never reach here: they
    are excluded from ``properties`` upstream (they become edges, not stored
    values), so they correctly carry no type.
    """
    by_name = {c.name: c for c in table.columns}
    out: dict[str, SemanticType] = {}
    for prop_name, column_name in properties.items():
        column = by_name.get(column_name)
        if column is None:
            continue
        semantic = semantic_type_for_sql(column.data_type)
        if semantic is not None:
            out[prop_name] = semantic
    return out


def _append_fk_edge(
    table: Table,
    fk: ForeignKey,
    schema: RelationalSchema,
    junctions: set[str],
    label_for: dict[str, str],
    edges: list[EdgeMapping],
    report: CoverageReport,
    seen_types: dict[str, set[str]],
    seen_edges: set[tuple[Any, ...]],
) -> None:
    """Emit (or skip, with a reason) the edge for one foreign key of a node table."""
    target = schema.table(fk.ref_table)
    if target is None:
        report.dropped_objects.append(
            (f"{table.name}.{fk.columns[0]}", f"foreign key references unknown table '{fk.ref_table}'; edge dropped")
        )
        return
    target_key = target.name.casefold()
    if target_key in junctions or target_key not in label_for:
        report.dropped_objects.append(
            (
                f"{table.name}.{fk.columns[0]}",
                f"foreign key references '{fk.ref_table}', which is modeled as an edge; edge dropped",
            )
        )
        return
    fk_holder_label = label_for[table.name.casefold()]
    referenced_label = label_for[target_key]
    # Composite keys are represented in full: emit every local column paired with
    # the referenced column (or the target's full primary key when the DDL omitted
    # the referenced column list). A join that cannot be positionally paired is
    # dropped rather than silently collapsed to its first column.
    source_fk = list(fk.columns)
    target_pk = list(fk.ref_columns) if fk.ref_columns else choose_primary_key(target)[0]
    if len(source_fk) != len(target_pk):
        report.warnings.append(
            f"Foreign key {table.name}({', '.join(source_fk)}) references "
            f"{target.name}({', '.join(target_pk)}) with mismatched column count; edge dropped"
        )
        report.dropped_objects.append(
            (
                f"{table.name}.{','.join(source_fk)}",
                "composite foreign key column count does not match the referenced key; edge dropped",
            )
        )
        return

    # Composition flips the labeled direction to parent -> child; the join columns
    # (source_table / source_foreign_key / target_primary_key) are unchanged. The
    # relationship's grammatical object drives the mechanical name, so a reversed
    # edge reads "parent HAS_<child>" instead of "child HAS_<parent>".
    composition = is_composition_fk(table, fk)
    if composition:
        source_label, target_label, naming_target = referenced_label, fk_holder_label, fk_holder_label
    else:
        source_label, target_label, naming_target = fk_holder_label, referenced_label, referenced_label
    edge_type = _unique_type(table.name, edge_type_for_fk(fk, target_label=naming_target), naming_target, seen_types)

    edge = EdgeMapping(
        type=edge_type,
        source_node=source_label,
        target_node=target_label,
        source_table=table.name,
        source_foreign_key=source_fk,
        target_primary_key=target_pk,
    )
    description = f"{source_label} -[{edge_type}]-> {target_label} ({table.name}.{','.join(source_fk)})"
    if composition:
        reason = "ON DELETE CASCADE" if fk.on_delete == "CASCADE" else "identifying foreign key in the primary key"
        description += f" [reversed to parent->child: composition via {reason}]"
        report.warnings.append(
            f"Edge '{edge_type}' {source_label}->{target_label} directed parent->child (composition: {reason})"
        )
    _add_edge(edge, edges, report, seen_edges, description)


def _append_junction_edge(
    table: Table,
    schema: RelationalSchema,
    label_for: dict[str, str],
    edges: list[EdgeMapping],
    report: CoverageReport,
    seen_types: dict[str, set[str]],
    seen_edges: set[tuple[Any, ...]],
) -> None:
    """Emit the single edge that a junction table collapses to."""
    f0, f1 = table.single_column_foreign_keys()
    a = schema.table(f0.ref_table)
    b = schema.table(f1.ref_table)
    if a is None or b is None:
        return  # guarded by is_junction_table, but keep types honest
    if a.name.casefold() not in label_for or b.name.casefold() not in label_for:
        # is_junction_table guarantees both tables exist, not that they became
        # nodes (an empty-column table is dropped); without both endpoints there
        # is no edge to build.
        report.dropped_objects.append(
            (table.name, "junction references a table that is not a node (no columns to project); edge dropped")
        )
        return
    source_label = label_for[a.name.casefold()]
    target_label = label_for[b.name.casefold()]
    fk_cols = {c.casefold() for c in table.fk_columns()}
    properties = {c.name: c.name for c in table.columns if c.name.casefold() not in fk_cols}
    source_fk = [f1.columns[0]]
    target_pk = list(f1.ref_columns) if f1.ref_columns else choose_primary_key(b)[0]
    if len(source_fk) != len(target_pk):
        # The junction's own foreign key is single-column; a composite referenced
        # key cannot be paired against it, so drop rather than collapse the target.
        report.dropped_objects.append(
            (table.name, "junction foreign key column count does not match the referenced key; edge dropped")
        )
        return
    edge_type = _unique_type(table.name, junction_to_edge_type(table.name), target_label, seen_types)

    edge = EdgeMapping(
        type=edge_type,
        source_node=source_label,
        target_node=target_label,
        source_table=table.name,
        source_foreign_key=source_fk,
        target_primary_key=target_pk,
        properties=properties,
        property_types=_property_types_for(table, properties),
    )
    report.edge_tables.append(table.name)
    _add_edge(
        edge, edges, report, seen_edges, f"{source_label} -[{edge_type}]-> {target_label} ({table.name}, junction)"
    )


def _add_edge(
    edge: EdgeMapping,
    edges: list[EdgeMapping],
    report: CoverageReport,
    seen_edges: set[tuple[Any, ...]],
    description: str,
) -> None:
    """Append *edge* unless an identical one already exists (the validator forbids dupes)."""
    key = (
        edge.type,
        edge.source_node,
        edge.target_node,
        edge.source_table,
        tuple(edge.source_foreign_key),
        tuple(edge.target_primary_key),
    )
    if key in seen_edges:
        return
    seen_edges.add(key)
    edges.append(edge)
    report.fk_edges.append(description)


def _unique_label(table_name: str, used: set[str]) -> str:
    """A node label unique among *used* (suffixing on collision)."""
    base = table_to_label(table_name)
    if base not in used:
        return base
    i = 2
    while f"{base}{i}" in used:
        i += 1
    return f"{base}{i}"


def _unique_type(source_table: str, base_type: str, target_label: str, seen_types: dict[str, set[str]]) -> str:
    """A relationship type unique within *source_table* (disambiguating on collision)."""
    assigned = seen_types.setdefault(source_table.casefold(), set())
    candidate = base_type
    if candidate in assigned:
        candidate = f"{base_type}_{target_label.upper()}"
    i = 2
    while candidate in assigned:
        candidate = f"{base_type}_{i}"
        i += 1
    assigned.add(candidate)
    return candidate
