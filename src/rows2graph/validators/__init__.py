"""Query validators and their typed configuration.

A :class:`QueryValidator` is the second half of the generate–validate–fix
loop: it inspects a candidate query and returns a list of error strings
(empty means valid). Three families ship:

* **Syntax** validators
  (:class:`~rows2graph.validators.cypher.syntax.CypherSyntaxValidator`,
  :class:`~rows2graph.validators.aql.syntax.AqlSyntaxValidator`) —
  regex-based, deployment-free; catch obvious structural defects.
* **Server** validators
  (:class:`~rows2graph.validators.cypher.server.CypherServerValidator`,
  :class:`~rows2graph.validators.aql.server.AqlServerValidator`) —
  delegate validation to a live graph database via its
  parse-without-executing endpoint (Neo4j ``EXPLAIN``, ArangoDB
  ``db.aql.validate``). Catches label/collection/property hallucinations
  in addition to syntactic defects.
* **No-op** (:class:`~rows2graph.validators.noop.NoopValidator`) — always
  reports success, so the loop exits after the first iteration. Used when
  measuring raw single-shot LLM quality.

The :class:`QueryValidator` Protocol is structural — implementations need
not inherit from anything in this module — which keeps the extension
surface clean.

Server-validator configs (:class:`Neo4jConfig`, :class:`ArangoDBConfig`)
form a Pydantic-discriminated tagged union :data:`ServerConfig`. The
discriminator field ``type`` selects the matching Pydantic subclass at YAML
load time, and downstream code dispatches via ``isinstance``.
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


ServerConfig = Annotated[Neo4jConfig | ArangoDBConfig, Field(discriminator="type")]
"""Tagged union over every supported server-validator config."""

_SERVER_CONFIG_ADAPTER: TypeAdapter[Neo4jConfig | ArangoDBConfig] = TypeAdapter(ServerConfig)


def load_server_config(path: Path | str) -> Neo4jConfig | ArangoDBConfig:
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
    server_config: Neo4jConfig | ArangoDBConfig | None = None,
) -> QueryValidator:
    """Construct a :class:`QueryValidator` from a target/mode pair.

    Args:
        target: ``"cypher"`` or ``"aql"``.
        mode: ``"syntax"``, ``"server"``, or ``"none"``.
        server_config: Required iff ``mode == "server"``. Must be a
            :class:`Neo4jConfig` when ``target == "cypher"`` and an
            :class:`ArangoDBConfig` when ``target == "aql"``.
    """
    if mode == "none":
        return NoopValidator()

    if mode == "syntax":
        if target == "cypher":
            return CypherSyntaxValidator()
        if target == "aql":
            return AqlSyntaxValidator()
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
        raise ValueError(f"Unknown target language: {target!r}")

    raise ValueError(f"Unknown validation mode: {mode!r}. Supported: 'syntax', 'server', 'none'.")


def make_async_validator(
    target: str,
    mode: str,
    *,
    server_config: Neo4jConfig | ArangoDBConfig | None = None,
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
    "AsyncNoopValidator",
    "AsyncQueryValidator",
    "CypherServerValidator",
    "CypherSyntaxValidator",
    "Neo4jConfig",
    "NoopValidator",
    "QueryValidator",
    "ServerConfig",
    "load_server_config",
    "make_async_validator",
    "make_validator",
]
