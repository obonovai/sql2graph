"""Async generate-validate-fix orchestrator.

Mirror of :mod:`rows2graph.translator` for callers that want non-blocking
translation — e.g. a Streamlit UI that should stay responsive during long
LLM calls, or a service that needs to handle several concurrent
translations on a single event loop.

The loop is *the same loop*: one initial generate, up to ``max_iterations``
validate/fix rounds, terminate on success or max-iterations. Only the calls
to the LLM and validator change from blocking to ``await`` ones. Iteration
numbering, event emission, the final :class:`TranslationResult` — all
identical to the sync path, by design. Code that observes events
(:mod:`rows2graph.events`) does not need to know which translator produced
them.
"""

from __future__ import annotations

import logging
from time import perf_counter
from types import TracebackType
from typing import Any, Self

from rows2graph.events import (
    CompletedEvent,
    ConversationCallback,
    EventHandler,
    FixGeneratedEvent,
    GeneratedEvent,
    MaxIterationsReachedEvent,
    StalledEvent,
    TranslationEvent,
    ValidatedEvent,
)
from rows2graph.llm import AsyncLLMClient, StreamCallback
from rows2graph.mapping import SchemaMapping
from rows2graph.preflight import (
    PreflightAction,
    build_rejected_result,
    evaluate_preflight,
)
from rows2graph.prompts import (
    build_escalation_prompt,
    build_fix_prompt,
    build_generate_prompt,
    build_system_prompt,
    error_signature,
    normalize_query,
)
from rows2graph.sql_features import analyze_sql
from rows2graph.state import TranslationResult, TranslationState
from rows2graph.targets import TargetLanguage
from rows2graph.validators import AsyncQueryValidator

logger = logging.getLogger(__name__)


