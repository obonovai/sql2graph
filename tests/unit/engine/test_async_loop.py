"""Async translator loop tests: fix loop, escalation, streaming, conversation, warmup."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sql2graph import CypherTarget, PreflightAction, TranslationResult


def test_async_translator_returns_result_on_first_try_success(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = scripted_async_llm(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            result = await translator.translate("SELECT * FROM persons")
        assert fake.closed is True
        return result

    result = asyncio.run(run())
    assert result.validation_passed is True
    assert result.status == "success"
    assert result.iterations_used == 1
    assert result.generated_query == "MATCH (p:Person) RETURN p"


def test_async_translator_forwards_dialect_to_analyze_sql(
    spy_analyze_sql: Callable[..., Any], scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    """Async mirror of the sync forwarding test: the constructor ``dialect`` reaches
    ``async_translator.analyze_sql`` (kept in lockstep with the sync path)."""
    import asyncio

    import sql2graph.engine.async_translator as async_translator_mod
    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    seen = spy_analyze_sql(async_translator_mod)

    async def run() -> None:
        fake = scripted_async_llm(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            dialect="mysql",
        ) as translator:
            await translator.translate("SELECT * FROM persons")

    asyncio.run(run())
    assert seen == ["mysql"]


def test_async_translator_runs_fix_loop_on_validation_failure(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = scripted_async_llm(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            return await translator.translate("SELECT * FROM persons")

    result = asyncio.run(run())
    assert result.validation_passed is True
    assert result.iterations_used == 2
    # Usage accumulates across both async LLM calls: 2 × (10 in + 5 out).
    assert result.token_usage.total_tokens == 30


def test_async_translator_rejects_unmapped_tables_without_calling_llm(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    import asyncio

    from sql2graph import AsyncSQLTranslator, CompletedEvent, TranslationEvent
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[TranslationResult, Any, list[TranslationEvent]]:
        fake = scripted_async_llm(["MATCH (p:Person) RETURN p"])  # must never be consumed
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            result = await translator.translate("SELECT * FROM nonexistent_table", on_event=events.append)
        return result, fake, events

    result, fake, events = asyncio.run(run())
    assert result.status == "unmapped_tables"
    assert result.unmapped_tables == ["nonexistent_table"]
    assert result.generated_query is None
    assert result.token_usage.total_tokens == 0
    assert len(fake.calls) == 0
    assert [type(e).__name__ for e in events] == ["UnmappedTablesEvent", "CompletedEvent"]
    assert isinstance(events[-1], CompletedEvent)


def test_async_translator_unmapped_column_warn_and_reject(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    import asyncio

    from sql2graph import AsyncSQLTranslator, TranslationEvent, UnmappedColumnsEvent
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def warn() -> tuple[TranslationResult, Any, list[TranslationEvent]]:
        fake = scripted_async_llm(["MATCH (f:Forum) RETURN f"])
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            unmapped_columns_action=PreflightAction.WARN,  # opt out of the reject default
        ) as translator:
            result = await translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)
        return result, fake, events

    async def reject() -> tuple[TranslationResult, Any, list[TranslationEvent]]:
        # No explicit action: reject is the default.
        fake = scripted_async_llm(["MATCH (f:Forum) RETURN f"])
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            result = await translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)
        return result, fake, events

    wr, wfake, wevents = asyncio.run(warn())
    assert wr.status == "success"
    assert len(wfake.calls) == 1
    assert wr.unmapped_columns == ["forums.bogus"]
    assert any(isinstance(e, UnmappedColumnsEvent) for e in wevents)

    rr, rfake, revents = asyncio.run(reject())
    assert rr.status == "unmapped_columns"
    assert rr.unmapped_columns == ["forums.bogus"]
    assert len(rfake.calls) == 0
    assert [type(e).__name__ for e in revents] == ["UnmappedColumnsEvent", "CompletedEvent"]


def test_async_translator_hits_max_iterations(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = scripted_async_llm(["MATCH (p:Person"] * 3)
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            max_iterations=3,
        ) as translator:
            return await translator.translate("SELECT * FROM persons")

    result = asyncio.run(run())
    assert result.validation_passed is False
    assert result.status == "max_iterations_reached"
    assert result.iterations_used == 3


def test_async_translator_escalates_and_aborts_when_stalled(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[TranslationResult, Any]:
        fake = scripted_async_llm(["MATCH (p:Person"] * 4)  # always invalid
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            max_iterations=10,
        ) as translator:
            return await translator.translate("SELECT * FROM persons"), fake

    result, fake = asyncio.run(run())
    assert result.status == "stalled"
    assert result.iterations_used == 3
    assert len(fake.calls) == 3
    # The escalation call (3rd) ran at the hot escalation temperature.
    assert fake.temperatures == [None, None, 0.6]


def test_async_translator_emits_same_event_sequence_as_sync(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    """The async translator emits the same event sequence as the sync one
    for an identical input: events are part of the cross-translator contract."""
    import asyncio

    from sql2graph import (
        AsyncSQLTranslator,
        CompletedEvent,
        FixGeneratedEvent,
        GeneratedEvent,
        TranslationEvent,
        ValidatedEvent,
    )
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> list[TranslationEvent]:
        fake = scripted_async_llm(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons", on_event=events.append)
        return events

    events = asyncio.run(run())
    types = [type(e).__name__ for e in events]
    assert types == [
        "GeneratedEvent",
        "ValidatedEvent",
        "FixGeneratedEvent",
        "ValidatedEvent",
        "CompletedEvent",
    ]
    assert isinstance(events[0], GeneratedEvent) and events[0].iteration == 1
    assert isinstance(events[1], ValidatedEvent) and events[1].passed is False
    assert isinstance(events[2], FixGeneratedEvent) and events[2].iteration == 1
    assert isinstance(events[3], ValidatedEvent) and events[3].passed is True
    assert isinstance(events[4], CompletedEvent)


def test_async_translator_forwards_stream_to_into_each_llm_call(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    """The translator must invoke the stream callback for every LLM call:
    once for the initial generate, once per fix iteration."""
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[list[str], Any]:
        fake = scripted_async_llm(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])
        chunks: list[str] = []
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate(
                "SELECT * FROM persons",
                stream_to=chunks.append,
            )
        return chunks, fake

    chunks, fake = asyncio.run(run())
    # Both LLM responses must have streamed in their entirety. The fake LLM
    # emits one chunk per character.
    streamed = "".join(chunks)
    assert "MATCH (p:Person" in streamed
    assert "MATCH (p:Person) RETURN p" in streamed
    assert fake.stream_calls == 2  # initial generate + 1 fix


def test_async_translator_omits_stream_to_by_default(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    """Without stream_to, the fake LLM records zero stream calls, confirms
    the streaming path is opt-in."""
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> Any:
        fake = scripted_async_llm(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons")
        return fake

    fake = asyncio.run(run())
    assert fake.stream_calls == 0


def test_async_translator_exposes_last_messages_conversation(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    """The async translator exposes the same last_messages conversation."""
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> list[dict[str, str]]:
        fake = scripted_async_llm(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons")
        return translator.last_messages

    messages = asyncio.run(run())
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    assert messages[-1]["content"] == "MATCH (p:Person) RETURN p"


def test_async_translator_on_conversation_streams_snapshots(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    """on_conversation fires growing snapshots, including a partial assistant turn."""
    import asyncio

    from sql2graph import AsyncSQLTranslator
    from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[list[list[dict[str, str]]], list[dict[str, str]]]:
        fake = scripted_async_llm(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])  # fail, then fix
        snaps: list[list[dict[str, str]]] = []
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons", on_conversation=snaps.append)
        return snaps, translator.last_messages

    snaps, last = asyncio.run(run())
    # The final snapshot is the full conversation and matches last_messages.
    assert snaps[-1] == last
    assert [m["role"] for m in snaps[-1]] == ["system", "user", "assistant", "user", "assistant"]
    # Per-token streaming produces many more snapshots than there are turns.
    assert len(snaps) > len(last)
    # At least one snapshot shows a *partial* assistant turn (mid-stream).
    partials = [s[-1]["content"] for s in snaps if s and s[-1]["role"] == "assistant"]
    assert any(0 < len(p) < len("MATCH (p:Person") for p in partials)


def test_async_translator_warms_up_validator_before_validation(
    scripted_async_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]
) -> None:
    """The async translator awaits the validator's warmup before the first validate."""
    import asyncio

    from sql2graph import AsyncSQLTranslator

    calls: list[str] = []

    class _AsyncWarmupValidator:
        async def warmup(self) -> None:
            calls.append("warmup")

        async def validate(self, _query: str) -> list[str]:
            calls.append("validate")
            return []

        async def close(self) -> None:
            calls.append("close")

    async def run() -> None:
        fake = scripted_async_llm(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=person_forum_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=_AsyncWarmupValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons")

    asyncio.run(run())
    assert calls[0] == "warmup"
    assert calls.count("warmup") == 1
    assert calls.index("warmup") < calls.index("validate")
