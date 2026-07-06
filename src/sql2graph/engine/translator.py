"""Generate-validate-fix orchestrator.

This module defines :class:`SQLTranslator`, the public orchestrator that
runs the framework's core feedback loop:

1. *Generate*: build a system prompt (schema mapping + target-language
   rules), send the SQL query as a user turn, extract the candidate query
   from the LLM's response.
2. *Validate*: pass the candidate to the configured
   :class:`~sql2graph.validators.QueryValidator`. If it returns an empty
   error list, the translation succeeded.
3. *Fix*: otherwise, append a fix prompt naming the errors, call the LLM
   again, and repeat. The loop terminates after
   ``max_iterations`` validate calls (counting the initial one), or
   earlier on success.

The class is deliberately small (about a single screen of orchestration)
and takes its dependencies (mapping, LLM, target, validator) as
constructor arguments rather than reading them from a configuration blob.
This makes each piece independently testable and lets callers wire bespoke
combinations (e.g. an in-memory fake LLM against a real syntax validator
for fast unit tests).
"""

from __future__ import annotations

import logging
from time import perf_counter
from types import TracebackType
from typing import Self

from sql2graph.engine._loop import build_result, emit_event, snapshot_messages, to_message
from sql2graph.engine.events import (
    CompletedEvent,
    EventHandler,
    FixGeneratedEvent,
    GeneratedEvent,
    MaxIterationsReachedEvent,
    StalledEvent,
    ValidatedEvent,
)
from sql2graph.engine.preflight import (
    PreflightAction,
    build_rejected_result,
    evaluate_preflight,
)
from sql2graph.engine.prompts import (
    build_escalation_prompt,
    build_fix_prompt,
    build_generate_prompt,
    build_system_prompt,
    error_signature,
    normalize_query,
)
from sql2graph.engine.state import TranslationResult, TranslationState
from sql2graph.llm import LLMClient
from sql2graph.mapping import SchemaMapping
from sql2graph.sql_features import analyze_sql
from sql2graph.targets import TargetLanguage
from sql2graph.validators import QueryValidator

logger = logging.getLogger(__name__)