class AsyncSQLTranslator:
    """Async sibling of :class:`rows2graph.translator.SQLTranslator`.

    Construct with already-instantiated async components. Call
    :meth:`translate` one or more times (each call is awaitable); the same
    translator instance reuses its LLM and validator resources across
    translations. Use as an ``async with`` context manager or call
    :meth:`close` when done.
    """

    def __init__(
        self,
        schema_mapping: SchemaMapping,
        llm: AsyncLLMClient,
        target: TargetLanguage,
        validator: AsyncQueryValidator,
        max_iterations: int = 3,
        escalation_temperature: float = 0.6,
        fix_temperature: float | None = None,
        parse_error_action: PreflightAction = PreflightAction.WARN,
        unmapped_tables_action: PreflightAction = PreflightAction.REJECT,
        unmapped_columns_action: PreflightAction = PreflightAction.WARN,
    ) -> None:
        self._schema_mapping = schema_mapping
        self._llm = llm
        self._target = target
        self._validator = validator
        self._max_iterations = max_iterations
        # See SQLTranslator.__init__ — same stall-escalation knobs, async sibling.
        self._fix_temperature = fix_temperature
        self._escalation_temperature = escalation_temperature
        # See SQLTranslator.__init__ — same input-side pre-flight policy.
        self._parse_error_action = parse_error_action
        self._unmapped_tables_action = unmapped_tables_action
        self._unmapped_columns_action = unmapped_columns_action
        # See SQLTranslator.last_messages — same contract, async sibling.
        self.last_messages: list[dict[str, str]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def translate(
        self,
        sql_query: str,
        on_event: EventHandler | None = None,
        stream_to: StreamCallback | None = None,
        on_conversation: ConversationCallback | None = None,
    ) -> TranslationResult:
        """Translate a SQL query through the full feedback loop.

        Awaitable counterpart of
        :meth:`rows2graph.translator.SQLTranslator.translate`. Same return
        type, same event semantics, same iteration numbering. Handler
        exceptions are caught and logged; they do not abort the loop.

        When ``stream_to`` is set, every LLM call in the loop (the initial
        generate and each fix) streams its text deltas through that
        callback as they arrive. The full assembled response still
        feeds the validator after each call completes. The callback is
        invoked many times per LLM call — once per text delta — and runs
        on the same task as the translator itself.

        When ``on_conversation`` is set, the handler receives a snapshot of the
        full message list each time the conversation changes — after each prompt
        and per-token while an assistant turn streams — which drives live
        conversation displays. Setting it implies streaming from the LLM even
        when ``stream_to`` is ``None``.
        """
        target_name = self._target.name
        if target_name not in ("cypher", "aql", "gremlin"):
            raise ValueError(f"Unsupported target language for TranslationState: {target_name!r}")

        # Pre-flight: parse the SQL once and decide whether to warn or reject
        # before any expensive work — see SQLTranslator.translate. A reject runs
        # before warmup so it never boots a managed database for a query we will
        # not translate.
        analysis = analyze_sql(sql_query)
        outcome = evaluate_preflight(
            analysis,
            self._schema_mapping,
            self._parse_error_action,
            self._unmapped_tables_action,
            self._unmapped_columns_action,
        )
        if outcome is not None:
            _emit(on_event, outcome.event)
            if outcome.is_reject:
                logger.info("Pre-flight rejected translation: %s", outcome.message)
                self.last_messages = []
                result = build_rejected_result(sql_query, target_name, outcome)
                _emit(on_event, CompletedEvent(result=result))
                return result

        # Provision the validator (e.g. boot a managed throwaway database) before
        # the timer starts, so duration_seconds measures only the LLM + validation
        # loop rather than one-off database setup.
        warmup = getattr(self._validator, "warmup", None)
        if callable(warmup):
            await warmup()

        start_time = perf_counter()
        state = TranslationState(
            sql_query=sql_query,
            target_language=target_name,  # type: ignore[arg-type]
        )

        features = analysis.features
        logger.info("Detected SQL features: %s", sorted(f.value for f in features))
        system_prompt = build_system_prompt(self._schema_mapping, self._target, features)
        state.messages.append(_msg("system", system_prompt))
        _emit_conversation(on_conversation, state.messages)

        user_msg = build_generate_prompt(sql_query)
        state.messages.append(_msg("user", user_msg))
        _emit_conversation(on_conversation, state.messages)

        reply = await self._llm.chat(
            state.messages, stream_to=_stream_with_conversation(state, on_conversation, stream_to)
        )
        state.token_usage = state.token_usage + reply.usage
        raw_content = reply.text
        state.messages.append(_msg("assistant", raw_content))
        _emit_conversation(on_conversation, state.messages)
        state.generated_query = self._target.extract_query(raw_content)

        logger.info("Initial query generated:\n%s", state.generated_query)
        _emit(on_event, GeneratedEvent(iteration=1, query=state.generated_query or ""))

        # See SQLTranslator.translate — same stall-detection/escalation logic.
        previous_signature: frozenset[str] | None = None
        previous_norm_query: str | None = None
        escalated = False

        while state.validation_iteration < self._max_iterations:
            state.validation_iteration += 1
            errors = await self._validator.validate(state.generated_query or "")
            state.validation_errors = errors

            if not errors:
                state.validation_passed = True
                state.final_status = "success"
                logger.info("Validation passed on iteration %d", state.validation_iteration)
                _emit(
                    on_event,
                    ValidatedEvent(
                        iteration=state.validation_iteration,
                        query=state.generated_query or "",
                        errors=[],
                        passed=True,
                    ),
                )
                break

            logger.info(
                "Validation iteration %d found %d error(s): %s",
                state.validation_iteration,
                len(errors),
                errors,
            )
            _emit(
                on_event,
                ValidatedEvent(
                    iteration=state.validation_iteration,
                    query=state.generated_query or "",
                    errors=list(errors),
                    passed=False,
                ),
            )

            if state.validation_iteration >= self._max_iterations:
                state.final_status = "max_iterations_reached"
                logger.warning(
                    "Max iterations (%d) reached with errors: %s",
                    self._max_iterations,
                    errors,
                )
                _emit(
                    on_event,
                    MaxIterationsReachedEvent(
                        iteration=state.validation_iteration,
                        errors=list(errors),
                    ),
                )
                break

            signature = error_signature(errors)
            norm_query = normalize_query(state.generated_query or "")
            no_progress = previous_signature is not None and (
                signature == previous_signature or norm_query == previous_norm_query
            )
            previous_signature = signature
            previous_norm_query = norm_query

            if no_progress and escalated:
                state.final_status = "stalled"
                logger.warning(
                    "Translation stalled (no progress after escalation) on iteration %d: %s",
                    state.validation_iteration,
                    errors,
                )
                break

            repair_hint = self._target.repair_hint(errors)

            if no_progress:
                escalated = True
                logger.info(
                    "No progress on iteration %d — escalating with a fresh context",
                    state.validation_iteration,
                )
                _emit(
                    on_event,
                    StalledEvent(
                        iteration=state.validation_iteration,
                        query=state.generated_query or "",
                        errors=list(errors),
                    ),
                )
                escalation_msg = build_escalation_prompt(
                    sql_query=sql_query,
                    generated_query=state.generated_query or "",
                    errors=errors,
                    repair_hint=repair_hint,
                )
                state.messages.append(_msg("user", escalation_msg))
                _emit_conversation(on_conversation, state.messages)
                # Re-ask from a CLEAN context (system turn + this one) so the
                # repetition-poisoned history doesn't pin the model to its
                # previous answer; the record still keeps the turn.
                reply = await self._llm.chat(
                    [state.messages[0], _msg("user", escalation_msg)],
                    stream_to=_stream_with_conversation(state, on_conversation, stream_to),
                    temperature=self._escalation_temperature,
                )
            else:
                fix_msg = build_fix_prompt(
                    sql_query=sql_query,
                    generated_query=state.generated_query or "",
                    errors=errors,
                    repair_hint=repair_hint,
                )
                state.messages.append(_msg("user", fix_msg))
                _emit_conversation(on_conversation, state.messages)
                reply = await self._llm.chat(
                    state.messages,
                    stream_to=_stream_with_conversation(state, on_conversation, stream_to),
                    temperature=self._fix_temperature,
                )

            state.token_usage = state.token_usage + reply.usage
            raw_content = reply.text
            state.messages.append(_msg("assistant", raw_content))
            _emit_conversation(on_conversation, state.messages)
            state.generated_query = self._target.extract_query(raw_content)

            logger.info("Fix iteration %d produced:\n%s", state.validation_iteration, state.generated_query)
            _emit(
                on_event,
                FixGeneratedEvent(
                    iteration=state.validation_iteration,
                    query=state.generated_query or "",
                ),
            )

        state.iterations_used = state.validation_iteration
        state.duration_seconds = perf_counter() - start_time
        self.last_messages = [{"role": str(m["role"]), "content": str(m["content"])} for m in state.messages]

        result = TranslationResult(
            sql_query=state.sql_query,
            generated_query=state.generated_query,
            target_language=state.target_language,
            validation_passed=state.validation_passed,
            validation_errors=state.validation_errors,
            iterations_used=state.iterations_used,
            status=state.final_status,
            # A surviving ``outcome`` here is a WARN (a reject returned earlier),
            # so the result stays self-describing about what the pre-flight flagged.
            unmapped_tables=list(outcome.tables) if outcome is not None else [],
            unmapped_columns=list(outcome.columns) if outcome is not None else [],
            duration_seconds=state.duration_seconds,
            token_usage=state.token_usage,
        )
        _emit(on_event, CompletedEvent(result=result))
        return result

    async def close(self) -> None:
        """Release LLM and validator resources.

        Idempotent: calling more than once is safe. Mirrors the sync
        translator's ``close()``.
        """
        try:
            await self._validator.close()
        finally:
            await self._llm.close()


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


def _snapshot(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"role": str(m["role"]), "content": str(m["content"])} for m in messages]


