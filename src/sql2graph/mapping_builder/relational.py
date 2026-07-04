"""A dependency-free intermediate representation (IR) of a relational schema.

The mapping builder works in two clearly separated phases: first *extract* the
structure of a relational schema into the value types defined here, then
*project* that structure onto a :class:`~sql2graph.mapping.SchemaMapping`. This
module is the seam between the two.

Keeping the IR free of any parser or database dependency is deliberate. Today
the only producer is :func:`sql2graph.mapping_builder.ddl.extract_schema_from_ddl`
(sqlglot over ``CREATE TABLE`` text), but the projection heuristics in
:mod:`sql2graph.mapping_builder.project` never touch sqlglot - they consume only
these dataclasses. A future second source (live-database introspection reading
``information_schema``, or SQLAlchemy reflection) can populate the very same
:class:`RelationalSchema` and reuse every downstream heuristic unchanged.

The types mirror the style of :class:`sql2graph.sql_features.SqlAnalysis`:
frozen dataclasses, tuples rather than lists so instances stay hashable and
obviously immutable, and identifiers stored with their original casing (all
*comparisons* casefold, mirroring :mod:`sql2graph.preflight`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    """One relational column.

    Attributes:
        name: Column identifier, with the casing exactly as written in the DDL.
        data_type: The rendered SQL type (e.g. ``"VARCHAR(25)"``), purely
            informational - the builder never reasons about types. ``None`` when
            the source did not declare one.
        nullable: ``False`` only when a ``NOT NULL`` constraint was declared;
            defaults to ``True`` otherwise (the SQL default).
    """

    name: str
    data_type: str | None = None
    nullable: bool = True


@dataclass(frozen=True)
class ForeignKey:
    """A foreign-key constraint: ``columns`` reference ``ref_table(ref_columns)``.

    Attributes:
        columns: Local FK columns in declaration order. A single-element tuple
            for the common single-column FK; longer for a composite FK.
        ref_table: The bare referenced table name (schema qualifier dropped).
        ref_columns: Referenced columns, parallel to ``columns``. Empty when the
            DDL omitted them (``REFERENCES t`` with no column list), in which
            case the projection falls back to the referenced table's primary key.
        name: The constraint name if one was declared, else ``None``.
        on_delete: The upper-cased ``ON DELETE`` referential action if the DDL
            declared one (e.g. ``"CASCADE"``, ``"RESTRICT"``, ``"SET NULL"``),
            else ``None``. ``CASCADE`` signals composition (the parent owns the
            child), which the projection uses to direct the edge parent -> child.
    """

    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...] = ()
    name: str | None = None
    on_delete: str | None = None


@dataclass(frozen=True)
class Table:
    """One relational table and the structure the builder reasons about.

    Attributes:
        name: Bare table name, casing as written.
        schema: The schema/catalog qualifier if the DDL gave one (e.g.
            ``public``), kept for reference but never used in comparisons.
        columns: Declared columns in declaration order.
        primary_key: Primary-key columns in order; empty when none was declared.
        foreign_keys: Every foreign key, inline and table-level, merged.
    """

    name: str
    schema: str | None
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKey, ...]

    def column_names(self) -> list[str]:
        """Column names in declaration order (original casing)."""
        return [c.name for c in self.columns]

    def fk_columns(self) -> set[str]:
        """Every column (casing as written) that participates in any foreign key.

        Used by the projection to exclude join columns from node *properties*:
        a foreign key becomes an edge, not a stored property.
        """
        return {col for fk in self.foreign_keys for col in fk.columns}

    def single_column_foreign_keys(self) -> list[ForeignKey]:
        """The subset of foreign keys that span exactly one column.

        These are the only foreign keys the builder turns into edges directly;
        composite foreign keys cannot be expressed by the single-column
        ``EdgeMapping`` join and are handled (with a warning) elsewhere.
        """
        return [fk for fk in self.foreign_keys if len(fk.columns) == 1]


@dataclass(frozen=True)
class SkippedObject:
    """A ``CREATE`` statement the source produced that is not a base table.

    Carried so the audit can tell the user *why* something in their DDL did not
    become a node (most commonly a ``CREATE VIEW``), rather than silently
    omitting it.
    """

    name: str
    kind: str  # e.g. "view", "index" - the lower-cased CREATE kind
    reason: str


@dataclass(frozen=True)
class RelationalSchema:
    """A whole relational schema: an ordered collection of :class:`Table`.

    Lookups are case-insensitive (relational engines fold case differently and a
    DDL may not match the eventual mapping's casing), while the stored names keep
    their original casing for faithful output and messages.

    ``skipped_objects`` records ``CREATE`` statements the source saw but could
    not represent as a base table; it is defaulted so a producer that doesn't
    track them (e.g. a future introspection source) need not populate it.
    """

    tables: tuple[Table, ...]
    skipped_objects: tuple[SkippedObject, ...] = ()

    def table(self, name: str) -> Table | None:
        """Return the table whose bare name casefold-matches *name*, or ``None``."""
        key = name.casefold()
        for t in self.tables:
            if t.name.casefold() == key:
                return t
        return None

    def table_names(self) -> set[str]:
        """The set of bare table names, with original casing."""
        return {t.name for t in self.tables}

    def referenced_tables(self) -> set[str]:
        """Casefolded names of every table targeted by some foreign key.

        A table in this set is the *parent* of at least one relationship, so it
        must remain a node (it cannot be collapsed into an edge).
        """
        return {fk.ref_table.casefold() for t in self.tables for fk in t.foreign_keys}
