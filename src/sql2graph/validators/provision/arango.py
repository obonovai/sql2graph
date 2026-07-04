"""Provision a throwaway ArangoDB container for managed AQL validation."""

from __future__ import annotations

from typing import Any

from sql2graph.validators.aql.server import ArangoDBConfig

# ArangoDB server image used for managed validation.
IMAGE = "arangodb:3.11"
# Deterministic root password for the disposable container.
_PASSWORD = "managedpassword"


def start() -> tuple[Any, ArangoDBConfig]:
    """Start an empty ArangoDB container and return ``(container, config)``.

    ``.start()`` blocks until ArangoDB logs that it is ready. The connection
    URL is built from the published host and port, honouring
    ``TESTCONTAINERS_HOST_OVERRIDE`` when set.
    """
    from testcontainers.arangodb import ArangoDbContainer

    container = ArangoDbContainer(IMAGE, arango_root_password=_PASSWORD)
    container.start()
    config = ArangoDBConfig(
        url=container.get_connection_url(),
        username="root",
        password=_PASSWORD,
        database="_system",
    )
    return container, config
