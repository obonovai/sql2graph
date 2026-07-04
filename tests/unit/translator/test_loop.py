"""Sync translator loop tests: fix loop, escalation, dialect forwarding, warmup."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sql2graph import (
    CypherSyntaxValidator,
    CypherTarget,
    GremlinSyntaxValidator,
    GremlinTarget,
    SQLTranslator,
    TranslationResult,
)


def test_translator_forwards_dialect_to_analyze_sql(spy_analyze_sql: Callable[..., Any], scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """The constructor ``dialect`` reaches the pre-flight ``analyze_sql`` call.

    Forwarding the sqlglot dialect is the whole point of the parameter: it lets a
    valid vendor-specific query parse (keeping the unmapped-table/column checks
    live) instead of false-failing under the neutral parser.
    """
    import sql2graph.translator as translator_mod

    seen = spy_analyze_sql(translator_mod)
    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        dialect="postgres",
    ) as translator:
        translator.translate("SELECT * FROM persons")

    assert seen == ["postgres"]


def test_translator_dialect_defaults_to_none(spy_analyze_sql: Callable[..., Any], scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """Omitting ``dialect`` keeps the pre-flight parse dialect-neutral (``None``),
    i.e. identical to the behaviour before the parameter existed."""
    import sql2graph.translator as translator_mod

    seen = spy_analyze_sql(translator_mod)
    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons")

    assert seen == [None]


def test_translator_returns_result_on_first_try_success(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    ) as translator:
        result = translator.translate("SELECT * FROM persons")

    assert isinstance(result, TranslationResult)
    assert result.validation_passed is True
    assert result.status == "success"
    assert result.iterations_used == 1
    assert result.generated_query == "MATCH (p:Person) RETURN p"
    assert len(fake.calls) == 1
    assert fake.closed is True
    # One LLM call → one TokenUsage (10 in + 5 out).
    assert result.token_usage.total_tokens == 15
    assert result.token_usage.input_tokens == 10
    assert result.token_usage.output_tokens == 5


def test_translator_runs_fix_loop_on_validation_failure(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    # First response: malformed. Second response: valid.
    fake = scripted_llm(
        [
            "MATCH (p:Person",  # malformed: unbalanced parenthesis
            "MATCH (p:Person) RETURN p",
        ]
    )
    translator = SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    )
    try:
        result = translator.translate("SELECT * FROM persons")
    finally:
        translator.close()

    assert result.validation_passed is True
    assert result.iterations_used == 2
    assert len(fake.calls) == 2
    # Usage accumulates across both LLM calls: 2 × (10 in + 5 out).
    assert result.token_usage.total_tokens == 30
    assert result.token_usage.input_tokens == 20
    assert result.token_usage.output_tokens == 10


def test_translator_hits_max_iterations(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    fake = scripted_llm(["MATCH (p:Person"] * 3)  # always invalid
    translator = SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    )
    try:
        result = translator.translate("SELECT * FROM persons")
    finally:
        translator.close()

    assert result.validation_passed is False
    assert result.status == "max_iterations_reached"
    assert result.iterations_used == 3
    assert result.generated_query == "MATCH (p:Person"


def test_translator_escalates_on_stall_then_recovers(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """A repeated (stalled) candidate triggers one fresh-context, hot retry that recovers."""
    from sql2graph import StalledEvent, TranslationEvent

    # gen=bad, fix=identical bad (→ stall), escalation=good.
    fake = scripted_llm(["MATCH (p:Person", "MATCH (p:Person", "MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=5,
    ) as translator:
        result = translator.translate("SELECT * FROM persons", on_event=events.append)

    assert result.status == "success"
    assert result.generated_query == "MATCH (p:Person) RETURN p"
    # Exactly one escalation was signalled.
    assert sum(isinstance(e, StalledEvent) for e in events) == 1
    # Three LLM calls: generate, normal fix, escalation.
    assert len(fake.calls) == 3
    # The escalation ran hotter than the (default) generate/fix calls...
    assert fake.temperatures == [None, None, 0.6]
    # ...and on a CLEAN context: system turn + the single escalation user turn.
    assert len(fake.calls[2]) == 2
    assert fake.calls[2][0]["role"] == "system"


def test_translator_aborts_early_when_stalled_instead_of_burning_iterations(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """When even the escalation makes no progress, abort as 'stalled', not 10 identical tries."""
    from sql2graph import StalledEvent, TranslationEvent

    fake = scripted_llm(["MATCH (p:Person"] * 4)  # always invalid; one response left unused
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=10,
    ) as translator:
        result = translator.translate("SELECT * FROM persons", on_event=events.append)

    assert result.status == "stalled"
    assert result.validation_passed is False
    # generate + normal fix + escalation = 3 calls, then it gives up (not 10).
    assert result.iterations_used == 3
    assert len(fake.calls) == 3
    assert sum(isinstance(e, StalledEvent) for e in events) == 1


def test_translator_context_manager_closes_components(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    fake = scripted_llm(["MATCH (p) RETURN p"])
    validator = CypherSyntaxValidator()
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=validator,
    ) as translator:
        translator.translate("SELECT 1")
    assert fake.closed is True


def test_translator_returns_result_for_gremlin_target(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    fake = scripted_llm(["```gremlin\ng.V().hasLabel('Person').valueMap()\n```"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=GremlinTarget(),
        validator=GremlinSyntaxValidator(),
        max_iterations=3,
    ) as translator:
        result = translator.translate("SELECT * FROM persons")

    assert isinstance(result, TranslationResult)
    assert result.validation_passed is True
    assert result.status == "success"
    assert result.target_language == "gremlin"
    assert result.generated_query == "g.V().hasLabel('Person').valueMap()"


def test_translator_exposes_last_messages_conversation(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """last_messages captures the full system↔model exchange (incl. a fix loop)."""
    fake = scripted_llm(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])  # fail, then fix
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons")

    roles = [m["role"] for m in translator.last_messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert translator.last_messages[-1]["content"] == "MATCH (p:Person) RETURN p"
    assert all(set(m) == {"role", "content"} for m in translator.last_messages)


def test_translator_warms_up_validator_before_validation(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """A validator exposing warmup() is warmed up exactly once, before the first validate."""
    calls: list[str] = []

    class _WarmupValidator:
        def warmup(self) -> None:
            calls.append("warmup")

        def validate(self, _query: str) -> list[str]:
            calls.append("validate")
            return []

        def close(self) -> None:
            calls.append("close")

    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=_WarmupValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons")

    assert calls[0] == "warmup"
    assert calls.count("warmup") == 1
    assert calls.index("warmup") < calls.index("validate")
