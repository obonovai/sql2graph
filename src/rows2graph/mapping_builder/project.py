"""Project a :class:`~rows2graph.mapping_builder.relational.RelationalSchema`
onto a :class:`~rows2graph.mapping.SchemaMapping` skeleton.

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

from rows2graph.mapping import EdgeMapping, NodeMapping, SchemaMapping, SemanticType
from rows2graph.mapping_builder.naming import edge_type_for_fk, junction_to_edge_type, table_to_label
from rows2graph.mapping_builder.relational import ForeignKey, RelationalSchema, Table
from rows2graph.mapping_builder.sql_types import semantic_type_for_sql


@dataclass
class CoverageReport:
    """A human-readable account of how the schema was projected.

    Every list is ordered for stable, snapshot-friendly output. ``warnings``
    collects the soft issues a reviewer should look at (synthesized keys,
    collapsed composite foreign keys, candidate association tables kept as
    nodes); ``dropped_objects`` lists things that produced nothing at all
    (views, FKs to unknown tables).
    """

    node_tables: list[str] = field(default_factory=list)
    edge_tables: list[str] = field(default_factory=list)  # junction tables collapsed to edges
    fk_edges: list[str] = field(default_factory=list)  # "Label -[TYPE]-> Label (table.col)"
    dropped_objects: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    synthesized_keys: list[str] = field(default_factory=list)  # tables whose PK was guessed
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view, for the web API's audit panel."""
        return {
            "node_tables": list(self.node_tables),
            "edge_tables": list(self.edge_tables),
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

    # Assign a unique label to every node (non-junction, non-empty) table first;
    # edges reference these labels, so they must all exist before edges build.
    label_for: dict[str, str] = {}
    used_labels: set[str] = set()
    node_tables: list[Table] = []
    for t in schema.tables:
        if t.name.casefold() in junctions:
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

    nodes = [_build_node(t, label_for[t.name.casefold()], report) for t in node_tables]

    edges: list[EdgeMapping] = []
    seen_types: dict[str, set[str]] = {}  # source_table.casefold() -> assigned types
    seen_edges: set[tuple[str, ...]] = set()

    for t in node_tables:
        for fk in t.foreign_keys:
            _append_fk_edge(
                t, fk, schema, junctions, label_for, edges, report, seen_types, seen_edges
            )

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


def choose_primary_key(table: Table) -> tuple[str, bool]:
    """Pick the identity column for a node and whether it had to be synthesized.

    Prefers the non-foreign-key column of a declared primary key (so a composite
    PK like ``lineitem(orderkey, linenumber)`` yields ``linenumber``, since
    ``orderkey`` is a foreign key that becomes an edge). With no declared key it
    falls back to the first column and flags the guess.
    """
    fk_cols = {c.casefold() for c in table.fk_columns()}
    if table.primary_key:
        non_fk = [c for c in table.primary_key if c.casefold() not in fk_cols]
        return (non_fk[0] if non_fk else table.primary_key[0], False)
    return (table.columns[0].name, True)


def _build_node(table: Table, label: str, report: CoverageReport) -> NodeMapping:
    """Build the :class:`NodeMapping` for one node table, using the resolved *label*.

    The label is passed in (not recomputed) so it stays identical to the one the
    edges reference - they share the single disambiguated ``label_for`` map.
    """
    pk_col, synthesized = choose_primary_key(table)
    if synthesized:
        report.synthesized_keys.append(table.name)
        report.warnings.append(
            f"Table '{table.name}' has no primary key; using column '{pk_col}' as the node key"
        )
    fk_cols = {c.casefold() for c in table.fk_columns()}
    # Non-FK columns become properties; the chosen key is always included even if
    # it happens to also be a foreign-key column (composite-key tables).
    properties: dict[str, str] = {c.name: c.name for c in table.columns if c.name.casefold() not in fk_cols}
    properties.setdefault(pk_col, pk_col)
    return NodeMapping(
        label=label,
        source_table=table.name,
        properties=properties,
        property_types=_property_types_for(table, properties),
        primary_key=pk_col,
    )


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
    seen_edges: set[tuple[str, ...]],
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
            (f"{table.name}.{fk.columns[0]}", f"foreign key references '{fk.ref_table}', which is modeled as an edge; edge dropped")
        )
        return
    if len(fk.columns) > 1:
        report.warnings.append(
            f"Composite foreign key {table.name}({', '.join(fk.columns)}) collapsed to its first column '{fk.columns[0]}'"
        )

    source_label = label_for[table.name.casefold()]
    target_label = label_for[target_key]
    target_pk = fk.ref_columns[0] if fk.ref_columns else choose_primary_key(target)[0]
    edge_type = _unique_type(table.name, edge_type_for_fk(fk, target_label=target_label), target_label, seen_types)

    edge = EdgeMapping(
        type=edge_type,
        source_node=source_label,
        target_node=target_label,
        source_table=table.name,
        source_foreign_key=fk.columns[0],
        target_primary_key=target_pk,
    )
    _add_edge(edge, edges, report, seen_edges, f"{source_label} -[{edge_type}]-> {target_label} ({table.name}.{fk.columns[0]})")


def _append_junction_edge(
    table: Table,
    schema: RelationalSchema,
    label_for: dict[str, str],
    edges: list[EdgeMapping],
    report: CoverageReport,
    seen_types: dict[str, set[str]],
    seen_edges: set[tuple[str, ...]],
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
    target_pk = f1.ref_columns[0] if f1.ref_columns else choose_primary_key(b)[0]
    edge_type = _unique_type(table.name, junction_to_edge_type(table.name), target_label, seen_types)

    edge = EdgeMapping(
        type=edge_type,
        source_node=source_label,
        target_node=target_label,
        source_table=table.name,
        source_foreign_key=f1.columns[0],
        target_primary_key=target_pk,
        properties=properties,
        property_types=_property_types_for(table, properties),
    )
    report.edge_tables.append(table.name)
    _add_edge(edge, edges, report, seen_edges, f"{source_label} -[{edge_type}]-> {target_label} ({table.name}, junction)")


def _add_edge(
    edge: EdgeMapping,
    edges: list[EdgeMapping],
    report: CoverageReport,
    seen_edges: set[tuple[str, ...]],
    description: str,
) -> None:
    """Append *edge* unless an identical one already exists (the validator forbids dupes)."""
    key = (
        edge.type,
        edge.source_node,
        edge.target_node,
        edge.source_table,
        edge.source_foreign_key,
        edge.target_primary_key,
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
