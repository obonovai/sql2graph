"""Typed events emitted by the translation loop.

The :meth:`~rows2graph.translator.SQLTranslator.translate` method accepts an
optional ``on_event`` callback that fires at every milestone of the
generate-validate-fix loop. This gives consumers — notably the Streamlit UI
— a structured, decoupled hook for live progress display, replacing the
older pattern of subscribing to log records and matching message strings.

Events are immutable :func:`dataclasses.dataclass` instances. The
:data:`TranslationEvent` union is the contract callers should dispatch on,
typically via a ``match`` statement::

    def on_event(event: TranslationEvent) -> None:
        match event:
            case GeneratedEvent(iteration, query):
                print(f"iter {iteration}: generated {query!r}")
            case ValidatedEvent(iteration, _, errors, passed):
                print(f"iter {iteration}: passed={passed} errors={errors}")
            case FixGeneratedEvent(iteration, query):
                print(f"iter {iteration}: fix → {query!r}")
            case MaxIterationsReachedEvent(iteration, errors):
                print(f"gave up at iter {iteration}: {errors}")
            case CompletedEvent(result):
                print(f"done: status={result.status}")

Iteration numbering matches the existing log lines: ``iteration`` is the
1-based validation-pass counter. ``FixGeneratedEvent(iteration=N)`` means
"the fix produced after iteration N's validation failed"; the resulting
candidate is validated as iteration N+1.

Exceptions raised by a handler do not abort the translation — they are
caught, logged at WARNING, and the loop continues.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rows2graph.state import TranslationResult


@dataclass(frozen=True)
class ParseFailedEvent:
    """The input SQL could not be parsed by sqlglot.

    Emitted at most once, before :class:`GeneratedEvent`, when the translator's
    ``parse_error_action`` surfaces a parse failure. Under the default
    ``"warn"`` action the translation still proceeds (the LLM is given a chance
    on the unparseable text); under ``"reject"`` this is followed directly by
    :class:`CompletedEvent` with ``status='parse_error'`` and no LLM call.
    ``message`` is a human-readable explanation.
    """

    message: str


@dataclass(frozen=True)
class UnmappedTablesEvent:
    """The input SQL reads tables absent from the schema mapping.

    Emitted at most once, before :class:`CompletedEvent`, when the translator's
    ``unmapped_tables_action`` fires. ``tables`` lists the offending source
    tables (as written in the SQL). Under the default ``"reject"`` action the
    LLM is skipped and the following :class:`CompletedEvent` carries
    ``status='unmapped_tables'``; under ``"warn"`` the translation proceeds.
    """

    tables: list[str]
    message: str


@dataclass(frozen=True)
class UnmappedColumnsEvent:
    """The input SQL uses columns of mapped tables that the mapping omits.

    Emitted at most once, before :class:`CompletedEvent`, when the translator's
    ``unmapped_columns_action`` fires. ``columns`` lists the offending
    ``"table.column"`` references. Under the default ``"reject"`` action the LLM
    is skipped and the following :class:`CompletedEvent` carries
    ``status='unmapped_columns'``; under ``"warn"`` the translation proceeds.
    """

    columns: list[str]
    message: str


@dataclass(frozen=True)
class GeneratedEvent:
    """Initial query was generated. ``iteration`` is always 1.

    Emitted once per translation, immediately after the first LLM call.
    The query will be validated next; expect a subsequent
    :class:`ValidatedEvent` with ``iteration=1``.
    """

    iteration: int
    query: str


@dataclass(frozen=True)
class ValidatedEvent:
    """A validation pass completed at the given iteration.

    ``passed`` mirrors ``len(errors) == 0`` for convenience. On
    ``passed=True`` no further events fire except :class:`CompletedEvent`.
    On ``passed=False`` either :class:`FixGeneratedEvent` or
    :class:`MaxIterationsReachedEvent` follows.
    """

    iteration: int
    query: str
    errors: list[str]
    passed: bool


@dataclass(frozen=True)
class FixGeneratedEvent:
    """A fix attempt produced a new query candidate.

    Emitted after the LLM produces a fix in response to iteration N's
    validation failure. The resulting candidate will be validated as
    iteration N+1.
    """

    iteration: int
    query: str


@dataclass(frozen=True)
class StalledEvent:
    """The loop detected no progress and is escalating with a fresh-context retry.

    Emitted at most once per translation, the moment the fix loop notices that a
    candidate repeated its predecessor or drew the *same* validator error twice
    running. The next LLM call discards the accumulated (poisoned) history and
    re-asks from a clean context at a higher temperature; ``query`` and
    ``errors`` are the stuck candidate and the error that triggered escalation.
    If that escalated attempt still makes no progress the translation ends with
    ``status='stalled'``.
    """

    iteration: int
    query: str
    errors: list[str]


@dataclass(frozen=True)
class MaxIterationsReachedEvent:
    """The loop terminated at iteration N without passing validation.

    ``errors`` is the validation error list from the final failed
    iteration. A :class:`CompletedEvent` follows with the final result
    (``status='max_iterations_reached'``).
    """

    iteration: int
    errors: list[str]


@dataclass(frozen=True)
class CompletedEvent:
    """Final event of a translation, always emitted last.

    Carries the same :class:`~rows2graph.state.TranslationResult` that
    :meth:`~rows2graph.translator.SQLTranslator.translate` returns.
    """

    result: TranslationResult


TranslationEvent = (
    ParseFailedEvent
    | UnmappedTablesEvent
    | UnmappedColumnsEvent
    | GeneratedEvent
    | ValidatedEvent
    | FixGeneratedEvent
    | StalledEvent
    | MaxIterationsReachedEvent
    | CompletedEvent
)
"""Discriminated union of every event the translator emits."""


EventHandler = Callable[[TranslationEvent], None]
"""Type alias for the ``on_event`` callback signature."""


ConversationCallback = Callable[[list[dict[str, str]]], None]
"""Signature for the ``on_conversation`` callback.

Receives a snapshot of the full system↔LLM message list (``{"role", "content"}``
dicts) each time the conversation changes — after each prompt is appended and
per-token while an assistant turn streams. Consumers typically re-render the
snapshot for a live display.
"""


__all__ = [
    "CompletedEvent",
    "ConversationCallback",
    "EventHandler",
    "FixGeneratedEvent",
    "GeneratedEvent",
    "MaxIterationsReachedEvent",
    "ParseFailedEvent",
    "StalledEvent",
    "TranslationEvent",
    "UnmappedColumnsEvent",
    "UnmappedTablesEvent",
    "ValidatedEvent",
]
