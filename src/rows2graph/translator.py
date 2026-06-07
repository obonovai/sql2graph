"""Generate–validate–fix orchestrator.

This module defines :class:`SQLTranslator`, the public orchestrator that
runs the framework's core feedback loop:

1. *Generate*: build a system prompt (schema mapping + target-language
   rules), send the SQL query as a user turn, extract the candidate query
   from the LLM's response.
2. *Validate*: pass the candidate to the configured
   :class:`~rows2graph.validators.QueryValidator`. If it returns an empty
   error list, the translation succeeded.
3. *Fix*: otherwise, append a fix prompt naming the errors, call the LLM
   again, and repeat. The loop terminates after
   ``max_iterations`` validate calls (counting the initial one), or
   earlier on success.

The class is deliberately small — about a single screen of orchestration —
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
from typing import Any, Self

from rows2graph.llm import LLMClient
from rows2graph.mapping import SchemaMapping
from rows2graph.prompts import build_fix_prompt, build_generate_prompt, build_system_prompt
from rows2graph.state import TranslationResult, TranslationState
from rows2graph.targets import TargetLanguage
from rows2graph.validators import QueryValidator

logger = logging.getLogger(__name__)


class SQLTranslator:
    """The generate–validate–fix orchestrator.

    Construct with already-instantiated components. Call :meth:`translate`
    one or more times — the same translator instance reuses its LLM and
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
    ) -> None:
        self._schema_mapping = schema_mapping
        self._llm = llm
        self._target = target
        self._validator = validator
        self._max_iterations = max_iterations

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def translate(self, sql_query: str) -> TranslationResult:
        """Translate a SQL query through the full feedback loop.

        Returns a :class:`TranslationResult` regardless of outcome — on
        ``max_iterations_reached`` the result still carries the last
        candidate query so the caller can inspect it.
        """
        start_time = perf_counter()
        target_name = self._target.name
        if target_name not in ("cypher", "aql"):
            # TranslationState's Literal currently restricts to these two.
            # See note in src/rows2graph/state.py about widening it.
            raise ValueError(f"Unsupported target language for TranslationState: {target_name!r}")
        state = TranslationState(
            sql_query=sql_query,
            target_language=target_name,  # type: ignore[arg-type]
        )

        system_prompt = build_system_prompt(self._schema_mapping, self._target)
        state.messages.append(_msg("system", system_prompt))

        user_msg = build_generate_prompt(sql_query)
        state.messages.append(_msg("user", user_msg))

        raw_content = self._llm.chat(state.messages)
        state.messages.append(_msg("assistant", raw_content))
        state.generated_query = self._target.extract_query(raw_content)

        logger.info("Initial query generated:\n%s", state.generated_query)

        while state.validation_iteration < self._max_iterations:
            state.validation_iteration += 1
            errors = self._validator.validate(state.generated_query or "")
            state.validation_errors = errors

            if not errors:
                state.validation_passed = True
                state.final_status = "success"
                logger.info("Validation passed on iteration %d", state.validation_iteration)
                break

            logger.info(
                "Validation iteration %d found %d error(s): %s",
                state.validation_iteration,
                len(errors),
                errors,
            )

            if state.validation_iteration >= self._max_iterations:
                state.final_status = "max_iterations_reached"
                logger.warning(
                    "Max iterations (%d) reached with errors: %s",
                    self._max_iterations,
                    errors,
                )
                break

            fix_msg = build_fix_prompt(
                sql_query=sql_query,
                generated_query=state.generated_query or "",
                errors=errors,
            )
            state.messages.append(_msg("user", fix_msg))

            raw_content = self._llm.chat(state.messages)
            state.messages.append(_msg("assistant", raw_content))
            state.generated_query = self._target.extract_query(raw_content)

            logger.info("Fix iteration %d produced:\n%s", state.validation_iteration, state.generated_query)

        state.iterations_used = state.validation_iteration
        state.duration_seconds = perf_counter() - start_time

        return TranslationResult(
            sql_query=state.sql_query,
            generated_query=state.generated_query,
            target_language=state.target_language,
            validation_passed=state.validation_passed,
            validation_errors=state.validation_errors,
            iterations_used=state.iterations_used,
            status=state.final_status,
            duration_seconds=state.duration_seconds,
        )

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


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}
