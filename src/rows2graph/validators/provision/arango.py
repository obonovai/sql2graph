"""Provision a throwaway ArangoDB container for managed AQL validation."""

from __future__ import annotations

from typing import Any

from rows2graph.validators.aql.server import ArangoDBConfig

# ArangoDB server image used for managed validation.
IMAGE = "arangodb:3.11"
# Deterministic root password for the disposable container.
_PASSWORD = "managedpassword"
# ``ArangoDBConfig`` requires ``graph_name``, but the validator only calls
# ``db.aql.validate`` — a static parse check that never dereferences the
# graph — so a placeholder is sufficient for an empty managed database.
_GRAPH_NAME = "managed"


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
        graph_name=_GRAPH_NAME,
    )
    return container, config
