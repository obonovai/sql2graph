"""Learn what we can from parsing a SQL query once: which operations it uses
and which tables it reads.

The system prompt assembled by :mod:`rows2graph.prompts` is the largest
per-translation input the LLM sees. Historically every translation received
the full rule set for the target language, including, for example, Cypher's
14-line ``LIKE``/``ILIKE`` mapping table even on queries with no string
predicates. This module turns that into an opt-in: parse the SQL once with
:mod:`sqlglot`, derive a :class:`SqlFeature` set, and let the target language
emit only the chunks corresponding to features actually present.

:func:`analyze_sql` is the single entry point: it parses once and returns a
:class:`SqlAnalysis` carrying the feature set, the real source tables, and a
``parse_ok`` flag. :func:`detect_features` is a thin delegate kept for callers
(and the prompt builder) that only want the features.

On any parser failure :func:`detect_features` returns :data:`ALL_FEATURES`,
which restores the pre-refactor behaviour ("ship every rule"). That fallback
is load-bearing: a silently-stripped rule would be a regression in
translation quality, while a few extra tokens on an unparseable query is
harmless. The ``parse_ok`` flag on :class:`SqlAnalysis` is how a caller that
*does* care about parseability (e.g. a pre-flight check) learns the parse
failed without disturbing that prompt-trimming fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import sqlglot
import sqlglot.errors
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


class SqlFeature(StrEnum):
    """A SQL operation cluster that has its own per-target rule chunk."""

    LIKE = "like"
    JOIN = "join"
    AGGREGATION = "aggregation"
    ORDER_LIMIT = "order_limit"
    CTE = "cte"
    UNION = "union"
    WINDOW = "window"
    CASE = "case"
    SUBQUERY = "subquery"
    DISTINCT = "distinct"
    TEMPORAL = "temporal"


ALL_FEATURES: frozenset[SqlFeature] = frozenset(SqlFeature)


@dataclass(frozen=True)
class SqlAnalysis:
    """Everything a single parse of a SQL query tells us.

    Attributes:
        features: The :class:`SqlFeature` set driving prompt assembly. Equals
            :data:`ALL_FEATURES` when the parse failed, preserving the
            "ship every rule" fallback that :func:`detect_features` relies on.
        source_tables: The real relational tables the query reads, with their
            casing as written in the SQL. CTE names and derived-table aliases
            are excluded (see :func:`_extract_source_tables`). Empty when the
            parse failed or the query reads no tables (``SELECT 1``, DML/DDL).
        column_refs: ``(table, column)`` pairs the query references, resolved to
            their real source table where unambiguous (see
            :func:`_extract_column_refs`). Casing is as written. Empty when the
            parse failed; columns that cannot be attributed to a table (e.g.
            unqualified columns in a multi-table query) are omitted.
        parse_ok: ``False`` iff :func:`sqlglot.parse_one` raised
            :class:`sqlglot.errors.ParseError`.
    """

    features: frozenset[SqlFeature]
    source_tables: frozenset[str]
    parse_ok: bool
    column_refs: frozenset[tuple[str, str]] = frozenset()

# An ISO-8601-ish date or timestamp string literal: ``'YYYY-MM-DD'`` optionally
# followed by a space- or ``T``-separated ``hh:mm`` / ``hh:mm:ss``. Matches the
# date literals SQL queries compare against (e.g. ``shipdate >= '1995-03-01'``);
# does NOT match identifiers, codes like ``'12-34-5678'``, or phone strings.
_DATE_LITERAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")

# sqlglot ``DataType.Type`` members whose name contains DATE or TIME: every
# temporal cast target (DATE, TIMESTAMP, DATETIME, TIME, TIMESTAMPTZ, …).
_TEMPORAL_CAST_TYPES = frozenset(t for t in exp.DataType.Type if "DATE" in t.name or "TIME" in t.name)


def analyze_sql(sql_query: str, *, dialect: str | None = None) -> SqlAnalysis:
    """Parse *sql_query* once and report its features, tables, and parse status.

    The dialect argument is passed through to :func:`sqlglot.parse_one` (e.g.
    ``"postgres"``, ``"mysql"``) when the caller knows it; ``None`` lets
    sqlglot use its dialect-neutral default, which is fine for the standard
    SQL the library targets.

    On parser failure returns ``SqlAnalysis(ALL_FEATURES, frozenset(),
    parse_ok=False)``, the full rule set (so the prompt is never
    silently-trimmed) paired with ``parse_ok=False`` so a caller that cares can
    notice the failure.
    """
    try:
        tree = sqlglot.parse_one(sql_query, read=dialect)
    except sqlglot.errors.ParseError:
        return SqlAnalysis(features=ALL_FEATURES, source_tables=frozenset(), parse_ok=False)

    found = _detect_features(tree)
    return SqlAnalysis(
        features=found,
        source_tables=_extract_source_tables(tree),
        parse_ok=True,
        column_refs=_extract_column_refs(tree),
    )


def detect_features(sql_query: str, *, dialect: str | None = None) -> frozenset[SqlFeature]:
    """Return the set of :class:`SqlFeature` values present in *sql_query*.

    Thin delegate over :func:`analyze_sql`; preserved as the entry point for
    callers (notably :func:`rows2graph.prompts.build_system_prompt`) that only
    need the feature set. Returns :data:`ALL_FEATURES` on parser failure so an
    unparseable query still receives the full rule set rather than a
    silently-trimmed one.
    """
    return analyze_sql(sql_query, dialect=dialect).features


def _extract_source_tables(tree: Any) -> frozenset[str]:
    """Return the real relational tables *tree* reads, with as-written casing.

    Uses sqlglot's scope resolver rather than a raw ``find_all(exp.Table)``:
    the latter reports CTE names and derived-table aliases as "tables" (a
    ``WITH recent AS (...) SELECT ... FROM recent`` would falsely surface
    ``recent``), whereas :func:`traverse_scope` enumerates only genuine table
    sources. Only the bare identifier (``exp.Table.name``) is kept: schema /
    catalog qualifiers are dropped so ``public.orders`` compares as ``orders``
    against a schema-agnostic mapping.

    Fails *open*: ``traverse_scope`` runs the optimizer's scope builder, which
    can raise on exotic-but-parseable input. An empty set makes any downstream
    coverage check a no-op, which is strictly safer than turning a translation
    into a brand-new crash.
    """
    try:
        return frozenset(
            src.name
            for scope in traverse_scope(tree)
            for src in scope.sources.values()
            if isinstance(src, exp.Table)
        )
    except Exception:  # noqa: BLE001 (table extraction must never crash a translation)
        return frozenset()


def _extract_column_refs(tree: Any) -> frozenset[tuple[str, str]]:
    """Return ``(table, column)`` pairs the query references, where attributable.

    Built on the same scope resolver as :func:`_extract_source_tables`. A column
    is attributed to a real table in two cases:

    * **Qualified** (``f.title``): resolve the qualifier through
      ``scope.sources``; keep it only when that source is a genuine
      :class:`~sqlglot.expressions.Table` (so a CTE/derived-table alias is
      excluded, and a SELECT alias like ``member_count`` never resolves).
    * **Unqualified** (``title``): attributed to the sole table *only* in a
      "leaf" scope: exactly one table source and no child sub-scopes. The
      leaf gate matters: an outer single-source scope would otherwise absorb a
      nested subquery's unqualified column (e.g. ``... WHERE id IN (SELECT
      person_id FROM knows)`` would mis-attribute ``person_id`` to the outer
      table). Multi-source scopes drop unqualified columns entirely (an
      under-count, never a false attribution).

    Only the bare table identifier is kept (schema qualifier dropped), matching
    :func:`_extract_source_tables`. Fails *open*; see that function for why an
    empty result is the safe degradation.
    """
    try:
        refs: set[tuple[str, str]] = set()
        for scope in traverse_scope(tree):
            table_sources = {alias: src for alias, src in scope.sources.items() if isinstance(src, exp.Table)}
            has_child_scopes = bool(
                scope.subquery_scopes or scope.derived_table_scopes or scope.cte_scopes or scope.union_scopes
            )
            sole_table = next(iter(table_sources.values())) if len(table_sources) == 1 else None
            for col in scope.columns:
                if col.table:
                    src = scope.sources.get(col.table)
                    if isinstance(src, exp.Table):
                        refs.add((src.name, col.name))
                elif sole_table is not None and not has_child_scopes:
                    refs.add((sole_table.name, col.name))
        return frozenset(refs)
    except Exception:  # noqa: BLE001 (column extraction must never crash a translation)
        return frozenset()


def _detect_features(tree: Any) -> frozenset[SqlFeature]:
    """Derive the :class:`SqlFeature` set from an already-parsed AST."""
    found: set[SqlFeature] = set()

    if _any(tree, exp.Like) or _any(tree, exp.ILike):
        found.add(SqlFeature.LIKE)
    if _any(tree, exp.Join):
        found.add(SqlFeature.JOIN)
    if _any(tree, exp.Group) or _any(tree, exp.Having) or _any(tree, exp.AggFunc):
        found.add(SqlFeature.AGGREGATION)
    if _any(tree, exp.Order) or _any(tree, exp.Limit) or _any(tree, exp.Offset):
        found.add(SqlFeature.ORDER_LIMIT)
    if _any(tree, exp.CTE):
        found.add(SqlFeature.CTE)
    if _any(tree, exp.Union) or _any(tree, exp.Intersect) or _any(tree, exp.Except):
        found.add(SqlFeature.UNION)
    if _any(tree, exp.Window):
        found.add(SqlFeature.WINDOW)
    if _any(tree, exp.Case):
        found.add(SqlFeature.CASE)
    # exp.Subquery wraps scalar/IN/FROM nested SELECTs but not CTEs, so the
    # CTE-only case correctly does not light up SUBQUERY here.
    if _any(tree, exp.Subquery) or _any(tree, exp.Exists):
        found.add(SqlFeature.SUBQUERY)
    if _any(tree, exp.Distinct):
        found.add(SqlFeature.DISTINCT)
    if _has_temporal(tree):
        found.add(SqlFeature.TEMPORAL)

    return frozenset(found)


def _any(tree: Any, node_type: type[Any]) -> bool:
    """Return ``True`` if any node of *node_type* exists anywhere under *tree*.

    Typed with :class:`Any` because sqlglot's ``AggFunc`` and ``Func`` are not
    declared as :class:`~sqlglot.expressions.Expression` subclasses in its
    type stubs, even though they are at runtime; strict typing here would
    block legitimate calls.
    """
    return next(iter(tree.find_all(node_type)), None) is not None


def _has_temporal(tree: Any) -> bool:
    """Return ``True`` if the query compares against a date/timestamp value.

    Three signals, any of which is sufficient:
      * a string literal shaped like an ISO date/timestamp
        (``'1995-03-01'``, ``'2010-06-01T12:30:00'``);
      * an explicit cast to a temporal type (``CAST(x AS DATE)``,
        ``DATE '2020-01-01'``, which sqlglot parses as a cast to DATE);
      * a ``CURRENT_DATE`` / ``CURRENT_TIMESTAMP`` builtin.

    Integer/identifier comparisons (``suppkey = 1337``) and ordinary string
    equality (``name = 'Supplier#000000666'``) produce none of these, so the
    detector stays quiet on plain selects.
    """
    if any(lit.is_string and _DATE_LITERAL_RE.match(lit.this) for lit in tree.find_all(exp.Literal)):
        return True
    if any(
        isinstance(cast.to, exp.DataType) and cast.to.this in _TEMPORAL_CAST_TYPES for cast in tree.find_all(exp.Cast)
    ):
        return True
    if next(iter(tree.find_all(exp.CurrentDate)), None) is not None:
        return True
    if next(iter(tree.find_all(exp.CurrentTimestamp)), None) is not None:
        return True
    return False
