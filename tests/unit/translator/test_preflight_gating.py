"""Pre-flight gating in the sync translator: unmapped tables/columns, parse warnings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sql2graph import CypherSyntaxValidator, CypherTarget, SQLTranslator
from sql2graph.preflight import PreflightAction


def test_translator_rejects_unmapped_tables_without_calling_llm(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    from sql2graph import CompletedEvent, TranslationEvent, UnmappedTablesEvent

    fake = scripted_llm(["MATCH (p:Person) RETURN p"])  # must never be consumed
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT * FROM nonexistent_table", on_event=events.append)

    assert result.status == "unmapped_tables"
    assert result.validation_passed is False
    assert result.unmapped_tables == ["nonexistent_table"]
    assert result.generated_query is None
    assert result.iterations_used == 0
    assert result.token_usage.total_tokens == 0
    assert len(fake.calls) == 0  # the LLM was skipped entirely
    # Exactly the rejection event then the always-last CompletedEvent.
    assert [type(e).__name__ for e in events] == ["UnmappedTablesEvent", "CompletedEvent"]
    assert isinstance(events[0], UnmappedTablesEvent)
    assert isinstance(events[-1], CompletedEvent)


def test_translator_warns_on_parse_failure_but_still_translates(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    from sql2graph import GeneratedEvent, ParseFailedEvent, TranslationEvent

    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT ;;; FROM", on_event=events.append)

    # Warn, don't block: the LLM still ran and produced a result.
    assert result.status == "success"
    assert len(fake.calls) == 1
    assert result.validation_errors == []  # warn must not pollute validation errors
    parse_events = [e for e in events if isinstance(e, ParseFailedEvent)]
    assert len(parse_events) == 1
    # The warning precedes the initial generation event.
    assert events.index(parse_events[0]) < next(i for i, e in enumerate(events) if isinstance(e, GeneratedEvent))


def test_translator_no_preflight_events_for_mapped_query(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    from sql2graph import ParseFailedEvent, TranslationEvent, UnmappedTablesEvent

    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons", on_event=events.append)

    assert not any(isinstance(e, (ParseFailedEvent, UnmappedTablesEvent)) for e in events)


def test_translator_does_not_flag_cte_name_as_unmapped(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    # A CTE alias must not be mistaken for an unmapped table: the underlying
    # 'persons' is mapped, so this translates normally.
    fake = scripted_llm(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("WITH recent AS (SELECT * FROM persons) SELECT * FROM recent")

    assert result.status == "success"
    assert result.unmapped_tables == []


def test_translator_ignore_action_keeps_legacy_behavior(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    # With both actions IGNORE, an unmapped table is translated as before
    # (the LLM is called, no preflight events, no rejection).
    from sql2graph import ParseFailedEvent, TranslationEvent, UnmappedTablesEvent

    fake = scripted_llm(["MATCH (x) RETURN x"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        parse_error_action=PreflightAction.IGNORE,
        unmapped_tables_action=PreflightAction.IGNORE,
    ) as translator:
        result = translator.translate("SELECT * FROM nonexistent_table", on_event=events.append)

    assert result.status == "success"
    assert len(fake.calls) == 1
    assert not any(isinstance(e, (ParseFailedEvent, UnmappedTablesEvent)) for e in events)


def test_translator_warns_on_unmapped_column_when_configured_to_warn(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    from sql2graph import GeneratedEvent, TranslationEvent, UnmappedColumnsEvent

    fake = scripted_llm(["MATCH (f:Forum) RETURN f"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        unmapped_columns_action=PreflightAction.WARN,  # opt out of the reject default
    ) as translator:
        result = translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)

    assert result.status == "success"
    assert len(fake.calls) == 1  # warn does not block the LLM
    assert result.validation_errors == []  # must not pollute validation errors
    assert result.unmapped_columns == ["forums.bogus"]  # self-describing result
    parse = [e for e in events if isinstance(e, UnmappedColumnsEvent)]
    assert len(parse) == 1
    assert parse[0].columns == ["forums.bogus"]
    # The warning precedes the initial generation event.
    assert events.index(parse[0]) < next(i for i, e in enumerate(events) if isinstance(e, GeneratedEvent))


def test_translator_rejects_unmapped_column_by_default(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    from sql2graph import CompletedEvent, TranslationEvent

    fake = scripted_llm(["MATCH (f:Forum) RETURN f"])  # must never be consumed
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)

    assert result.status == "unmapped_columns"
    assert result.unmapped_columns == ["forums.bogus"]
    assert result.generated_query is None
    assert result.token_usage.total_tokens == 0
    assert len(fake.calls) == 0
    assert [type(e).__name__ for e in events] == ["UnmappedColumnsEvent", "CompletedEvent"]
    assert isinstance(events[-1], CompletedEvent)


def test_translator_no_column_signal_for_mapped_or_star_queries(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    from sql2graph import TranslationEvent, UnmappedColumnsEvent

    for sql in ("SELECT * FROM persons", "SELECT full_name FROM persons WHERE id = 1"):
        fake = scripted_llm(["MATCH (p:Person) RETURN p"])
        events: list[TranslationEvent] = []
        with SQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=CypherSyntaxValidator(),
        ) as translator:
            result = translator.translate(sql, on_event=events.append)
        assert not any(isinstance(e, UnmappedColumnsEvent) for e in events), sql
        assert result.unmapped_columns == [], sql
