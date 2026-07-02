"""Integration tests exercising a real Neo4j server validator."""

from __future__ import annotations

import asyncio

import pytest

from rows2graph import (
    AnthropicConfig,
    AnthropicLLMClient,
    CypherTarget,
    Neo4jConfig,
    SchemaMapping,
    SQLTranslator,
)
from rows2graph.validators.cypher.server import (
    AsyncCypherServerValidator,
    CypherServerValidator,
)

pytestmark = pytest.mark.integration


def test_real_neo4j_server_validator_rejects_known_bad_query(
    neo4j_config: Neo4jConfig,
) -> None:
    """A query referencing an obviously nonsensical label must produce errors."""
    validator = CypherServerValidator(neo4j_config)
    try:
        # Even on an empty database, EXPLAIN catches schema-level issues like
        # malformed syntax. Deliberately broken paren balance is the most
        # reliable always-fails check.
        errors = validator.validate("MATCH (n RETURN n")
    finally:
        validator.close()
    assert errors, "expected the server to reject malformed query"


def test_real_neo4j_server_validator_accepts_well_formed_query(
    neo4j_config: Neo4jConfig,
) -> None:
    """A trivially valid query must produce no errors."""
    validator = CypherServerValidator(neo4j_config)
    try:
        errors = validator.validate("MATCH (n) RETURN n LIMIT 1")
    finally:
        validator.close()
    assert errors == [], f"expected no errors, got: {errors}"


def test_real_neo4j_async_server_validator_matches_sync(
    neo4j_config: Neo4jConfig,
) -> None:
    """Async server validator returns the same shape of result as sync."""

    async def run_async(query: str) -> list[str]:
        v = AsyncCypherServerValidator(neo4j_config)
        try:
            return await v.validate(query)
        finally:
            await v.close()

    bad = asyncio.run(run_async("MATCH (n RETURN n"))
    good = asyncio.run(run_async("MATCH (n) RETURN n LIMIT 1"))
    assert bad, "async server validator should reject malformed query"
    assert good == [], f"async server validator should accept well-formed query, got: {good}"


def test_real_full_loop_anthropic_with_neo4j_server_validation(
    anthropic_config: AnthropicConfig,
    neo4j_config: Neo4jConfig,
    small_schema: SchemaMapping,
) -> None:
    """End-to-end: translate against real LLM, validate against real Neo4j EXPLAIN."""
    with SQLTranslator(
        schema_mapping=small_schema,
        llm=AnthropicLLMClient(anthropic_config),
        target=CypherTarget(),
        validator=CypherServerValidator(neo4j_config),
        max_iterations=3,
    ) as translator:
        result = translator.translate("SELECT full_name FROM persons WHERE id = 1")

    # We do not pin the exact final query; model output varies. But the
    # loop must converge to *some* server-validated query within 3 iterations.
    assert result.validation_passed, (
        f"server validation did not pass: status={result.status} "
        f"errors={result.validation_errors} iterations={result.iterations_used}"
    )
    assert result.generated_query is not None
    assert result.iterations_used >= 1
