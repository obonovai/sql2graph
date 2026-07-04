"""Typed iteration events emitted by the sync translator loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sql2graph import CypherSyntaxValidator, CypherTarget, SQLTranslator


def test_translator_emits_event_sequence_on_first_try_success(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """One-shot success: Generated → Validated(passed=True) → Completed."""
    from sql2graph import CompletedEvent, GeneratedEvent, TranslationEvent, ValidatedEvent

    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons", on_event=events.append)

    assert len(events) == 3
    assert isinstance(events[0], GeneratedEvent)
    assert events[0].iteration == 1
    assert events[0].query == "MATCH (p:Person) RETURN p"
    assert isinstance(events[1], ValidatedEvent)
    assert events[1].iteration == 1
    assert events[1].passed is True
    assert events[1].errors == []
    assert isinstance(events[2], CompletedEvent)
    assert events[2].result.status == "success"


def test_translator_emits_event_sequence_on_fix_loop(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """One fix cycle: Generated → Validated(failed) → FixGenerated → Validated(passed) → Completed."""
    from sql2graph import (
        CompletedEvent,
        FixGeneratedEvent,
        GeneratedEvent,
        TranslationEvent,
        ValidatedEvent,
    )

    fake = scripted_llm(
        [
            "MATCH (p:Person",  # malformed: unbalanced parenthesis
            "MATCH (p:Person) RETURN p",
        ]
    )
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons", on_event=events.append)

    types = [type(e).__name__ for e in events]
    assert types == [
        "GeneratedEvent",
        "ValidatedEvent",
        "FixGeneratedEvent",
        "ValidatedEvent",
        "CompletedEvent",
    ]
    assert isinstance(events[0], GeneratedEvent) and events[0].iteration == 1
    assert isinstance(events[1], ValidatedEvent) and events[1].iteration == 1 and events[1].passed is False
    assert events[1].errors  # non-empty
    assert isinstance(events[2], FixGeneratedEvent) and events[2].iteration == 1
    assert events[2].query == "MATCH (p:Person) RETURN p"
    assert isinstance(events[3], ValidatedEvent) and events[3].iteration == 2 and events[3].passed is True
    assert isinstance(events[4], CompletedEvent)


def test_translator_emits_max_iterations_event_when_loop_gives_up(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    from sql2graph import CompletedEvent, MaxIterationsReachedEvent, TranslationEvent

    fake = scripted_llm(["MATCH (p:Person"] * 3)  # always invalid
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    ) as translator:
        translator.translate("SELECT 1", on_event=events.append)

    max_events = [e for e in events if isinstance(e, MaxIterationsReachedEvent)]
    assert len(max_events) == 1
    assert max_events[0].iteration == 3
    assert max_events[0].errors  # non-empty
    # CompletedEvent is always the last event, even on max-iterations failure.
    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].result.status == "max_iterations_reached"


def test_translator_translates_without_on_event_handler(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """Backwards-compat: omitting on_event must not change behavior."""
    fake = scripted_llm(["MATCH (p) RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT 1")
    assert result.status == "success"


def test_translator_swallows_handler_exceptions(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    """A misbehaving handler must not abort the translation."""

    def boom(_event: object) -> None:
        raise RuntimeError("handler bug")

    fake = scripted_llm(["MATCH (p) RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT 1", on_event=boom)
    assert result.status == "success"
    assert result.generated_query == "MATCH (p) RETURN p"
