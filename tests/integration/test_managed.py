"""Integration tests exercising managed (auto-provisioned) server validators."""

from __future__ import annotations

import asyncio

import pytest

from sql2graph import (
    AqlSyntaxValidator,
    AsyncManagedServerValidator,
    ManagedServerValidator,
    make_async_validator,
    make_validator,
)

pytestmark = pytest.mark.integration


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
def test_managed_cypher_suppresses_label_hallucination() -> None:
    """Managed Cypher must not flag an unknown label: its empty DB sets
    notifications_min_severity='OFF', so every label would otherwise look unknown."""
    validator = ManagedServerValidator("cypher")
    try:
        errors = validator.validate("MATCH (n:Zzz_NoSuchLabel_9f3a) RETURN n")
    finally:
        validator.close()
    assert errors == [], f"managed mode should suppress label hallucinations, got: {errors}"


@pytest.mark.usefixtures("docker_available")
def test_managed_aql_suppresses_collection_hallucination() -> None:
    """Managed AQL must not flag an unknown collection: provisioning sets
    check_collections=False so the empty catalogue is not treated as hallucinated."""
    validator = ManagedServerValidator("aql")
    try:
        errors = validator.validate("FOR x IN zzz_no_such_collection RETURN x")
    finally:
        validator.close()
    assert errors == [], f"managed mode should suppress collection hallucinations, got: {errors}"


@pytest.mark.usefixtures("docker_available")
def test_aql_offline_syntax_agrees_with_server() -> None:
    """The offline (hand-ported) AQL grammar should agree with ArangoDB's own parser.

    Guards the hand-port against drift: every query is validated both by the
    deployment-free ``AqlSyntaxValidator`` and by ArangoDB's ``db.aql.validate``
    (via the managed validator). They must agree on accept vs. reject for this
    purely-syntactic corpus.
    """
    valid = [
        "RETURN 1",
        "FOR u IN users FILTER u.age > 20 SORT u.name DESC LIMIT 10 RETURN u.name",
        "FOR u IN users COLLECT c = u.city INTO g RETURN { c, n: LENGTH(g) }",
        "FOR u IN users COLLECT WITH COUNT INTO total RETURN total",
        "RETURN x NOT IN [1, 2, 3]",
        "RETURN a ? b : c",
        "RETURN [1, 2, 3][*]",
    ]
    invalid = [
        "FOR u IN RETURN u",
        "RETURN",
        "RETURN (1 + )",
        "SELECT * FROM users",
        "FOR u IN users RETURN u EXTRA",
    ]
    offline = AqlSyntaxValidator()
    server = ManagedServerValidator("aql")
    try:
        for q in valid:
            assert offline.validate(q) == [], f"offline rejected valid AQL: {q!r}"
            assert server.validate(q) == [], f"server rejected valid AQL: {q!r}"
        for q in invalid:
            assert offline.validate(q), f"offline accepted invalid AQL: {q!r}"
            assert server.validate(q), f"server accepted invalid AQL: {q!r}"
    finally:
        offline.close()
        server.close()


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
