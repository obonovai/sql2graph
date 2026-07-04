"""Query validators and their typed configuration.

A :class:`QueryValidator` is the second half of the generate-validate-fix
loop: it inspects a candidate query and returns a list of error strings
(empty means valid). Three families ship:

* **Syntax** validators
  (:class:`~sql2graph.validators.cypher.syntax.CypherSyntaxValidator`,
  :class:`~sql2graph.validators.gremlin.syntax.GremlinSyntaxValidator`,
  :class:`~sql2graph.validators.aql.syntax.AqlSyntaxValidator`):
  grammar-based (ANTLR), deployment-free; catch structural / grammar errors
  with no database. Cypher and Gremlin use each engine's own published
  grammar; AQL uses a hand-port of ArangoDB's Flex+Bison parser (ArangoDB
  ships no reusable offline grammar), so its syntax check is best-effort and
  the server mode below remains authoritative.
* **Server** validators
  (:class:`~sql2graph.validators.cypher.server.CypherServerValidator`,
  :class:`~sql2graph.validators.aql.server.AqlServerValidator`,
  :class:`~sql2graph.validators.gremlin.server.GremlinServerValidator`):
  delegate validation to a live graph database via its
  parse-without-executing endpoint (Neo4j ``EXPLAIN``, ArangoDB
  ``db.aql.validate``, Gremlin Server script submission). Catches
  label/collection/property hallucinations on schema-aware backends
  (Neo4j, ArangoDB, JanusGraph); on schemaless TinkerGraph the Gremlin
  server validator only catches parse / step-compatibility errors.
* **No-op** (:class:`~sql2graph.validators.noop.NoopValidator`): always
  reports success, so the loop exits after the first iteration. Used when
  measuring raw single-shot LLM quality.

The :class:`QueryValidator` Protocol is structural (implementations need
not inherit from anything in this module), which keeps the extension
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

from sql2graph._env import interpolate_env
from sql2graph.validators.aql.server import (
    AqlServerValidator,
    ArangoDBConfig,
    AsyncAqlServerValidator,
)
from sql2graph.validators.aql.syntax import AqlSyntaxValidator, AsyncAqlSyntaxValidator
from sql2graph.validators.cypher.server import (
    AsyncCypherServerValidator,
    CypherServerValidator,
    Neo4jConfig,
)
from sql2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator, CypherSyntaxValidator
from sql2graph.validators.gremlin.server import (
    AsyncGremlinServerValidator,
    GremlinConfig,
    GremlinServerValidator,
)
from sql2graph.validators.gremlin.syntax import (
    AsyncGremlinSyntaxValidator,
    GremlinSyntaxValidator,
)
from sql2graph.validators.noop import AsyncNoopValidator, NoopValidator
from sql2graph.validators.provision import AsyncManagedServerValidator, ManagedServerValidator


class QueryValidator(Protocol):
    """Structural type for any query validator."""

    def validate(self, query: str) -> list[str]: ...

    def close(self) -> None: ...


class AsyncQueryValidator(Protocol):
    """Structural type for any async query validator.

    Consumed by :class:`sql2graph.async_translator.AsyncSQLTranslator`.
    Same shape as :class:`QueryValidator` with both methods made async.
    """

    async def validate(self, query: str) -> list[str]: ...

    async def close(self) -> None: ...


# User-facing validation modes (``managed`` is derived, not chosen directly; see
# ``resolve_validation_mode``). The canonical set, so callers/CLIs/web UIs don't
# each hardcode their own copy.
VALID_VALIDATION_MODES: tuple[str, ...] = ("none", "syntax", "server")

# Which server-config / database type each target language validates against.
TARGET_SERVER_TYPE: dict[str, str] = {"cypher": "neo4j", "aql": "arangodb", "gremlin": "gremlin"}


def resolve_validation_mode(mode: str, *, server_config: object | None) -> str:
    """Resolve the *effective* validation mode.

    ``"server"`` with no ``server_config`` means *managed*: the library provisions
    a throwaway database itself. Every other case passes ``mode`` through unchanged.
    Centralised here so callers (e.g. the web backend) share one rule.
    """
    if mode == "server" and server_config is None:
        return "managed"
    return mode


def valid_modes_for_target(target: str) -> tuple[str, ...]:
    """Validation modes available for a target language.

    Centralises the per-target rule so callers don't each hardcode it
    (mirrors :data:`VALID_VALIDATION_MODES` and :func:`resolve_validation_mode`).
    All three targets now offer the same modes; AQL's ``"syntax"`` mode uses a
    hand-ported ArangoDB grammar (see :mod:`sql2graph.validators.aql.syntax`).
    """
    if target in ("cypher", "gremlin", "aql"):
        return ("none", "syntax", "server")
    raise ValueError(f"Unknown target language: {target!r}")


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
        mode: ``"syntax"``, ``"server"``, ``"managed"``, or ``"none"``.
        server_config: Required iff ``mode == "server"`` (ignored when
            ``mode == "managed"``, which provisions its own database). Must be a
            :class:`Neo4jConfig` when ``target == "cypher"``, an
            :class:`ArangoDBConfig` when ``target == "aql"``, and a
            :class:`GremlinConfig` when ``target == "gremlin"``.
    """
    if mode == "none":
        return NoopValidator()

    if mode == "syntax":
        if target == "cypher":
            return CypherSyntaxValidator()
        if target == "gremlin":
            return GremlinSyntaxValidator()
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
        if target == "gremlin":
            if not isinstance(server_config, GremlinConfig):
                raise TypeError(
                    f"target='gremlin' with mode='server' requires a GremlinConfig, got {type(server_config).__name__}"
                )
            return GremlinServerValidator(server_config)
        raise ValueError(f"Unknown target language: {target!r}")

    if mode == "managed":
        if target not in ("cypher", "aql", "gremlin"):
            raise ValueError(f"Unknown target language: {target!r}")
        # server_config is intentionally ignored: managed mode provisions its own.
        return ManagedServerValidator(target)

    raise ValueError(f"Unknown validation mode: {mode!r}. Supported: 'syntax', 'server', 'managed', 'none'.")


