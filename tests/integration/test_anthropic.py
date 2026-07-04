"""Integration tests exercising real Anthropic LLM calls (no database required)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from sql2graph import (
    AnthropicConfig,
    AnthropicLLMClient,
    AsyncSQLTranslator,
    CypherSyntaxValidator,
    CypherTarget,
    SchemaMapping,
    SQLTranslator,
    make_async_llm,
    make_async_validator,
)

pytestmark = pytest.mark.integration


def test_real_anthropic_translates_simple_select_to_cypher(
    anthropic_config: AnthropicConfig,
    small_schema: SchemaMapping,
) -> None:
    """End-to-end: real Anthropic call → syntactically valid Cypher."""
    with SQLTranslator(
        schema_mapping=small_schema,
        llm=AnthropicLLMClient(anthropic_config),
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    ) as translator:
        result = translator.translate("SELECT full_name FROM persons WHERE id = 1")

    assert result.generated_query is not None, "model returned no extractable query"
    assert result.validation_passed, f"loop did not converge: errors={result.validation_errors}"
    # Sanity: the query should mention the Person node label since the mapping
    # tells the model that's how ``persons`` maps to the graph.
    assert "Person" in result.generated_query


def test_real_anthropic_logs_token_usage(
    anthropic_config: AnthropicConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Anthropic chat helper must log AND return non-zero token counts."""
    client = AnthropicLLMClient(anthropic_config)
    with caplog.at_level(logging.INFO, logger="sql2graph.llm.anthropic"):
        reply = client.chat(
            [
                {"role": "system", "content": "Respond with exactly the word OK."},
                {"role": "user", "content": "ready?"},
            ]
        )
    assert reply.text  # non-empty response text
    # Usage is now returned on the ChatReply, not just logged. Counts are
    # model-dependent, so we only assert they're present and non-zero.
    assert reply.usage.input_tokens > 0
    assert reply.usage.output_tokens > 0
    assert reply.usage.total_tokens >= reply.usage.input_tokens + reply.usage.output_tokens
    usage_lines = [r for r in caplog.records if "Anthropic call:" in r.getMessage()]
    assert usage_lines, "expected at least one 'Anthropic call:' usage log line"
    msg = usage_lines[-1].getMessage()
    assert "input=" in msg and "output=" in msg
    assert "input=0" not in msg
    assert "output=0" not in msg


def test_real_anthropic_async_translates_simple_select(
    anthropic_config: AnthropicConfig,
    small_schema: SchemaMapping,
) -> None:
    """Async path is structurally equivalent: same kind of result for the same input."""

    async def run() -> str | None:
        llm = make_async_llm(anthropic_config)
        validator = make_async_validator("cypher", "syntax")
        async with AsyncSQLTranslator(
            schema_mapping=small_schema,
            llm=llm,
            target=CypherTarget(),
            validator=validator,
            max_iterations=3,
        ) as translator:
            result = await translator.translate("SELECT full_name FROM persons WHERE id = 1")
        return result.generated_query if result.validation_passed else None

    query = asyncio.run(run())
    assert query is not None, "async loop did not converge"
    assert "Person" in query
