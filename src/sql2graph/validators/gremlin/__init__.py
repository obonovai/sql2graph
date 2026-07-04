"""Gremlin validators (syntax + Apache TinkerPop server-side).

Mirrors the structure of :mod:`sql2graph.validators.cypher` and
:mod:`sql2graph.validators.aql`: a regex syntax validator that needs no
deployment, and a server validator that submits scripts to a live
Gremlin Server (TinkerGraph by default; JanusGraph / Neptune /
Cosmos DB Gremlin API are wire-compatible).
"""

from sql2graph.validators.gremlin.server import (
    AsyncGremlinServerValidator,
    GremlinConfig,
    GremlinServerValidator,
)
from sql2graph.validators.gremlin.syntax import (
    AsyncGremlinSyntaxValidator,
    GremlinSyntaxValidator,
)

__all__ = [
    "AsyncGremlinServerValidator",
    "AsyncGremlinSyntaxValidator",
    "GremlinConfig",
    "GremlinServerValidator",
    "GremlinSyntaxValidator",
]
