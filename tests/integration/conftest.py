"""Shared fixtures for the integration suite.

These hit real LLMs / databases and skip themselves when the relevant
credential env var is missing, so a partial setup still gets useful coverage.
See ``tests/README.md`` for the full env-var list and a docker-compose recipe.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from sql2graph import (
    AnthropicConfig,
    EdgeMapping,
    Neo4jConfig,
    NodeMapping,
    SchemaMapping,
)


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
def seeded_neo4j_config(neo4j_config: Neo4jConfig) -> Iterator[Neo4jConfig]:
    """``neo4j_config`` with the ``small_schema`` labels/type/property keys present.

    Server validation now reports unknown-label/property notifications, so the
    end-to-end loop only converges when the mapping's schema actually exists in
    the database. This seeds one node per label (which registers the label and
    property-key tokens) plus a ``MEMBER_OF`` edge, yields the same config, and
    removes the seed nodes on teardown. Uses ``id = -1`` so it never collides
    with real data.
    """
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(neo4j_config.uri, auth=(neo4j_config.username, neo4j_config.password))
    seed = (
        "MERGE (p:Person {id: -1}) SET p.name = 'seed' "
        "MERGE (f:Forum {id: -1}) SET f.title = 'seed' "
        "MERGE (p)-[:MEMBER_OF]->(f)"
    )
    try:
        with driver.session(database=neo4j_config.database) as s:
            s.run(seed).consume()
        yield neo4j_config
    finally:
        with driver.session(database=neo4j_config.database) as s:
            s.run("MATCH (n) WHERE (n:Person OR n:Forum) AND n.id = -1 DETACH DELETE n").consume()
        driver.close()


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
    """A minimal two-node, one-edge schema, small enough to keep prompts cheap."""
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
