"""Gremlin server-side validator (Apache TinkerPop family).

The validator submits each candidate query as a Gremlin-Groovy script
to a live Gremlin Server and consumes the result. Any parse or step-
compatibility error surfaces as a :class:`Exception` (commonly
``GremlinServerError``) and is captured as a validation message.

Default backend is **Apache TinkerPop Gremlin Server with TinkerGraph**,
the official reference implementation, runnable in one line via the
``tinkerpop/gremlin-server`` Docker image and free for development /
CI / thesis evaluation. The same validator works against any
TinkerPop-compatible server (the script form is portable):

* **JanusGraph**: production-grade, recommended when stronger
  validation is needed because a registered JanusGraph schema lets the
  server reject label / property hallucinations the schemaless
  TinkerGraph cannot.
* **Amazon Neptune**: managed AWS service; switch ``url`` to the
  Neptune endpoint and configure IAM auth.
* **Azure Cosmos DB Gremlin API**: managed Azure service; switch
  ``url`` to the Cosmos endpoint. Note that Cosmos supports only a
  subset of Gremlin steps, so script-level validation may pass while
  the server rejects unsupported steps at execution time.

The :class:`GremlinConfig` Pydantic model is colocated with the
validator that consumes it, mirroring the placement of
:class:`~rows2graph.validators.cypher.server.Neo4jConfig` and
:class:`~rows2graph.validators.aql.server.ArangoDBConfig`.

Caveat: TinkerGraph is *schemaless*. Running validation against an
empty TinkerGraph catches script-level parse errors and unsupported
steps, but does NOT catch label / property hallucinations: the
server happily accepts `.hasLabel('Doesnotexist')` against an empty
graph and returns an empty result. Use JanusGraph with a registered
schema for schema-aware validation comparable to Neo4j's ``EXPLAIN``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from gremlin_python.driver.client import Client
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class GremlinConfig(BaseModel):
    """Connection settings for a Gremlin Server (TinkerPop-compatible).

    Used only when the demo is invoked with ``--validation server`` and
    ``--target gremlin``. The discriminator field ``type="gremlin"`` is
    what :data:`rows2graph.validators.ServerConfig` uses to dispatch
    :func:`rows2graph.validators.load_server_config` to this class.

    ``username`` and ``password`` are optional: TinkerGraph in its
    default Docker configuration accepts unauthenticated connections.
    Set both for authenticated backends (Neptune IAM, JanusGraph with
    SASL, etc.).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["gremlin"] = "gremlin"
    url: str = "ws://localhost:8182/gremlin"
    traversal_source: str = "g"
    username: str | None = None
    password: str | None = None


def _make_client(config: GremlinConfig) -> Any:
    """Build a :class:`gremlin_python.driver.client.Client` from a config."""
    kwargs: dict[str, Any] = {}
    if config.username is not None:
        kwargs["username"] = config.username
    if config.password is not None:
        kwargs["password"] = config.password
    return Client(config.url, config.traversal_source, **kwargs)


class GremlinServerValidator:
    """Validate Gremlin queries by submitting them to a Gremlin Server."""

    def __init__(self, config: GremlinConfig) -> None:
        self._client = _make_client(config)

    def validate(self, query: str) -> list[str]:
        errors: list[str] = []
        try:
            result_set = self._client.submit(query)
            # Force the request to round-trip: ``.all().result()`` blocks
            # until the server has finished (and would have raised on a
            # parse / step error).
            result_set.all().result()
        except Exception as e:
            logger.info("Gremlin validation error: %s", e)
            errors.append(str(e))
        return errors

    def close(self) -> None:
        self._client.close()


class AsyncGremlinServerValidator:
    """Async sibling of :class:`GremlinServerValidator`.

    ``gremlinpython``'s async surface area is inconsistent across
    releases, so the safer path is to wrap the sync :class:`Client`
    operations in :func:`asyncio.to_thread`. The validator is I/O-bound
    against a remote server; the thread hop is negligible compared to
    the network round-trip.
    """

    def __init__(self, config: GremlinConfig) -> None:
        self._client = _make_client(config)

    async def validate(self, query: str) -> list[str]:
        def _run() -> list[str]:
            errors: list[str] = []
            try:
                result_set = self._client.submit(query)
                result_set.all().result()
            except Exception as e:
                logger.info("Gremlin validation error: %s", e)
                errors.append(str(e))
            return errors

        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)
