"""AQL server-side validator (ArangoDB).

Submits each candidate query to ``db.aql.validate(query)`` against a live
ArangoDB instance. The endpoint parses the query without executing it,
making the check safe for any statement, and reports the syntax errors the
hand-ported grammar cannot.

``db.aql.validate`` parses only and never consults the collection
catalogue, so on its own it accepts a query over a collection that does not
exist. To catch that hallucination the validator additionally cross-checks
the collections the parse reports as referenced against the ones the
database actually holds (:func:`_unknown_collection_errors`). That
cross-check is governed by :attr:`ArangoDBConfig.check_collections`: enabled
by default for a populated user server, and disabled by managed validation,
whose empty throwaway database would otherwise flag every collection as
unknown.

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
    # When True (the default, for user-provided servers) the collection names a
    # query references are cross-checked against the database catalogue and
    # unknown ones reported as hallucinations. Managed validation sets this
    # False: its throwaway database is empty, so every collection would
    # otherwise look unknown.
    check_collections: bool = True


def _unknown_collection_errors(db: object, parsed: object) -> list[str]:
    """Report collections a parsed query references but the database lacks.

    ``db.aql.validate`` returns the collection names the query mentions but
    never checks they exist; this closes that gap by cross-checking them
    against ``db.collections()``. A failure to list collections degrades to no
    errors (rather than failing an otherwise valid query): the check can only
    add confidence, never withhold a verdict the parse already reached.
    """
    referenced: set[str] = set(parsed.get("collections") or [])  # type: ignore[attr-defined]
    if not referenced:
        return []
    try:
        existing = {c["name"] for c in db.collections()}  # type: ignore[attr-defined]
    except Exception as e:
        logger.info("AQL collection lookup failed: %s", e)
        return []
    return [
        f"Unknown collection '{name}': not present in database '{db.name}'."  # type: ignore[attr-defined]
        for name in sorted(referenced - existing)
    ]


def _validate_aql_sync(db: object, query: str, check_collections: bool) -> list[str]:
    """Shared validation body for sync and async server validators."""
    errors: list[str] = []
    try:
        parsed = db.aql.validate(query)  # type: ignore[attr-defined]
    except AQLQueryValidateError as e:
        logger.info("AQL validation error: %s", e)
        errors.append(str(e))
        return errors
    except Exception as e:
        logger.info("AQL validation failed: %s", e)
        errors.append(f"AQL validation failed: {e}")
        return errors
    if check_collections:
        errors.extend(_unknown_collection_errors(db, parsed))
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
        self._check_collections = config.check_collections

    def validate(self, query: str) -> list[str]:
        return _validate_aql_sync(self._db, query, self._check_collections)

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
        self._check_collections = config.check_collections

    async def validate(self, query: str) -> list[str]:
        return await asyncio.to_thread(_validate_aql_sync, self._db, query, self._check_collections)

    async def close(self) -> None:
        # python-arango uses pooled HTTP sessions; nothing persistent to close.
        return None
