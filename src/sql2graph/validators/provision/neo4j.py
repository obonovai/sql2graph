"""Provision a throwaway Neo4j container for managed Cypher validation."""

from __future__ import annotations

from typing import Any

from sql2graph.validators.cypher.server import Neo4jConfig

# Neo4j server image used for managed validation. Pinned to the 5.x LTS
# line, which the installed neo4j driver connects to over Bolt.
IMAGE = "neo4j:5.26"
# Deterministic password for the disposable container: it is never exposed
# beyond the local Docker host and is torn down after validation.
_PASSWORD = "managedpassword"


def start() -> tuple[Any, Neo4jConfig]:
    """Start an empty Neo4j container and return ``(container, config)``.

    ``.start()`` blocks until Neo4j accepts Bolt connections (the
    testcontainers Neo4j module waits for the readiness log and a
    connectivity round-trip). ``get_connection_url()`` is assembled from the
    published host and port, so it honours ``TESTCONTAINERS_HOST_OVERRIDE``
    when the caller itself runs inside a container.
    """
    from testcontainers.neo4j import Neo4jContainer

    container = Neo4jContainer(IMAGE, password=_PASSWORD)
    container.start()
    config = Neo4jConfig(
        uri=container.get_connection_url(),
        username=container.username,
        password=container.password,
        database="neo4j",
        # The managed DB is empty, so unknown-label/property notifications are
        # guaranteed noise; tell the server not to send them.
        notifications_min_severity="OFF",
    )
    return container, config
