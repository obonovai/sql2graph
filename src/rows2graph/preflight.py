"""Input-side pre-flight checks run before the generate-validate-fix loop.

The translator validates its *output* (the generated graph query) but,
historically, nothing about its *input*. Three cheap checks catch inputs that
cannot translate well:

* **Parse failure** — :func:`rows2graph.sql_features.analyze_sql` could not
  parse the SQL. A weak-but-useful signal (sqlglot is dialect-flexible, so a
  valid-but-exotic query can also fail), hence the default action is to *warn*
  and translate anyway rather than reject.
* **Unmapped tables** — the SQL reads tables that have no node/edge in the
  :class:`~rows2graph.mapping.SchemaMapping`. A strong signal: with no mapping
  the LLM has nothing to translate those tables *to*, so the default action is
  to *reject* and skip the (wasted) LLM call.
* **Unmapped columns** — the SQL uses a column of a *mapped* table that the
  mapping doesn't expose as a property/key. A softer signal (the table maps, so
  the LLM can often still produce a useful query by dropping/approximating the
  column), and column attribution has more residual false-positive surface, so
  the default action is to *warn*.

This module owns the *policy* (what each :class:`PreflightAction` does) and the
single home for table/column-name normalization (:func:`find_unmapped_tables`,
:func:`find_unmapped_columns`). The translators own only the plumbing: run
:func:`evaluate_preflight`, emit the returned event, and on a reject build a
terminal :class:`~rows2graph.state.TranslationResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from rows2graph.events import ParseFailedEvent, UnmappedColumnsEvent, UnmappedTablesEvent
from rows2graph.mapping import SchemaMapping
from rows2graph.sql_features import SqlAnalysis
from rows2graph.state import TranslationResult

# Terminal ``TranslationResult.status`` strings emitted on the reject path.
PARSE_ERROR_STATUS = "parse_error"
UNMAPPED_TABLES_STATUS = "unmapped_tables"
UNMAPPED_COLUMNS_STATUS = "unmapped_columns"


class PreflightAction(StrEnum):
    """What a pre-flight signal does to a translation.

    * ``IGNORE`` — do nothing; reproduces the pre-pre-flight behaviour.
    * ``WARN`` — emit the signal's event, then translate anyway.
    * ``REJECT`` — emit the signal's event, skip the LLM, and return a terminal
      result carrying the signal's status.
    """

    IGNORE = "ignore"
    WARN = "warn"
    REJECT = "reject"


@dataclass(frozen=True)
class PreflightOutcome:
    """The decision :func:`evaluate_preflight` reached for one translation.

    Attributes:
        event: The event the translator should emit (a :class:`ParseFailedEvent`,
            :class:`UnmappedTablesEvent`, or :class:`UnmappedColumnsEvent`).
        is_reject: When ``True`` the translator must skip the LLM and return a
            terminal result; when ``False`` it emits ``event`` and proceeds.
        status: Terminal ``TranslationResult.status`` to use on a reject. Only
            meaningful when ``is_reject`` is ``True``.
        message: Human-readable explanation (also used as the single
            ``validation_errors`` entry on a reject).
        tables: Offending source tables for the unmapped-tables signal; empty
            otherwise.
        columns: Offending ``"table.column"`` strings for the unmapped-columns
            signal; empty otherwise.
    """

    event: ParseFailedEvent | UnmappedTablesEvent | UnmappedColumnsEvent
    is_reject: bool
    status: str
    message: str
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)


def find_unmapped_tables(sql_tables: frozenset[str], mapping: SchemaMapping) -> list[str]:
    """Return the SQL source tables not covered by *mapping*, case-insensitively.

    Comparison is by :meth:`str.casefold` (mappings are conventionally
    lowercase; SQL may not be), but the returned names keep their original SQL
    casing so an error message echoes what the user wrote. Sorted for a stable,
    snapshot-friendly order.
    """
    covered = {t.casefold() for t in mapping.source_tables()}
    return sorted(t for t in sql_tables if t.casefold() not in covered)


def find_unmapped_columns(column_refs: frozenset[tuple[str, str]], mapping: SchemaMapping) -> list[str]:
    """Return ``"table.column"`` refs whose column is missing from *mapping*.

    Only columns of **node source-tables** are checkable: pure junction/edge
    tables are skipped because the mapping stores just one of a junction's
    foreign keys, so a strict check would false-flag the *other* legitimate join
    key. For a checkable table the covered SQL columns are its node property
    values and primary key, plus — for a table that *also* backs an edge — that
    edge's property values and its join keys (``source_foreign_key`` /
    ``target_primary_key``). The edge union is a leniency valve: it keeps a
    node-table's FK/PK join columns from being flagged.

    Comparison is :meth:`str.casefold` (mappings are conventionally lowercase),
    but the returned strings keep the SQL's original casing. Sorted + deduped.
    """
    node_tables = {n.source_table.casefold() for n in mapping.nodes}
    if not node_tables:
        return []
    covered: dict[str, set[str]] = {t: set() for t in node_tables}
    for n in mapping.nodes:
        t = n.source_table.casefold()
        covered[t].update(c.casefold() for c in n.properties.values())
        covered[t].add(n.primary_key.casefold())
    for e in mapping.edges:
        t = e.source_table.casefold()
        if t in covered:  # only matters when the edge's table is also a node source
            covered[t].update(c.casefold() for c in e.properties.values())
            covered[t].add(e.source_foreign_key.casefold())
            covered[t].add(e.target_primary_key.casefold())
    flagged = {
        f"{table}.{column}"
        for table, column in column_refs
        if table.casefold() in node_tables and column.casefold() not in covered[table.casefold()]
    }
    return sorted(flagged)


def parse_error_message(*, rejected: bool) -> str:
    """Human-readable explanation for a parse-failure signal."""
    tail = "translation was skipped." if rejected else "attempting translation anyway."
    return (
        "The SQL query could not be parsed — it may be malformed or use syntax "
        f"sqlglot does not recognise; {tail}"
    )


def unmapped_tables_message(tables: list[str], *, rejected: bool) -> str:
    """Human-readable explanation for an unmapped-tables signal."""
    listed = ", ".join(tables)
    tail = (
        "Translation was skipped because these tables have no node or edge in the schema mapping."
        if rejected
        else "These tables have no node or edge in the schema mapping; attempting translation anyway."
    )
    return f"The SQL reads table(s) absent from the schema mapping: {listed}. {tail}"


def unmapped_columns_message(columns: list[str], *, rejected: bool) -> str:
    """Human-readable explanation for an unmapped-columns signal."""
    listed = ", ".join(columns)
    tail = (
        "Translation was skipped because these columns are not exposed by the schema mapping."
        if rejected
        else "These columns are not exposed by the schema mapping; attempting translation anyway."
    )
    return f"The SQL uses column(s) of mapped tables that the mapping does not define: {listed}. {tail}"


def evaluate_preflight(
    analysis: SqlAnalysis,
    mapping: SchemaMapping,
    parse_error_action: PreflightAction,
    unmapped_tables_action: PreflightAction,
    unmapped_columns_action: PreflightAction,
) -> PreflightOutcome | None:
    """Decide the pre-flight outcome for one translation, or ``None`` to proceed.

    Signals are checked in order — parse → unmapped tables → unmapped columns —
    and at most one fires. Parse is first because a query that did not parse has
    no reliable table/column set. The column check runs only when no table is
    unmapped: if some table is missing entirely, that is the more fundamental
    problem and its signal returns first. (Should ``unmapped_tables_action`` be
    ``IGNORE`` while a table is genuinely unmapped, control still falls through
    to the column check — safe, because that check only inspects node tables.)
    """
    if not analysis.parse_ok:
        if parse_error_action is PreflightAction.IGNORE:
            return None
        rejected = parse_error_action is PreflightAction.REJECT
        message = parse_error_message(rejected=rejected)
        return PreflightOutcome(
            event=ParseFailedEvent(message=message),
            is_reject=rejected,
            status=PARSE_ERROR_STATUS,
            message=message,
        )

    if unmapped_tables_action is not PreflightAction.IGNORE:
        unmapped = find_unmapped_tables(analysis.source_tables, mapping)
        if unmapped:
            rejected = unmapped_tables_action is PreflightAction.REJECT
            message = unmapped_tables_message(unmapped, rejected=rejected)
            return PreflightOutcome(
                event=UnmappedTablesEvent(tables=unmapped, message=message),
                is_reject=rejected,
                status=UNMAPPED_TABLES_STATUS,
                message=message,
                tables=unmapped,
            )

    if unmapped_columns_action is not PreflightAction.IGNORE:
        columns = find_unmapped_columns(analysis.column_refs, mapping)
        if columns:
            rejected = unmapped_columns_action is PreflightAction.REJECT
            message = unmapped_columns_message(columns, rejected=rejected)
            return PreflightOutcome(
                event=UnmappedColumnsEvent(columns=columns, message=message),
                is_reject=rejected,
                status=UNMAPPED_COLUMNS_STATUS,
                message=message,
                columns=columns,
            )

    return None


def build_rejected_result(sql_query: str, target_language: str, outcome: PreflightOutcome) -> TranslationResult:
    """Build the terminal result returned when a pre-flight check rejects the input.

    No LLM ran, so the query is ``None``, no iterations were used, and token
    usage stays at its zero default. The signal's message becomes the single
    ``validation_errors`` entry (mirroring how ``max_iterations_reached``
    surfaces its errors, so existing consumers render it for free).
    """
    return TranslationResult(
        sql_query=sql_query,
        generated_query=None,
        target_language=target_language,  # type: ignore[arg-type]  # validated by the caller
        validation_passed=False,
        validation_errors=[outcome.message],
        iterations_used=0,
        status=outcome.status,
        unmapped_tables=list(outcome.tables),
        unmapped_columns=list(outcome.columns),
    )


__all__ = [
    "PARSE_ERROR_STATUS",
    "UNMAPPED_COLUMNS_STATUS",
    "UNMAPPED_TABLES_STATUS",
    "PreflightAction",
    "PreflightOutcome",
    "build_rejected_result",
    "evaluate_preflight",
    "find_unmapped_columns",
    "find_unmapped_tables",
    "parse_error_message",
    "unmapped_columns_message",
    "unmapped_tables_message",
]