def make_async_validator(
    target: str,
    mode: str,
    *,
    server_config: Neo4jConfig | ArangoDBConfig | GremlinConfig | None = None,
) -> AsyncQueryValidator:
    """Construct an :class:`AsyncQueryValidator` from a target/mode pair.

    Parallels :func:`make_validator` with the same target/mode/server_config
    contract; only the returned validator's interface is async.
    """
    if mode == "none":
        return AsyncNoopValidator()

    if mode == "syntax":
        if target == "cypher":
            return AsyncCypherSyntaxValidator()
        if target == "gremlin":
            return AsyncGremlinSyntaxValidator()
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
        if target == "gremlin":
            if not isinstance(server_config, GremlinConfig):
                raise TypeError(
                    f"target='gremlin' with mode='server' requires a GremlinConfig, got {type(server_config).__name__}"
                )
            return AsyncGremlinServerValidator(server_config)
        raise ValueError(f"Unknown target language: {target!r}")

    if mode == "managed":
        if target not in ("cypher", "aql", "gremlin"):
            raise ValueError(f"Unknown target language: {target!r}")
        # server_config is intentionally ignored: managed mode provisions its own.
        return AsyncManagedServerValidator(target)

    raise ValueError(f"Unknown validation mode: {mode!r}. Supported: 'syntax', 'server', 'managed', 'none'.")


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
    "AsyncManagedServerValidator",
    "AsyncNoopValidator",
    "AsyncQueryValidator",
    "CypherServerValidator",
    "CypherSyntaxValidator",
    "GremlinConfig",
    "GremlinServerValidator",
    "GremlinSyntaxValidator",
    "ManagedServerValidator",
    "Neo4jConfig",
    "NoopValidator",
    "QueryValidator",
    "ServerConfig",
    "TARGET_SERVER_TYPE",
    "VALID_VALIDATION_MODES",
    "load_server_config",
    "make_async_validator",
    "make_validator",
    "resolve_validation_mode",
    "valid_modes_for_target",
]
