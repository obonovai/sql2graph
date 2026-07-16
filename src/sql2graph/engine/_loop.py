"""Shared, side-effect-free helpers for the sync and async translators.

The sync (:mod:`sql2graph.engine.translator`) and async
(:mod:`sql2graph.engine.async_translator`) orchestrators run the *same*
generate-validate-fix loop; only their LLM/validator calls differ (blocking vs
awaited). To keep the two ``translate()`` methods provably parallel *without*
re-typing their non-IO scaffolding, the pieces both paths share verbatim live
here: chat-message construction, the immutable conversation snapshot,
exception-isolated event emission, and final-result assembly.

Deliberately narrow: only the IO-free, control-flow-free fragments move. The two
``while`` loops themselves stay in their own modules, byte-for-byte mirrors of
each other, because that readable duplication is the property the codebase
optimises for (see ``docs/architecture.md``).
"""

from __future__ import annotations

import logging
from typing import Any

from sql2graph.engine.events import EventHandler, TranslationEvent
from sql2graph.engine.preflight import PreflightOutcome
from sql2graph.engine.state import TranslationResult, TranslationState

logger = logging.getLogger(__name__)


def to_message(role: str, content: str) -> dict[str, Any]:
    """Build one chat message in the shape the LLM clients consume."""
    return {"role": role, "content": content}


def snapshot_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return a plain ``{"role", "content"}`` copy of the running message list.

    The loop mutates ``state.messages`` in place, so ``last_messages`` and the
    live ``on_conversation`` snapshots hand consumers a stringified copy rather
    than an alias into that mutable list.
    """
    return [{"role": str(m["role"]), "content": str(m["content"])} for m in messages]


def emit_event(handler: EventHandler | None, event: TranslationEvent) -> None:
    """Invoke an event handler, isolating its exceptions from the loop.

    A misbehaving handler must not abort the user's translation; the exception is
    logged at WARNING and the loop continues. Event handlers are synchronous in
    both translators, so this helper never awaits; the handler owns its own
    thread/coroutine safety.
    """
    if handler is None:
        return
    try:
        handler(event)
    except Exception:  # noqa: BLE001 (the whole point is to swallow user errors)
        logger.warning("Event handler raised on %s", type(event).__name__, exc_info=True)


def build_result(state: TranslationState, outcome: PreflightOutcome | None) -> TranslationResult:
    """Assemble the public :class:`TranslationResult` from the final loop state.

    Shared verbatim by both translators' completion path. A surviving *outcome*
    here is always a WARN (a reject returns earlier), so its flagged tables /
    columns are echoed onto the result, keeping it self-describing about what the
    pre-flight noticed.
    """
    return TranslationResult(
        sql_query=state.sql_query,
        generated_query=state.generated_query,
        target_language=state.target_language,
        validation_passed=state.validation_passed,
        validation_errors=state.validation_errors,
        iterations_used=state.iterations_used,
        status=state.final_status,
        unmapped_tables=list(outcome.tables) if outcome is not None else [],
        unmapped_columns=list(outcome.columns) if outcome is not None else [],
        duration_seconds=state.duration_seconds,
        token_usage=state.token_usage,
    )
