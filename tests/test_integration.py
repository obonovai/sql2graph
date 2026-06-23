"""Integration tests against real LLMs and databases.

These tests are deselected by default — pyproject.toml's
``addopts = "-m 'not integration'"`` excludes the ``integration`` marker
from the default ``pytest`` run. Opt in with::

    uv run pytest -m integration

Tests that need credentials skip themselves when the corresponding env var
is unset, so partial setups (e.g. Anthropic key but no Neo4j) still get
useful coverage on the slice they can run.

See ``tests/README.md`` for the full env-var list and a docker-compose
recipe for the database services.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from rows2graph import (
    AnthropicConfig,
    AnthropicLLMClient,
    AsyncManagedServerValidator,
    AsyncSQLTranslator,
    CypherSyntaxValidator,
    CypherTarget,
    EdgeMapping,
    ManagedServerValidator,
    Neo4jConfig,
    NodeMapping,
    SchemaMapping,
    SQLTranslator,
    make_async_llm,
    make_async_validator,
    make_validator,
)
from rows2graph.validators.cypher.server import (
    AsyncCypherServerValidator,
    CypherServerValidator,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anthropic_config() -> AnthropicConfig:
    """An :class:`AnthropicConfig` ready to use, or skip if no API key."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    return AnthropicConfig(
        # ``api_key=None`` makes the SDK fall back to ANTHROPIC_API_KEY.
        model="claude-haiku-4-5-20251001",
        temperature=0.1,
        max_output_tokens=512,
    )


@pytest.fixture
def neo4j_config() -> Neo4jConfig:
    """A :class:`Neo4jConfig` ready to use, or skip if NEO4J_PASSWORD unset."""
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        pytest.skip("NEO4J_PASSWORD not set")
    return Neo4jConfig(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        username=os.environ.get("NEO4J_USERNAME", "neo4j"),
        password=password,
        database=os.environ.get("NEO4J_DATABASE", "neo4j"),
    )


@pytest.fixture
def docker_available() -> None:
    """Skip the test unless a Docker daemon is reachable (managed mode needs it).

    Retries a few times so a cold daemon (e.g. Docker Desktop still warming up)
    does not spuriously skip the first managed test in a run.
    """
    import time

    import docker

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            docker.from_env(timeout=120).ping()
            return
        except Exception as e:
            last_exc = e
            if attempt < 2:
                time.sleep(3)
    pytest.skip(f"Docker daemon not available: {last_exc}")


@pytest.fixture
def small_schema() -> SchemaMapping:
    """A minimal two-node, one-edge schema — small enough to keep prompts cheap."""
    return SchemaMapping(
        nodes=[
            NodeMapping(
                label="Person",
                source_table="persons",
                primary_key="id",
                properties={"name": "full_name"},
            ),
            NodeMapping(
                label="Forum",
                source_table="forums",
                primary_key="id",
                properties={"title": "title"},
            ),
        ],
        edges=[
            EdgeMapping(
                type="MEMBER_OF",
                source_node="Person",
                target_node="Forum",
                source_table="forum_members",
                source_foreign_key="person_id",
                target_primary_key="id",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Anthropic-only tests (no database required)
# ---------------------------------------------------------------------------


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
    with caplog.at_level(logging.INFO, logger="rows2graph.llm.anthropic"):
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
    """Async path is structurally equivalent — same kind of result for the same input."""

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


# ---------------------------------------------------------------------------
# Neo4j-only tests (no LLM required)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Full loop: real Anthropic + real Neo4j
# ---------------------------------------------------------------------------


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

    # We do not pin the exact final query — model output varies. But the
    # loop must converge to *some* server-validated query within 3 iterations.
    assert result.validation_passed, (
        f"server validation did not pass: status={result.status} "
        f"errors={result.validation_errors} iterations={result.iterations_used}"
    )
    assert result.generated_query is not None
    assert result.iterations_used >= 1


# ---------------------------------------------------------------------------
# Managed validation: auto-provisioned throwaway databases (need Docker only)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("docker_available")
def test_managed_cypher_validator_accepts_and_rejects() -> None:
    """Managed Cypher provisions Neo4j, then accepts a valid query and rejects a bad one."""
    validator = ManagedServerValidator("cypher")
    try:
        bad = validator.validate("MATCH (n RETURN n")
        good = validator.validate("MATCH (n) RETURN n LIMIT 1")
    finally:
        validator.close()
    assert bad, "managed Cypher validator should reject a malformed query"
    assert good == [], f"managed Cypher validator should accept a valid query, got: {good}"


@pytest.mark.usefixtures("docker_available")
def test_managed_aql_validator_accepts_and_rejects() -> None:
    """Managed AQL provisions ArangoDB, then accepts valid AQL and rejects malformed."""
    validator = ManagedServerValidator("aql")
    try:
        bad = validator.validate("FOR x IN RETURN x")
        good = validator.validate("RETURN 1")
    finally:
        validator.close()
    assert bad, "managed AQL validator should reject a malformed query"
    assert good == [], f"managed AQL validator should accept a valid query, got: {good}"


@pytest.mark.usefixtures("docker_available")
def test_managed_gremlin_validator_accepts_and_rejects() -> None:
    """Managed Gremlin provisions a Gremlin Server, then accepts a valid traversal."""
    validator = ManagedServerValidator("gremlin")
    try:
        good = validator.validate("g.V().limit(1)")
        bad = validator.validate("g.V(.limit(1)")
    finally:
        validator.close()
    assert good == [], f"managed Gremlin validator should accept a valid traversal, got: {good}"
    assert bad, "managed Gremlin validator should reject a malformed traversal"


@pytest.mark.usefixtures("docker_available")
def test_managed_validator_via_factory() -> None:
    """make_validator(..., 'managed') provisions and validates end-to-end."""
    validator = make_validator("cypher", "managed")
    assert isinstance(validator, ManagedServerValidator)
    try:
        assert validator.validate("MATCH (n) RETURN n LIMIT 1") == []
    finally:
        validator.close()


@pytest.mark.usefixtures("docker_available")
def test_managed_async_validator_matches_sync() -> None:
    """The async managed validator provisions and validates like the sync one."""

    async def run() -> list[str]:
        v = make_async_validator("cypher", "managed")
        assert isinstance(v, AsyncManagedServerValidator)
        try:
            return await v.validate("MATCH (n) RETURN n LIMIT 1")
        finally:
            await v.close()

    assert asyncio.run(run()) == []
