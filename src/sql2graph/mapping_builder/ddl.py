"""Extract a :class:`~sql2graph.mapping_builder.relational.RelationalSchema`
from ``CREATE TABLE`` DDL text, using sqlglot.

This is the *one* schema source the builder ships today. It is deliberately the
only module in the package that imports sqlglot: it walks the parse tree and
converts every relevant node into the dependency-free IR, so the projection
heuristics downstream never see an ``exp`` node. sqlglot expression nodes are
typed :class:`Any` here, the same boundary convention
:mod:`sql2graph.sql_features` uses (sqlglot's stubs are loose), while the IR
that crosses every other module boundary is fully typed.

sqlglot already ships as a dependency (it backs SQL feature detection and the
pre-flight checks), so DDL-based mapping construction adds no new runtime
requirement.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import sqlglot
import sqlglot.errors
from sqlglot import exp

# ``REFERENCES t(c) ON DELETE CASCADE`` -> sqlglot keeps the action as a string in
# ``Reference.args["options"]`` (e.g. ``["ON DELETE CASCADE"]``). This matches the
# supported referential actions; the projection only reasons about ``CASCADE``.
_ON_DELETE_RE = re.compile(r"ON\s+DELETE\s+(CASCADE|RESTRICT|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION)", re.IGNORECASE)

from sql2graph.mapping_builder.relational import (
    Column,
    ForeignKey,
    RelationalSchema,
    SkippedObject,
    Table,
)


class DdlParseError(ValueError):
    """Raised when sqlglot cannot parse the supplied DDL.

    A subclass of :class:`ValueError` so callers that already funnel value
    errors into a user-facing message (the CLI's ``_die``, the web API's
    ``HTTPException(400)``) handle it without a new branch.
    """


def extract_schema_from_ddl(ddl: str, *, dialect: str | None = None) -> RelationalSchema:
    """Parse *ddl* and return the relational structure it declares.

    Every ``CREATE TABLE`` statement becomes a :class:`Table`. ``ALTER TABLE ...
    ADD CONSTRAINT`` statements (the canonical pg_dump / migration style of
    declaring keys *after* the tables) are merged into the table they target, so
    primary and foreign keys added that way are not lost. Anything that cannot
    become a base table - views, indexes, a ``CREATE TABLE ... AS SELECT`` with no
    column list, a duplicate table name, an ALTER against an unknown table - is
    skipped but recorded in :attr:`RelationalSchema.skipped_objects` so the audit
    can always explain it. The ``dialect`` is passed straight to
    :func:`sqlglot.parse` (e.g. ``"postgres"``, ``"mysql"``); ``None`` uses
    sqlglot's dialect-neutral default.

    Raises:
        DdlParseError: if sqlglot cannot parse the DDL (any
            :class:`sqlglot.errors.SqlglotError`, including tokenizer errors).
    """
    try:
        statements = sqlglot.parse(ddl, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise DdlParseError(f"Could not parse the SQL DDL: {exc}") from exc

    tables: list[Table] = []
    index: dict[str, int] = {}  # casefolded bare name -> position in tables
    skipped: list[SkippedObject] = []
    alter_pks: list[tuple[str, tuple[str, ...]]] = []
    alter_fks: list[tuple[str, ForeignKey]] = []

    for stmt in statements:
        if stmt is None:
            continue
        if isinstance(stmt, exp.Create):
            _handle_create(stmt, tables=tables, index=index, skipped=skipped)
        elif isinstance(stmt, exp.Alter):
            _collect_alter_constraints(stmt, alter_pks=alter_pks, alter_fks=alter_fks)

    _merge_alter_constraints(tables, index, skipped, alter_pks=alter_pks, alter_fks=alter_fks)
    return RelationalSchema(tables=tuple(tables), skipped_objects=tuple(skipped))


def _handle_create(
    stmt: Any,
    *,
    tables: list[Table],
    index: dict[str, int],
    skipped: list[SkippedObject],
) -> None:
    """Route one ``CREATE`` statement to a table or a recorded skip."""
    kind = (stmt.args.get("kind") or "").lower()
    if kind != "table":
        name = _create_object_name(stmt)
        label = kind or type(stmt).__name__.lower()
        skipped.append(
            SkippedObject(
                name=name,
                kind=label,
                reason=f"{label or 'object'} is not a base table; only CREATE TABLE becomes a node",
            )
        )
        return
    table = _table_from_create(stmt)
    if table is None:
        skipped.append(
            SkippedObject(
                name=_create_object_name(stmt),
                kind="table",
                reason="CREATE TABLE without a column list (e.g. AS SELECT); no columns to project",
            )
        )
        return
    key = table.name.casefold()
    if key in index:
        skipped.append(
            SkippedObject(
                name=table.name,
                kind="table",
                reason="duplicate table name (schema-qualified names collapse to the bare name); kept the first definition",
            )
        )
        return
    index[key] = len(tables)
    tables.append(table)


def _collect_alter_constraints(
    stmt: Any,
    *,
    alter_pks: list[tuple[str, tuple[str, ...]]],
    alter_fks: list[tuple[str, ForeignKey]],
) -> None:
    """Harvest ``ADD CONSTRAINT`` primary/foreign keys from one ``ALTER TABLE``.

    The merge is deferred (these are collected, then applied in
    :func:`_merge_alter_constraints`) so an ALTER may legally precede the table's
    ``CREATE`` in the batch.
    """
    target = stmt.this
    table_name = getattr(target, "name", None)
    if not isinstance(table_name, str) or not table_name:
        return
    for action in stmt.args.get("actions") or []:
        for fk_node in action.find_all(exp.ForeignKey):
            parent = fk_node.parent
            name = parent.name if isinstance(parent, exp.Constraint) else None
            fk = _foreign_key_from_node(fk_node, name=name or None)
            if fk is not None:
                alter_fks.append((table_name, fk))
        for pk_node in action.find_all(exp.PrimaryKey):
            cols = tuple(e.name for e in pk_node.expressions)
            if cols:
                alter_pks.append((table_name, cols))


def _merge_alter_constraints(
    tables: list[Table],
    index: dict[str, int],
    skipped: list[SkippedObject],
    *,
    alter_pks: list[tuple[str, tuple[str, ...]]],
    alter_fks: list[tuple[str, ForeignKey]],
) -> None:
    """Fold ALTER-collected keys into their tables, recording orphans as skips."""
    for table_name, pk_cols in alter_pks:
        i = index.get(table_name.casefold())
        if i is None:
            skipped.append(
                SkippedObject(
                    name=table_name,
                    kind="constraint",
                    reason="ALTER TABLE adds a primary key to a table not defined in this DDL; constraint skipped",
                )
            )
            continue
        # First primary-key declaration wins, mirroring _set_primary_key.
        if not tables[i].primary_key:
            tables[i] = replace(tables[i], primary_key=pk_cols)
    for table_name, fk in alter_fks:
        i = index.get(table_name.casefold())
        if i is None:
            skipped.append(
                SkippedObject(
                    name=table_name,
                    kind="constraint",
                    reason="ALTER TABLE adds a foreign key to a table not defined in this DDL; constraint skipped",
                )
            )
            continue
        tables[i] = replace(tables[i], foreign_keys=tables[i].foreign_keys + (fk,))


def _create_object_name(stmt: Any) -> str:
    """Best-effort name of any ``CREATE`` object, for skip reporting."""
    target = stmt.find(exp.Table) or stmt.this
    name = getattr(target, "name", None)
    return name if isinstance(name, str) and name else "<unnamed>"


def _table_from_create(stmt: Any) -> Table | None:
    """Build a :class:`Table` from a ``CREATE TABLE`` statement, or ``None``.

    Returns ``None`` for a ``CREATE TABLE ... AS SELECT`` (or any form whose
    body is not a column-definition ``Schema``): there is no declared column
    list to project.
    """
    tbl_node = stmt.find(exp.Table)
    if tbl_node is None:
        return None
    body = stmt.this
    if not isinstance(body, exp.Schema):
        return None

    columns: list[Column] = []
    pk_columns: list[str] = []
    fks: list[ForeignKey] = []

    for child in body.expressions:
        _dispatch_body_child(child, columns=columns, pk_columns=pk_columns, fks=fks)

    return Table(
        name=tbl_node.name,
        schema=tbl_node.db or None,
        columns=tuple(columns),
        primary_key=tuple(pk_columns),
        foreign_keys=tuple(fks),
    )


def _dispatch_body_child(
    child: Any,
    *,
    columns: list[Column],
    pk_columns: list[str],
    fks: list[ForeignKey],
) -> None:
    """Route one child of the table body to the right accumulator.

    Table bodies hold column definitions and table-level constraints; a *named*
    constraint (``CONSTRAINT fk_x FOREIGN KEY ...``) wraps the real constraint
    in an :class:`exp.Constraint`, so we recurse into its children.
    """
    if isinstance(child, exp.ColumnDef):
        columns.append(_column_from_def(child, pk_columns=pk_columns, fks=fks))
    elif isinstance(child, exp.PrimaryKey):
        _set_primary_key(pk_columns, [e.name for e in child.expressions])
    elif isinstance(child, exp.ForeignKey):
        fk = _foreign_key_from_node(child)
        if fk is not None:
            fks.append(fk)
    elif isinstance(child, exp.Constraint):
        constraint_name = child.name or None
        for inner in child.expressions:
            if isinstance(inner, exp.PrimaryKey):
                _set_primary_key(pk_columns, [e.name for e in inner.expressions])
            elif isinstance(inner, exp.ForeignKey):
                fk = _foreign_key_from_node(inner, name=constraint_name)
                if fk is not None:
                    fks.append(fk)


def _column_from_def(
    col: Any,
    *,
    pk_columns: list[str],
    fks: list[ForeignKey],
) -> Column:
    """Build a :class:`Column`, harvesting inline PK / FK / NOT NULL constraints."""
    data_type_node = col.args.get("kind")
    data_type = data_type_node.sql() if data_type_node is not None else None
    nullable = True
    for constraint in col.args.get("constraints") or []:
        kind = getattr(constraint, "kind", None)
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            _set_primary_key(pk_columns, [col.name])
        elif isinstance(kind, exp.NotNullColumnConstraint):
            nullable = False
        elif isinstance(kind, exp.Reference):
            fk = _foreign_key_from_reference(kind, local_columns=[col.name])
            if fk is not None:
                fks.append(fk)
    return Column(name=col.name, data_type=data_type, nullable=nullable)


def _set_primary_key(pk_columns: list[str], cols: list[str]) -> None:
    """Record primary-key columns, keeping the first declaration if repeated.

    An inline ``PRIMARY KEY`` and a table-level ``PRIMARY KEY (...)`` for the
    same table can coexist in malformed DDL; the first one wins rather than
    appending duplicates.
    """
    if not pk_columns:
        pk_columns.extend(cols)


def _foreign_key_from_node(fk_node: Any, *, name: str | None = None) -> ForeignKey | None:
    """Build a :class:`ForeignKey` from a table-level ``exp.ForeignKey``."""
    local = [e.name for e in fk_node.expressions]
    reference = fk_node.args.get("reference")
    if reference is None or not local:
        return None
    return _foreign_key_from_reference(reference, local_columns=local, name=name)


def _foreign_key_from_reference(
    reference: Any, *, local_columns: list[str], name: str | None = None
) -> ForeignKey | None:
    """Build a :class:`ForeignKey` from an ``exp.Reference`` plus its local columns.

    The reference target lives under ``reference.this``: usually an
    :class:`exp.Schema` whose ``this`` is the referenced :class:`exp.Table` and
    whose ``expressions`` are the referenced columns; for a bare ``REFERENCES t``
    it is the :class:`exp.Table` directly (no column list).
    """
    target = reference.this
    if isinstance(target, exp.Schema):
        ref_table_node = target.this
        ref_columns = [e.name for e in target.expressions]
    else:
        ref_table_node = target
        ref_columns = []
    if not isinstance(ref_table_node, exp.Table):
        return None
    return ForeignKey(
        columns=tuple(local_columns),
        ref_table=ref_table_node.name,
        ref_columns=tuple(ref_columns),
        name=name,
        on_delete=_on_delete_action(reference),
    )


def _on_delete_action(reference: Any) -> str | None:
    """Return the upper-cased ``ON DELETE`` action of a reference, or ``None``.

    sqlglot stores referential actions as strings in ``Reference.args["options"]``
    (``["ON DELETE CASCADE"]``); older/other builds may store expression nodes, so
    each option is coerced to text before matching. Whitespace inside a multi-word
    action (``SET NULL``) is normalized to a single space.
    """
    for option in reference.args.get("options") or []:
        text = option if isinstance(option, str) else option.sql()
        match = _ON_DELETE_RE.search(text)
        if match:
            return re.sub(r"\s+", " ", match.group(1).upper())
    return None
