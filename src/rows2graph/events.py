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
    GeneratedEvent
    | ValidatedEvent
    | FixGeneratedEvent
    | MaxIterationsReachedEvent
    | CompletedEvent
)
"""Discriminated union of every event the translator emits."""


EventHandler = Callable[[TranslationEvent], None]
"""Type alias for the ``on_event`` callback signature."""


__all__ = [
    "CompletedEvent",
    "EventHandler",
    "FixGeneratedEvent",
    "GeneratedEvent",
    "MaxIterationsReachedEvent",
    "TranslationEvent",
    "ValidatedEvent",
]
