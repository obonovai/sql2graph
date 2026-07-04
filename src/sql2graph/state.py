"""Loop state and public translation result.

Every piece of mutable state used by the generate-validate-fix loop in
:class:`sql2graph.translator.SQLTranslator` lives in :class:`TranslationState`.
The motivation for a single, explicit Pydantic state model (rather than a
collection of attributes on the translator object or a free-form dictionary)
is twofold:

1. **Traceability for thesis-quality analysis.** Every transition the loop
   performs is a write to one or more fields of a typed model; the resulting
   trace is unambiguous when reasoning about correctness of the loop's
   termination conditions.
2. **A clean public/internal split.** :class:`TranslationState` is internal:
   it accumulates the chat-message history and intermediate flags. The
   :class:`TranslationResult` returned to the caller exposes only the
   externally meaningful fields: the original SQL, the final query, the
   final status, the iteration count, and the wall-clock duration.

The ``target_language`` field is declared as
``Literal["cypher", "aql", "gremlin"]``, the same set of target languages
supported elsewhere in the framework. A further target language would
require widening this literal (and the counterpart in
:class:`sql2graph.validators.QueryValidator` dispatch). The Protocol-based
extension story documented in ``docs/ARCHITECTURE.md`` notes this as a
known limitation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sql2graph.llm.usage import TokenUsage


class TranslationState(BaseModel):
    """Internal state accumulated by the generate-validate-fix loop.

    Treat instances as mutable scratch space: the loop updates fields in place
    across iterations. The list ``messages`` is the full chat history sent to
    the LLM on each call (system + user + assistant + every prior fix
    request and prior LLM response) so the LLM can see what was tried and
    what went wrong.
    """

    sql_query: str
    target_language: Literal["cypher", "aql", "gremlin"] = "cypher"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    generated_query: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    validation_iteration: int = 0
    validation_passed: bool = False
    iterations_used: int = 0
    final_status: str = "pending"
    duration_seconds: float = 0.0
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class TranslationResult(BaseModel):
    """Public, immutable view of a completed translation.

    Returned by :meth:`sql2graph.translator.SQLTranslator.translate`. Contains
    only the fields that callers need to consume the outcome; the chat
    history and iteration counter are deliberately omitted.

    The ``status`` field takes one of these values:

    * ``"success"``: validator returned no errors on some iteration.
    * ``"max_iterations_reached"``: the loop hit
      ``max_validation_iterations`` without producing a valid query;
      ``generated_query`` still holds the last attempt.
    * ``"stalled"``: the loop detected it was making no progress (the model
      repeated a candidate / drew the same error twice), escalated once with a
      fresh-context, higher-temperature retry, and still could not advance, so
      it aborted early rather than burning the remaining iterations.
      ``generated_query`` holds the last attempt.
    * ``"unmapped_tables"``: a pre-flight check found the SQL reads tables
      absent from the schema mapping, so translation was rejected before any
      LLM call (the default ``unmapped_tables_action``). ``unmapped_tables``
      lists the offending tables; ``generated_query`` is ``None`` and
      ``token_usage`` is zero.
    * ``"unmapped_columns"``: a pre-flight check found the SQL uses columns of
      mapped tables that the mapping does not define, so translation was rejected
      before any LLM call (the default ``unmapped_columns_action``).
      ``unmapped_columns`` lists the offending ``"table.column"`` refs;
      ``generated_query`` is ``None`` and ``token_usage`` is zero. Pass
      ``"warn"`` or ``"ignore"`` to translate anyway.
    * ``"parse_error"``: a pre-flight check found the SQL was unparseable and
      the translator was configured with ``parse_error_action="reject"`` (not
      the default, which only warns). The LLM was skipped; ``generated_query``
      is ``None``.
    * ``"pending"``: sentinel for an unfinished translation; should not
      appear in returned results.
    """

    sql_query: str
    generated_query: str | None
    target_language: Literal["cypher", "aql", "gremlin"]
    validation_passed: bool
    validation_errors: list[str]
    iterations_used: int
    status: str
    unmapped_tables: list[str] = Field(default_factory=list)
    unmapped_columns: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
