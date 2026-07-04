"""Cypher server-side validator (Neo4j).

The validator submits each candidate query as ``EXPLAIN <query>`` against a
live Neo4j instance. ``EXPLAIN`` parses and plans the query without
executing it, so the check is safe to run for arbitrary statements
(including writes and deletes) and catches the entire class of errors
the syntax validator misses (non-existent labels, non-existent
relationship types, non-existent properties) that the LLM is most prone
to hallucinate.

The :class:`Neo4jConfig` Pydantic model is colocated with the validator
that consumes it, rather than living in a shared config module. This
follows the design principle that *configuration belongs next to the
component that interprets it*.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from neo4j import AsyncGraphDatabase, GraphDatabase
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class Neo4jConfig(BaseModel):
    """Connection settings for a Neo4j instance.

    Used only under server validation for the cypher target. The
    discriminator field ``type="neo4j"`` is what
    :data:`sql2graph.validators.ServerConfig` uses to dispatch
    :func:`sql2graph.validators.load_server_config` to this class.

    The ``password`` field has no default; a real password (or an
    environment-variable reference like ``${NEO4J_PASSWORD}``) must be
    provided.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["neo4j"] = "neo4j"
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str
    database: str = "neo4j"
    # Server-side notification filter forwarded to the driver. ``"OFF"`` stops
    # the server sending advisory notifications (e.g. unknown-label/property
    # warnings against an empty database); managed validation sets this to keep
    # output clean. ``None`` leaves the driver default (notifications enabled).
    notifications_min_severity: Literal["OFF", "INFORMATION", "WARNING"] | None = None


class CypherServerValidator:
    """Validate Cypher queries by running ``EXPLAIN`` against a Neo4j server."""

    def __init__(self, config: Neo4jConfig) -> None:
        kwargs: dict[str, Any] = {}
        if config.notifications_min_severity is not None:
            kwargs["notifications_min_severity"] = config.notifications_min_severity
        self._driver = GraphDatabase.driver(
            config.uri,
            auth=(config.username, config.password),
            **kwargs,
        )
        self._database = config.database

    def validate(self, query: str) -> list[str]:
        errors: list[str] = []
        try:
            with self._driver.session(database=self._database) as session:
                session.run(f"EXPLAIN {query}").consume()
        except Exception as e:
            logger.info("Cypher validation error: %s", e)
            errors.append(str(e))
        return errors

    def close(self) -> None:
        self._driver.close()


class AsyncCypherServerValidator:
    """Async sibling of :class:`CypherServerValidator`.

    Uses :class:`neo4j.AsyncGraphDatabase` to drive the same ``EXPLAIN``
    round-trip without blocking the event loop. Same :class:`Neo4jConfig`:
    both validators consume the same loaded config.
    """

    def __init__(self, config: Neo4jConfig) -> None:
        kwargs: dict[str, Any] = {}
        if config.notifications_min_severity is not None:
            kwargs["notifications_min_severity"] = config.notifications_min_severity
        self._driver = AsyncGraphDatabase.driver(
            config.uri,
            auth=(config.username, config.password),
            **kwargs,
        )
        self._database = config.database

    async def validate(self, query: str) -> list[str]:
        errors: list[str] = []
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run(f"EXPLAIN {query}")
                await result.consume()
        except Exception as e:
            logger.info("Cypher validation error: %s", e)
            errors.append(str(e))
        return errors

    async def close(self) -> None:
        await self._driver.close()
