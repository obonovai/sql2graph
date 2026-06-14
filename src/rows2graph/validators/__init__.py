"""Query validators and their typed configuration.

A :class:`QueryValidator` is the second half of the generate–validate–fix
loop: it inspects a candidate query and returns a list of error strings
(empty means valid). Three families ship:

* **Syntax** validators
  (:class:`~rows2graph.validators.cypher.syntax.CypherSyntaxValidator`,
  :class:`~rows2graph.validators.aql.syntax.AqlSyntaxValidator`,
  :class:`~rows2graph.validators.gremlin.syntax.GremlinSyntaxValidator`) —
  regex-based, deployment-free; catch obvious structural defects.
* **Server** validators
  (:class:`~rows2graph.validators.cypher.server.CypherServerValidator`,
  :class:`~rows2graph.validators.aql.server.AqlServerValidator`,
  :class:`~rows2graph.validators.gremlin.server.GremlinServerValidator`) —
  delegate validation to a live graph database via its
  parse-without-executing endpoint (Neo4j ``EXPLAIN``, ArangoDB
  ``db.aql.validate``, Gremlin Server script submission). Catches
  label/collection/property hallucinations on schema-aware backends
  (Neo4j, ArangoDB, JanusGraph); on schemaless TinkerGraph the Gremlin
  server validator only catches parse / step-compatibility errors.
* **No-op** (:class:`~rows2graph.validators.noop.NoopValidator`) — always
  reports success, so the loop exits after the first iteration. Used when
  measuring raw single-shot LLM quality.

The :class:`QueryValidator` Protocol is structural — implementations need
not inherit from anything in this module — which keeps the extension
surface clean.

Server-validator configs (:class:`Neo4jConfig`, :class:`ArangoDBConfig`,
:class:`GremlinConfig`) form a Pydantic-discriminated tagged union
:data:`ServerConfig`. The discriminator field ``type`` selects the
matching Pydantic subclass at YAML load time, and downstream code
dispatches via ``isinstance``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Protocol

import yaml
from pydantic import Field, TypeAdapter

from rows2graph._env import interpolate_env
from rows2graph.validators.aql.server import (
    AqlServerValidator,
    ArangoDBConfig,
    AsyncAqlServerValidator,
)
from rows2graph.validators.aql.syntax import AqlSyntaxValidator, AsyncAqlSyntaxValidator
from rows2graph.validators.cypher.server import (
    AsyncCypherServerValidator,
    CypherServerValidator,
    Neo4jConfig,
)
from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator, CypherSyntaxValidator
from rows2graph.validators.gremlin.server import (
    AsyncGremlinServerValidator,
    GremlinConfig,
    GremlinServerValidator,
)
from rows2graph.validators.gremlin.syntax import (
    AsyncGremlinSyntaxValidator,
    GremlinSyntaxValidator,
)
from rows2graph.validators.noop import AsyncNoopValidator, NoopValidator


class QueryValidator(Protocol):
    """Structural type for any query validator."""

    def validate(self, query: str) -> list[str]: ...

    def close(self) -> None: ...


class AsyncQueryValidator(Protocol):
    """Structural type for any async query validator.

    Consumed by :class:`rows2graph.async_translator.AsyncSQLTranslator`.
    Same shape as :class:`QueryValidator` with both methods made async.
    """

    async def validate(self, query: str) -> list[str]: ...

    async def close(self) -> None: ...


ServerConfig = Annotated[Neo4jConfig | ArangoDBConfig | GremlinConfig, Field(discriminator="type")]
"""Tagged union over every supported server-validator config."""

_SERVER_CONFIG_ADAPTER: TypeAdapter[Neo4jConfig | ArangoDBConfig | GremlinConfig] = TypeAdapter(ServerConfig)


def load_server_config(path: Path | str) -> Neo4jConfig | ArangoDBConfig | GremlinConfig:
    """Load and validate a server config YAML file.

    Environment-variable references (``${VAR}``) are interpolated before
    Pydantic validation; an undeclared variable raises :class:`KeyError`
    (typically what you want for passwords).
    """
    with open(path) as f:
        raw = yaml.safe_load(f)
    interpolated = interpolate_env(raw)
    return _SERVER_CONFIG_ADAPTER.validate_python(interpolated)


def make_validator(
    target: str,
    mode: str,
    *,
    server_config: Neo4jConfig | ArangoDBConfig | GremlinConfig | None = None,
) -> QueryValidator:
    """Construct a :class:`QueryValidator` from a target/mode pair.

    Args:
        target: ``"cypher"``, ``"aql"``, or ``"gremlin"``.
        mode: ``"syntax"``, ``"server"``, or ``"none"``.
        server_config: Required iff ``mode == "server"``. Must be a
            :class:`Neo4jConfig` when ``target == "cypher"``, an
            :class:`ArangoDBConfig` when ``target == "aql"``, and a
            :class:`GremlinConfig` when ``target == "gremlin"``.
    """
    if mode == "none":
        return NoopValidator()

    if mode == "syntax":
        if target == "cypher":
            return CypherSyntaxValidator()
        if target == "aql":
            return AqlSyntaxValidator()
        if target == "gremlin":
            return GremlinSyntaxValidator()
        raise ValueError(f"Unknown target language: {target!r}")

    if mode == "server":
        if server_config is None:
            raise ValueError(f"validation_mode='server' requires a server_config (target={target!r})")
        if target == "cypher":
            if not isinstance(server_config, Neo4jConfig):
                raise TypeError(
                    f"target='cypher' with mode='server' requires a Neo4jConfig, got {type(server_config).__name__}"
                )
            return CypherServerValidator(server_config)
        if target == "aql":
            if not isinstance(server_config, ArangoDBConfig):
                raise TypeError(
                    f"target='aql' with mode='server' requires an ArangoDBConfig, got {type(server_config).__name__}"
                )
            return AqlServerValidator(server_config)
        if target == "gremlin":
            if not isinstance(server_config, GremlinConfig):
                raise TypeError(
                    f"target='gremlin' with mode='server' requires a GremlinConfig, got {type(server_config).__name__}"
                )
            return GremlinServerValidator(server_config)
        raise ValueError(f"Unknown target language: {target!r}")

    raise ValueError(f"Unknown validation mode: {mode!r}. Supported: 'syntax', 'server', 'none'.")


def make_async_validator(
    target: str,
    mode: str,
    *,
    server_config: Neo4jConfig | ArangoDBConfig | GremlinConfig | None = None,
) -> AsyncQueryValidator:
    """Construct an :class:`AsyncQueryValidator` from a target/mode pair.

    Parallels :func:`make_validator` with the same target/mode/server_config
    contract — only the returned validator's interface is async.
    """
    if mode == "none":
        return AsyncNoopValidator()

    if mode == "syntax":
        if target == "cypher":
            return AsyncCypherSyntaxValidator()
        if target == "aql":
            return AsyncAqlSyntaxValidator()
        if target == "gremlin":
            return AsyncGremlinSyntaxValidator()
        raise ValueError(f"Unknown target language: {target!r}")

    if mode == "server":
        if server_config is None:
            raise ValueError(f"validation_mode='server' requires a server_config (target={target!r})")
        if target == "cypher":
            if not isinstance(server_config, Neo4jConfig):
                raise TypeError(
                    f"target='cypher' with mode='server' requires a Neo4jConfig, got {type(server_config).__name__}"
                )
            return AsyncCypherServerValidator(server_config)
        if target == "aql":
            if not isinstance(server_config, ArangoDBConfig):
                raise TypeError(
                    f"target='aql' with mode='server' requires an ArangoDBConfig, got {type(server_config).__name__}"
                )
            return AsyncAqlServerValidator(server_config)
        if target == "gremlin":
            if not isinstance(server_config, GremlinConfig):
                raise TypeError(
                    f"target='gremlin' with mode='server' requires a GremlinConfig, got {type(server_config).__name__}"
                )
            return AsyncGremlinServerValidator(server_config)
        raise ValueError(f"Unknown target language: {target!r}")

    raise ValueError(f"Unknown validation mode: {mode!r}. Supported: 'syntax', 'server', 'none'.")


__all__ = [
    "AqlServerValidator",
    "AqlSyntaxValidator",
    "ArangoDBConfig",
    "AsyncAqlServerValidator",
    "AsyncAqlSyntaxValidator",
    "AsyncCypherServerValidator",
    "AsyncCypherSyntaxValidator",
    "AsyncGremlinServerValidator",
    "AsyncGremlinSyntaxValidator",
    "AsyncNoopValidator",
    "AsyncQueryValidator",
    "CypherServerValidator",
    "CypherSyntaxValidator",
    "GremlinConfig",
    "GremlinServerValidator",
    "GremlinSyntaxValidator",
    "Neo4jConfig",
    "NoopValidator",
    "QueryValidator",
    "ServerConfig",
    "load_server_config",
    "make_async_validator",
    "make_validator",
]
