"""Map a source SQL column type onto a normalized :class:`SemanticType`.

The DDL parser already renders each column's SQL type onto
:attr:`sql2graph.mapping_builder.relational.Column.data_type` (e.g.
``"TIMESTAMP"``, ``"DECIMAL(15,2)"``). This module collapses that dialect-noisy
string down to the small, loader-agnostic vocabulary the translator can actually
use in a prompt, so the projection can annotate a property with, say,
``datetime`` instead of forcing the LLM to guess a temporal column's type from a
literal's shape.

The mapping is best-effort: a type string that does not resolve to one of the
known families (``UUID``, ``JSON``, an array, a vendor type) returns ``None`` and
the property is simply left untyped - and, like every derived name in the
builder, the result stays overridable by hand in the emitted YAML. This is the
only builder module besides :mod:`sql2graph.mapping_builder.ddl` that depends on
sqlglot, which is why :class:`SemanticType` itself lives in the dependency-light
:mod:`sql2graph.mapping`.
"""

from __future__ import annotations

from sqlglot import exp

from sql2graph.mapping import SemanticType

# Partition sqlglot's temporal family: a bare calendar date, a time-of-day, and
# everything else with a time component (TIMESTAMP*, DATETIME*, SMALLDATETIME).
# INTERVAL is a duration and is NOT in TEMPORAL_TYPES, so it is listed on its own.
_T = exp.DataType.Type
_DATE_TYPES = {_T.DATE, _T.DATE32}
_TIME_TYPES = {_T.TIME, _T.TIMETZ}
_DATETIME_TYPES = exp.DataType.TEMPORAL_TYPES - _DATE_TYPES - _TIME_TYPES
_DURATION_TYPES = {_T.INTERVAL}
_BOOLEAN_TYPES = {_T.BOOLEAN}  # BOOLEAN is in none of sqlglot's numeric/text sets


def semantic_type_for_sql(data_type: str | None) -> SemanticType | None:
    """Return the :class:`SemanticType` for a rendered SQL type, or ``None``.

    ``data_type`` is the string on :attr:`Column.data_type` (already produced by
    sqlglot's ``.sql()``), so it round-trips cleanly through
    :meth:`sqlglot.exp.DataType.build`. Integers are checked before reals since
    the two families are disjoint; anything outside the known families (UUID,
    JSON, arrays, unresolved vendor types) yields ``None``.
    """
    if not data_type:
        return None
    try:
        this = exp.DataType.build(data_type).this
    except Exception:  # noqa: BLE001 (an exotic/unparseable type is simply left untyped)
        return None
    if this in _DATE_TYPES:
        return SemanticType.DATE
    if this in _TIME_TYPES:
        return SemanticType.TIME
    if this in _DATETIME_TYPES:
        return SemanticType.DATETIME
    if this in _DURATION_TYPES:
        return SemanticType.DURATION
    if this in _BOOLEAN_TYPES:
        return SemanticType.BOOLEAN
    if this in exp.DataType.INTEGER_TYPES:
        return SemanticType.INTEGER
    if this in exp.DataType.REAL_TYPES:
        return SemanticType.FLOAT
    if this in exp.DataType.TEXT_TYPES:
        return SemanticType.STRING
    return None
