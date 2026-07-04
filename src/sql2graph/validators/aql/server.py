"""AQL server-side validator (ArangoDB).

Submits each candidate query to ``db.aql.validate(query)`` against a live
ArangoDB instance. The endpoint parses the query without executing it,
making the check safe for any statement and catching syntax and
collection-name hallucinations that the regex syntax validator cannot
detect.

The :class:`ArangoDBConfig` Pydantic model is colocated with the validator
that consumes it. The framework uses bare edge-collection traversals
(``FOR v IN OUTBOUND <doc> <EdgeCollection>``), so no named graph is
referenced and none needs to be configured here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from arango.client import ArangoClient
from arango.exceptions import AQLQueryValidateError
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class ArangoDBConfig(BaseModel):
    """Connection settings for an ArangoDB instance.

    Used only under server validation for the aql target. The
    discriminator field ``type="arangodb"`` is what
    :data:`sql2graph.validators.ServerConfig` uses to dispatch
    :func:`sql2graph.validators.load_server_config` to this class.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["arangodb"] = "arangodb"
    url: str = "http://localhost:8529"
    username: str = "root"
    password: str
    database: str = "_system"


def _validate_aql_sync(db: object, query: str) -> list[str]:
    """Shared validation body for sync and async server validators."""
    errors: list[str] = []
    try:
        db.aql.validate(query)  # type: ignore[attr-defined]
    except AQLQueryValidateError as e:
        logger.info("AQL validation error: %s", e)
        errors.append(str(e))
    except Exception as e:
        logger.info("AQL validation failed: %s", e)
        errors.append(f"AQL validation failed: {e}")
    return errors


class AqlServerValidator:
    """Validate AQL queries via ``db.aql.validate`` against an ArangoDB server."""

    def __init__(self, config: ArangoDBConfig) -> None:
        self._client = ArangoClient(hosts=config.url)
        self._db = self._client.db(
            config.database,
            username=config.username,
            password=config.password,
        )

    def validate(self, query: str) -> list[str]:
        return _validate_aql_sync(self._db, query)

    def close(self) -> None:
        # python-arango uses pooled HTTP sessions; nothing persistent to close.
        return None


class AsyncAqlServerValidator:
    """Async sibling of :class:`AqlServerValidator`.

    python-arango ships no native async driver, so the sync HTTP call is
    pushed to a worker thread via :func:`asyncio.to_thread`. That keeps the
    event loop responsive without forcing the project onto an alternative
    aioarango stack.
    """

    def __init__(self, config: ArangoDBConfig) -> None:
        self._client = ArangoClient(hosts=config.url)
        self._db = self._client.db(
            config.database,
            username=config.username,
            password=config.password,
        )

    async def validate(self, query: str) -> list[str]:
        return await asyncio.to_thread(_validate_aql_sync, self._db, query)

    async def close(self) -> None:
        # python-arango uses pooled HTTP sessions; nothing persistent to close.
        return None
