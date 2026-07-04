"""Auto-provisioned ("managed") throwaway databases for server validation.

When server validation is requested *without* a connection config, the library
starts a disposable graph database in a Docker container (via ``testcontainers``),
points the matching server validator at it, and tears the container down when the
validator is closed. Users no longer need to run or configure their own Neo4j /
ArangoDB / Gremlin instance.

Per-engine provisioning lives in the sibling modules :mod:`.neo4j`, :mod:`.arango`,
and :mod:`.gremlin`; each exposes ``start() -> (container, config)``. This module
hosts the engine-agnostic validators that drive them. ``testcontainers`` is
imported lazily inside each ``start()`` so importing :mod:`sql2graph` never
requires Docker.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from sql2graph.validators.aql.server import (
    AqlServerValidator,
    ArangoDBConfig,
    AsyncAqlServerValidator,
)
from sql2graph.validators.cypher.server import (
    AsyncCypherServerValidator,
    CypherServerValidator,
    Neo4jConfig,
)
from sql2graph.validators.gremlin.server import (
    AsyncGremlinServerValidator,
    GremlinConfig,
    GremlinServerValidator,
)
from sql2graph.validators.provision import arango, gremlin, neo4j

logger = logging.getLogger(__name__)

_ServerConfig = Neo4jConfig | ArangoDBConfig | GremlinConfig

# Maps a target language to the function that starts its database container.
_PROVISIONERS: dict[str, Callable[[], tuple[Any, _ServerConfig]]] = {
    "cypher": neo4j.start,
    "aql": arango.start,
    "gremlin": gremlin.start,
}


def _provision(target: str) -> tuple[Any, _ServerConfig]:
    """Start a throwaway database for ``target``, mapping failures to a clear error."""
    try:
        start = _PROVISIONERS[target]
    except KeyError:
        raise ValueError(f"Unknown target language: {target!r}") from None
    try:
        return start()
    except ImportError as e:
        raise RuntimeError(
            "Managed validation requires the 'testcontainers' package (installed with sql2graph by default)."
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"Managed validation could not start a {target!r} database. Is Docker installed and running? ({e})"
        ) from e


class ManagedServerValidator:
    """Server validator that provisions and owns a throwaway database.

    On the first :meth:`validate` call it starts a disposable container for
    ``target`` (Neo4j for ``cypher``, ArangoDB for ``aql``, Gremlin Server for
    ``gremlin``), builds the matching server validator against it, and delegates.
    :meth:`close` stops the container and is safe to call repeatedly or when the
    container was never started.
    """

    def __init__(self, target: str) -> None:
        if target not in _PROVISIONERS:
            raise ValueError(f"Unknown target language: {target!r}")
        self._target = target
        self._container: Any | None = None
        self._inner: CypherServerValidator | AqlServerValidator | GremlinServerValidator | None = None

    def _ensure_started(self) -> None:
        if self._inner is not None:
            return
        container, config = _provision(self._target)
        # Assign the container first so close() can stop it even if building the
        # inner validator below raises.
        self._container = container
        if isinstance(config, Neo4jConfig):
            self._inner = CypherServerValidator(config)
        elif isinstance(config, ArangoDBConfig):
            self._inner = AqlServerValidator(config)
        else:
            self._inner = GremlinServerValidator(config)

    def validate(self, query: str) -> list[str]:
        self._ensure_started()
        assert self._inner is not None
        return self._inner.validate(query)

    def warmup(self) -> None:
        """Start the throwaway database now (idempotent).

        Lets callers provision before timing/looping so one-off container setup
        is not counted as translation latency; ``validate`` would otherwise start
        it lazily on first use.
        """
        self._ensure_started()

    def close(self) -> None:
        if self._inner is not None:
            try:
                self._inner.close()
            except Exception as e:
                logger.warning("Error closing managed inner validator: %s", e)
            self._inner = None
        if self._container is not None:
            try:
                self._container.stop()
            except Exception as e:
                logger.warning("Error stopping managed container: %s", e)
            self._container = None


class AsyncManagedServerValidator:
    """Async sibling of :class:`ManagedServerValidator`.

    Container startup and teardown are blocking, so they run in a worker thread
    via :func:`asyncio.to_thread`; an :class:`asyncio.Lock` guarantees a single
    container is started even under concurrent :meth:`validate` calls.
    """

    def __init__(self, target: str) -> None:
        if target not in _PROVISIONERS:
            raise ValueError(f"Unknown target language: {target!r}")
        self._target = target
        self._container: Any | None = None
        self._inner: AsyncCypherServerValidator | AsyncAqlServerValidator | AsyncGremlinServerValidator | None = None
        self._lock = asyncio.Lock()

    async def _ensure_started(self) -> None:
        # Always acquire the lock (cheap when uncontended) so a single
        # container is started even under concurrent validate() calls.
        async with self._lock:
            if self._inner is not None:
                return
            container, config = await asyncio.to_thread(_provision, self._target)
            self._container = container
            if isinstance(config, Neo4jConfig):
                self._inner = AsyncCypherServerValidator(config)
            elif isinstance(config, ArangoDBConfig):
                self._inner = AsyncAqlServerValidator(config)
            else:
                self._inner = AsyncGremlinServerValidator(config)

    async def validate(self, query: str) -> list[str]:
        await self._ensure_started()
        assert self._inner is not None
        return await self._inner.validate(query)

    async def warmup(self) -> None:
        """Start the throwaway database now (idempotent).

        Lets callers provision before timing/looping so one-off container setup
        is not counted as translation latency; ``validate`` would otherwise start
        it lazily on first use.
        """
        await self._ensure_started()

    async def close(self) -> None:
        if self._inner is not None:
            try:
                await self._inner.close()
            except Exception as e:
                logger.warning("Error closing managed inner validator: %s", e)
            self._inner = None
        if self._container is not None:
            try:
                await asyncio.to_thread(self._container.stop)
            except Exception as e:
                logger.warning("Error stopping managed container: %s", e)
            self._container = None


__all__ = [
    "AsyncManagedServerValidator",
    "ManagedServerValidator",
]
