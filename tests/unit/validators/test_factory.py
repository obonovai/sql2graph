"""Unit tests for validator factory dispatch (make_validator / make_async_validator)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from sql2graph import (
    AqlSyntaxValidator,
    ArangoDBConfig,
    CypherSyntaxValidator,
    GremlinConfig,
    GremlinSyntaxValidator,
    Neo4jConfig,
    NoopValidator,
    make_validator,
    valid_modes_for_target,
)


def test_make_validator_noop() -> None:
    v = make_validator("cypher", "none")
    assert isinstance(v, NoopValidator)


def test_make_validator_cypher_syntax() -> None:
    v = make_validator("cypher", "syntax")
    assert isinstance(v, CypherSyntaxValidator)


def test_make_validator_aql_syntax() -> None:
    v = make_validator("aql", "syntax")
    assert isinstance(v, AqlSyntaxValidator)


def test_valid_modes_for_target() -> None:
    assert valid_modes_for_target("cypher") == ("none", "syntax", "server")
    assert valid_modes_for_target("gremlin") == ("none", "syntax", "server")
    assert valid_modes_for_target("aql") == ("none", "syntax", "server")


def test_make_validator_gremlin_syntax() -> None:
    v = make_validator("gremlin", "syntax")
    assert isinstance(v, GremlinSyntaxValidator)


def test_make_validator_cypher_server_requires_neo4j_config() -> None:
    with pytest.raises(ValueError, match="requires a server_config"):
        make_validator("cypher", "server")


def test_make_validator_cypher_server_rejects_arangodb_config() -> None:
    arango_config = ArangoDBConfig(password="p")
    with pytest.raises(TypeError, match="Neo4jConfig"):
        make_validator("cypher", "server", server_config=arango_config)


def test_make_validator_aql_server_rejects_neo4j_config() -> None:
    neo4j_config = Neo4jConfig(password="p")
    with pytest.raises(TypeError, match="ArangoDBConfig"):
        make_validator("aql", "server", server_config=neo4j_config)


def test_make_validator_cypher_server_constructs_with_neo4j() -> None:
    with patch("sql2graph.validators.cypher.server.GraphDatabase") as mock_gdb:
        mock_gdb.driver = MagicMock()
        v = make_validator(
            "cypher",
            "server",
            server_config=Neo4jConfig(password="secret"),
        )
        from sql2graph.validators.cypher.server import CypherServerValidator

        assert isinstance(v, CypherServerValidator)
        mock_gdb.driver.assert_called_once()


def test_neo4j_config_notifications_min_severity_defaults_to_none() -> None:
    assert Neo4jConfig(password="x").notifications_min_severity is None


def test_neo4j_config_rejects_invalid_notifications_min_severity() -> None:
    with pytest.raises(ValidationError):
        Neo4jConfig(password="x", notifications_min_severity="LOUD")  # type: ignore[arg-type]


def test_cypher_server_validator_forwards_notifications_min_severity() -> None:
    from sql2graph.validators.cypher.server import CypherServerValidator

    with patch("sql2graph.validators.cypher.server.GraphDatabase") as mock_gdb:
        CypherServerValidator(Neo4jConfig(password="secret", notifications_min_severity="OFF"))
        assert mock_gdb.driver.call_args.kwargs.get("notifications_min_severity") == "OFF"


def test_cypher_server_validator_omits_notifications_min_severity_when_unset() -> None:
    from sql2graph.validators.cypher.server import CypherServerValidator

    with patch("sql2graph.validators.cypher.server.GraphDatabase") as mock_gdb:
        CypherServerValidator(Neo4jConfig(password="secret"))
        assert "notifications_min_severity" not in mock_gdb.driver.call_args.kwargs


def test_make_validator_aql_server_constructs_with_arangodb() -> None:
    with patch("sql2graph.validators.aql.server.ArangoClient") as mock_client:
        v = make_validator(
            "aql",
            "server",
            server_config=ArangoDBConfig(password="secret"),
        )
        from sql2graph.validators.aql.server import AqlServerValidator

        assert isinstance(v, AqlServerValidator)
        mock_client.assert_called_once()


def test_make_validator_gremlin_server_requires_gremlin_config() -> None:
    with pytest.raises(ValueError, match="requires a server_config"):
        make_validator("gremlin", "server")


def test_make_validator_gremlin_server_rejects_neo4j_config() -> None:
    with pytest.raises(TypeError, match="GremlinConfig"):
        make_validator("gremlin", "server", server_config=Neo4jConfig(password="p"))


def test_make_validator_cypher_server_rejects_gremlin_config() -> None:
    with pytest.raises(TypeError, match="Neo4jConfig"):
        make_validator("cypher", "server", server_config=GremlinConfig())


def test_make_validator_gremlin_server_constructs_with_gremlin_config() -> None:
    with patch("sql2graph.validators.gremlin.server.Client") as mock_client:
        v = make_validator(
            "gremlin",
            "server",
            server_config=GremlinConfig(),
        )
        from sql2graph.validators.gremlin.server import GremlinServerValidator

        assert isinstance(v, GremlinServerValidator)
        mock_client.assert_called_once()


def test_make_validator_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="Unknown validation mode"):
        make_validator("cypher", "telepathy")


def test_make_validator_managed_dispatches_without_docker() -> None:
    """managed mode returns a ManagedServerValidator without touching Docker."""
    from sql2graph.validators.provision import ManagedServerValidator

    v = make_validator("cypher", "managed")
    assert isinstance(v, ManagedServerValidator)
    # No container is started until the first validate(); closing an unstarted
    # validator must be a no-op and idempotent.
    v.close()
    v.close()


def test_make_validator_managed_ignores_server_config() -> None:
    """managed mode ignores server_config rather than type-checking it."""
    from sql2graph.validators.provision import ManagedServerValidator

    v = make_validator("cypher", "managed", server_config=GremlinConfig())
    assert isinstance(v, ManagedServerValidator)
    v.close()


def test_make_validator_managed_rejects_unknown_target() -> None:
    with pytest.raises(ValueError, match="Unknown target language"):
        make_validator("sparql", "managed")


def test_make_async_validator_managed_dispatches() -> None:
    from sql2graph import make_async_validator
    from sql2graph.validators.provision import AsyncManagedServerValidator

    assert isinstance(make_async_validator("gremlin", "managed"), AsyncManagedServerValidator)


def test_make_async_validator_dispatches_correctly() -> None:
    from sql2graph import make_async_validator
    from sql2graph.validators import (
        AsyncCypherSyntaxValidator,
        AsyncNoopValidator,
    )

    assert isinstance(make_async_validator("cypher", "none"), AsyncNoopValidator)
    assert isinstance(make_async_validator("cypher", "syntax"), AsyncCypherSyntaxValidator)
