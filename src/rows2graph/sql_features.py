"""Detect which SQL operations a query uses, so the system prompt can ship
only the rules that apply.

The system prompt assembled by :mod:`rows2graph.prompts` is the largest
per-translation input the LLM sees. Historically every translation received
the full rule set for the target language — including, for example, Cypher's
14-line ``LIKE``/``ILIKE`` mapping table even on queries with no string
predicates. This module turns that into an opt-in: parse the SQL once with
:mod:`sqlglot`, derive a :class:`SqlFeature` set, and let the target language
emit only the chunks corresponding to features actually present.

On any parser failure :func:`detect_features` returns :data:`ALL_FEATURES`,
which restores the pre-refactor behaviour ("ship every rule"). That fallback
is load-bearing — a silently-stripped rule would be a regression in
translation quality, while a few extra tokens on an unparseable query is
harmless.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

import sqlglot
import sqlglot.errors
from sqlglot import exp


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

# An ISO-8601-ish date or timestamp string literal: ``'YYYY-MM-DD'`` optionally
# followed by a space- or ``T``-separated ``hh:mm`` / ``hh:mm:ss``. Matches the
# date literals SQL queries compare against (e.g. ``shipdate >= '1995-03-01'``);
# does NOT match identifiers, codes like ``'12-34-5678'``, or phone strings.
_DATE_LITERAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")

# sqlglot ``DataType.Type`` members whose name contains DATE or TIME — every
# temporal cast target (DATE, TIMESTAMP, DATETIME, TIME, TIMESTAMPTZ, …).
_TEMPORAL_CAST_TYPES = frozenset(t for t in exp.DataType.Type if "DATE" in t.name or "TIME" in t.name)


def detect_features(sql_query: str, *, dialect: str | None = None) -> frozenset[SqlFeature]:
    """Return the set of :class:`SqlFeature` values present in *sql_query*.

    The dialect argument is passed through to :func:`sqlglot.parse_one` (e.g.
    ``"postgres"``, ``"mysql"``) when the caller knows it; ``None`` lets
    sqlglot use its dialect-neutral default, which is fine for the standard
    SQL the library targets.

    Returns :data:`ALL_FEATURES` on parser failure so an unparseable query
    still receives the full rule set rather than a silently-trimmed one.
    """
    try:
        tree = sqlglot.parse_one(sql_query, read=dialect)
    except sqlglot.errors.ParseError:
        return ALL_FEATURES

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
    type stubs, even though they are at runtime — strict typing here would
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