class SQLTranslator:
    """The generate-validate-fix orchestrator.

    Construct with already-instantiated components. Call :meth:`translate`
    one or more times; the same translator instance reuses its LLM and
    validator resources across translations. Call :meth:`close` (or use the
    object as a context manager) when done.
    """

    def __init__(
        self,
        schema_mapping: SchemaMapping,
        llm: LLMClient,
        target: TargetLanguage,
        validator: QueryValidator,
        max_iterations: int = 3,
        escalation_temperature: float = 0.6,
        fix_temperature: float | None = None,
        parse_error_action: PreflightAction = PreflightAction.WARN,
        unmapped_tables_action: PreflightAction = PreflightAction.REJECT,
        unmapped_columns_action: PreflightAction = PreflightAction.REJECT,
        dialect: str | None = None,
    ) -> None:
        self._schema_mapping = schema_mapping
        self._llm = llm
        self._target = target
        self._validator = validator
        self._max_iterations = max_iterations
        # Input-side pre-flight policy (see sql2graph.engine.preflight). Defaults match
        # the product decision: warn-and-translate on an unparseable query (a
        # weak signal: sqlglot can false-fail on valid exotic SQL), and reject on
        # a query that reads tables (or names columns of a mapped table) absent
        # from the mapping. A column explicitly referenced but unmapped can't be
        # translated faithfully (the model has no property to map it to), so the
        # call would be wasted; the column check is conservative (only confidently
        # attributed columns of node tables), keeping false positives rare.
        self._parse_error_action = parse_error_action
        self._unmapped_tables_action = unmapped_tables_action
        self._unmapped_columns_action = unmapped_columns_action
        # sqlglot dialect for input analysis only (the single pre-flight parse:
        # parse_ok, source_tables/column_refs, and feature detection). ``None``
        # is dialect-neutral; the dialect never enters the LLM prompt.
        self._dialect = dialect
        # Sampling temperature for ordinary fix turns (``None`` = backend
        # default) and for the one-shot stall-breaking escalation retry. The
        # escalation runs hotter on purpose: a near-greedy retry over a history
        # full of the same rejected query just reproduces it.
        self._fix_temperature = fix_temperature
        self._escalation_temperature = escalation_temperature
        # Full system↔LLM conversation from the most recent translate() call
        # (system + user/assistant turns), overwritten each call. Exposed for
        # callers that want to display the exchange; TranslationResult itself
        # deliberately omits the chat history.
        self.last_messages: list[dict[str, str]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def translate(
        self,
        sql_query: str,
        on_event: EventHandler | None = None,
    ) -> TranslationResult:
        """Translate a SQL query through the full feedback loop.

        Returns a :class:`TranslationResult` regardless of outcome: on
        ``max_iterations_reached`` the result still carries the last
        candidate query so the caller can inspect it.

        Args:
            sql_query: The SQL to translate.
            on_event: Optional callback invoked at each loop milestone
                (initial generation, each validation pass, each fix,
                max-iterations hit, completion). See
                :mod:`sql2graph.engine.events`. Handler exceptions are caught,
                logged at WARNING, and do not abort the translation.
        """
        target_name = self._target.name
        if target_name not in ("cypher", "aql", "gremlin"):
            # TranslationState's Literal currently restricts to these three.
            # See note in src/sql2graph/engine/state.py about widening it.
            raise ValueError(f"Unsupported target language for TranslationState: {target_name!r}")

        # Pre-flight: parse the SQL once and decide whether to warn or reject
        # before doing any expensive work (a reject must run before warmup so it
        # never boots a managed database for a query we won't translate).
        analysis = analyze_sql(sql_query, dialect=self._dialect)
        outcome = evaluate_preflight(
            analysis,
            self._schema_mapping,
            self._parse_error_action,
            self._unmapped_tables_action,
            self._unmapped_columns_action,
        )
        if outcome is not None:
            emit_event(on_event, outcome.event)
            if outcome.is_reject:
                logger.info("Pre-flight rejected translation: %s", outcome.message)
                self.last_messages = []
                result = build_rejected_result(sql_query, target_name, outcome)
                emit_event(on_event, CompletedEvent(result=result))
                return result

        # Provision the validator (e.g. boot a managed throwaway database) before
        # the timer starts, so duration_seconds measures only the LLM + validation
        # loop rather than one-off database setup.
        warmup = getattr(self._validator, "warmup", None)
        if callable(warmup):
            warmup()

        start_time = perf_counter()
        state = TranslationState(
            sql_query=sql_query,
            target_language=target_name,  # type: ignore[arg-type]
        )

        features = analysis.features
        logger.info("Detected SQL features: %s", sorted(f.value for f in features))
        system_prompt = build_system_prompt(self._schema_mapping, self._target, features)
        state.messages.append(to_message("system", system_prompt))

        user_msg = build_generate_prompt(sql_query)
        state.messages.append(to_message("user", user_msg))

        reply = self._llm.chat(state.messages)
        state.token_usage = state.token_usage + reply.usage
        raw_content = reply.text
        state.messages.append(to_message("assistant", raw_content))
        state.generated_query = self._target.extract_query(raw_content)

        logger.info("Initial query generated:\n%s", state.generated_query)
        emit_event(on_event, GeneratedEvent(iteration=1, query=state.generated_query or ""))

        # Stall tracking: a fix that reproduces the prior candidate, or draws
        # the *same* validator error twice running, means the loop is stuck. On
        # the first stall we escalate once (fresh context + higher temperature);
        # a second stall aborts early with status "stalled" rather than burning
        # the remaining iterations on byte-identical output.
        previous_signature: frozenset[str] | None = None
        previous_norm_query: str | None = None
        escalated = False

        while state.validation_iteration < self._max_iterations:
            state.validation_iteration += 1
            errors = self._validator.validate(state.generated_query or "")
            state.validation_errors = errors

            if not errors:
                state.validation_passed = True
                state.final_status = "success"
                logger.info("Validation passed on iteration %d", state.validation_iteration)
                emit_event(
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
            emit_event(
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
                emit_event(
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
                    "No progress on iteration %d: escalating with a fresh context",
                    state.validation_iteration,
                )
                emit_event(
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
                state.messages.append(to_message("user", escalation_msg))
                # Re-ask from a CLEAN context (system turn + this one). The
                # accumulated history is several copies of the rejected query
                # and its error, exactly what pins a low-temperature model to
                # reproducing it. The full record still keeps the turn.
                reply = self._llm.chat(
                    [state.messages[0], to_message("user", escalation_msg)],
                    temperature=self._escalation_temperature,
                )
            else:
                fix_msg = build_fix_prompt(
                    sql_query=sql_query,
                    generated_query=state.generated_query or "",
                    errors=errors,
                    repair_hint=repair_hint,
                )
                state.messages.append(to_message("user", fix_msg))
                reply = self._llm.chat(state.messages, temperature=self._fix_temperature)

            state.token_usage = state.token_usage + reply.usage
            raw_content = reply.text
            state.messages.append(to_message("assistant", raw_content))
            state.generated_query = self._target.extract_query(raw_content)

            logger.info("Fix iteration %d produced:\n%s", state.validation_iteration, state.generated_query)
            emit_event(
                on_event,
                FixGeneratedEvent(
                    iteration=state.validation_iteration,
                    query=state.generated_query or "",
                ),
            )

        state.iterations_used = state.validation_iteration
        state.duration_seconds = perf_counter() - start_time
        self.last_messages = snapshot_messages(state.messages)

        result = build_result(state, outcome)
        emit_event(on_event, CompletedEvent(result=result))
        return result

    def close(self) -> None:
        """Release LLM and validator resources.

        Idempotent: calling more than once is safe. Both delegates' own
        ``close()`` methods are no-ops in the in-process syntax / no-op
        validators and the Ollama / Anthropic clients; only the Neo4j
        driver has real resources to release.
        """
        try:
            self._validator.close()
        finally:
            self._llm.close()