def _emit_conversation(handler: ConversationCallback | None, messages: list[dict[str, Any]]) -> None:
    """Invoke an ``on_conversation`` handler with a message snapshot, isolating its errors.

    Mirrors :func:`_emit`: a misbehaving handler must not abort the translation.
    """
    if handler is None:
        return
    try:
        handler(_snapshot(messages))
    except Exception:  # noqa: BLE001 — the whole point is to swallow user errors
        logger.warning("Conversation handler raised", exc_info=True)


def _stream_with_conversation(
    state: TranslationState,
    on_conversation: ConversationCallback | None,
    stream_to: StreamCallback | None,
) -> StreamCallback | None:
    """Build the per-delta stream callback that also emits live conversation snapshots.

    When ``on_conversation`` is set, the returned callback streams the assembling
    assistant turn into the snapshot (so consumers see the model "typing") and
    still forwards deltas to ``stream_to`` if the caller supplied one. When
    ``on_conversation`` is ``None`` it returns ``stream_to`` unchanged — so
    non-live callers keep their behaviour, including no streaming when
    ``stream_to`` is also ``None``.
    """
    if on_conversation is None:
        return stream_to
    parts: list[str] = []

    def _cb(delta: str) -> None:
        parts.append(delta)
        if stream_to is not None:
            stream_to(delta)
        _emit_conversation(on_conversation, [*state.messages, _msg("assistant", "".join(parts))])

    return _cb


def _emit(handler: EventHandler | None, event: TranslationEvent) -> None:
    """Invoke an event handler, isolating its exceptions from the loop.

    Sync helper even though the translator is async: event handlers in this
    codebase are sync (matching the sync translator's signature), so this
    function intentionally does not ``await`` anything. If a future caller
    wants async handlers, the Protocol and this helper would change
    together.
    """
    if handler is None:
        return
    try:
        handler(event)
    except Exception:  # noqa: BLE001
        logger.warning("Event handler raised on %s", type(event).__name__, exc_info=True)
