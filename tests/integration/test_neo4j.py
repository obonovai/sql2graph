"""Integration tests exercising a real Neo4j server validator."""

from __future__ import annotations

import asyncio

import pytest

from sql2graph import (
    AnthropicConfig,
    AnthropicLLMClient,
    CypherTarget,
    Neo4jConfig,
    SchemaMapping,
    SQLTranslator,
)
from sql2graph.validators.cypher.server import (
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
        # Malformed syntax is raised by EXPLAIN itself (not an advisory
        # notification), so a deliberately broken paren balance fails
        # regardless of what the database contains.
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


def test_real_neo4j_reports_unknown_label_hallucination(
    neo4j_config: Neo4jConfig,
) -> None:
    """A reference to a non-existent label is surfaced, not silently dropped.

    Neo4j reports this as an advisory notification rather than raising it, so the
    validator must read the ``EXPLAIN`` summary and turn the ``UNRECOGNIZED``
    class into an error. The label below is seeded by no test, so it is
    guaranteed absent from any database this runs against.
    """
    validator = CypherServerValidator(neo4j_config)
    try:
        errors = validator.validate("MATCH (n:Zzz_NoSuchLabel_9f3a) RETURN n")
    finally:
        validator.close()
    assert errors, "expected the unknown label to be reported as a hallucination"


def test_real_neo4j_accepts_seeded_label(
    seeded_neo4j_config: Neo4jConfig,
) -> None:
    """A query over a label that exists produces no hallucination error."""
    validator = CypherServerValidator(seeded_neo4j_config)
    try:
        errors = validator.validate("MATCH (n:Person) RETURN n.name LIMIT 1")
    finally:
        validator.close()
    assert errors == [], f"expected no errors for a seeded label, got: {errors}"


def test_real_neo4j_notifications_off_suppresses_hallucination(
    neo4j_config: Neo4jConfig,
) -> None:
    """``notifications_min_severity='OFF'`` silences the hallucination report.

    This is exactly the mechanism managed mode uses so its empty database does
    not flag every label as unknown.
    """
    off_config = neo4j_config.model_copy(update={"notifications_min_severity": "OFF"})
    validator = CypherServerValidator(off_config)
    try:
        errors = validator.validate("MATCH (n:Zzz_NoSuchLabel_9f3a) RETURN n")
    finally:
        validator.close()
    assert errors == [], f"OFF should suppress hallucination notifications, got: {errors}"


@pytest.mark.usefixtures("docker_available")
def test_provisioned_neo4j_reports_and_suppresses_label_hallucination() -> None:
    """Docker-only proof (no NEO4J_PASSWORD needed) of both halves of the feature.

    Provisions the same throwaway Neo4j managed mode uses, then flips
    notifications back on to emulate a populated user server: an unknown label is
    reported, a seeded label passes, and with notifications OFF the same unknown
    label is suppressed.
    """
    from neo4j import GraphDatabase

    from sql2graph.validators.provision import neo4j as neo4j_provision

    container, managed_config = neo4j_provision.start()  # notifications OFF
    try:
        reporting_config = managed_config.model_copy(update={"notifications_min_severity": None})
        driver = GraphDatabase.driver(
            reporting_config.uri, auth=(reporting_config.username, reporting_config.password)
        )
        try:
            with driver.session(database=reporting_config.database) as s:
                s.run("MERGE (p:Person {id: -1}) SET p.name = 'seed'").consume()
        finally:
            driver.close()

        reporting = CypherServerValidator(reporting_config)
        try:
            unknown = reporting.validate("MATCH (n:Zzz_NoSuchLabel_9f3a) RETURN n")
            known = reporting.validate("MATCH (n:Person) RETURN n.name LIMIT 1")
        finally:
            reporting.close()
        assert unknown, "expected the unknown label to be reported as a hallucination"
        assert known == [], f"a seeded label must pass, got: {known}"

        suppressed_validator = CypherServerValidator(managed_config)  # notifications OFF
        try:
            suppressed = suppressed_validator.validate("MATCH (n:Zzz_NoSuchLabel_9f3a) RETURN n")
        finally:
            suppressed_validator.close()
        assert suppressed == [], f"OFF should suppress hallucination notifications, got: {suppressed}"
    finally:
        container.stop()


def test_real_full_loop_anthropic_with_neo4j_server_validation(
    anthropic_config: AnthropicConfig,
    seeded_neo4j_config: Neo4jConfig,
    small_schema: SchemaMapping,
) -> None:
    """End-to-end: translate against real LLM, validate against real Neo4j EXPLAIN.

    Runs against a database seeded with the mapping's schema so that reporting of
    unknown-label/property hallucinations does not spuriously stall the loop.
    """
    with SQLTranslator(
        schema_mapping=small_schema,
        llm=AnthropicLLMClient(anthropic_config),
        target=CypherTarget(),
        validator=CypherServerValidator(seeded_neo4j_config),
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
