"""Static tests for the rows2graph framework.

"Static" here means: no network, no real LLM calls, no real graph database.
Every external dependency (LLM, Neo4j driver, ArangoDB client) is mocked or
swapped for an in-process double. The goal is to exercise the framework's
type discipline, factory dispatch, and loop logic without provisioning
infrastructure.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from rows2graph import (
    AnthropicConfig,
    AqlSyntaxValidator,
    AqlTarget,
    ArangoDBConfig,
    CypherSyntaxValidator,
    CypherTarget,
    EdgeMapping,
    GremlinConfig,
    GremlinSyntaxValidator,
    GremlinTarget,
    Neo4jConfig,
    NodeMapping,
    NoopValidator,
    OllamaConfig,
    SchemaMapping,
    SQLTranslator,
    TranslationResult,
    load_model_config,
    load_server_config,
    make_llm,
    make_target,
    make_validator,
    valid_modes_for_target,
)
from rows2graph._env import interpolate_env
from rows2graph.llm.usage import ChatReply, TokenUsage
from rows2graph.preflight import PreflightAction, find_unmapped_columns, find_unmapped_tables
from rows2graph.prompts import (
    build_escalation_prompt,
    build_fix_prompt,
    build_generate_prompt,
    build_system_prompt,
    error_signature,
    normalize_query,
)
from rows2graph.sql_features import ALL_FEATURES, SqlFeature, analyze_sql, detect_features
from rows2graph.targets import aql as aql_target
from rows2graph.targets import cypher as cypher_target
from rows2graph.targets import gremlin as gremlin_target
from rows2graph.targets._schema import EX_JOIN_FILTER_SQL, EX_POINT_LOOKUP_SQL

# Target classes and modules for parametrized cross-target parity tests.
_ALL_TARGET_CLASSES = [CypherTarget, AqlTarget, GremlinTarget]
_ALL_TARGET_MODULES = [cypher_target, aql_target, gremlin_target]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _schema() -> SchemaMapping:
    return SchemaMapping(
        nodes=[
            NodeMapping(
                label="Person",
                source_table="persons",
                primary_key="id",
                properties={"name": "full_name"},
            ),
            NodeMapping(
                label="Forum",
                source_table="forums",
                primary_key="id",
                properties={"title": "title"},
            ),
        ],
        edges=[
            EdgeMapping(
                type="KNOWS",
                source_node="Person",
                target_node="Person",
                source_table="knows",
                source_foreign_key="from_id",
                target_primary_key="id",
            )
        ],
    )


# ---------------------------------------------------------------------------
# SchemaMapping
# ---------------------------------------------------------------------------


def test_schema_rejects_unknown_edge_source_node() -> None:
    with pytest.raises(ValidationError, match=r"undefined source_node 'Alien'"):
        SchemaMapping(
            nodes=[NodeMapping(label="Person", source_table="t", primary_key="id", properties={"a": "a"})],
            edges=[
                EdgeMapping(
                    type="X",
                    source_node="Alien",
                    target_node="Person",
                    source_table="t",
                    source_foreign_key="fk",
                    target_primary_key="id",
                )
            ],
        )


def test_schema_rejects_unknown_edge_target_node() -> None:
    with pytest.raises(ValidationError, match=r"undefined target_node 'Alien'"):
        SchemaMapping(
            nodes=[NodeMapping(label="Person", source_table="t", primary_key="id", properties={"a": "a"})],
            edges=[
                EdgeMapping(
                    type="X",
                    source_node="Person",
                    target_node="Alien",
                    source_table="t",
                    source_foreign_key="fk",
                    target_primary_key="id",
                )
            ],
        )


def test_schema_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        # source_tabel is a typo for source_table; strict mode catches it
        NodeMapping(
            label="X",
            source_tabel="t",  # type: ignore[call-arg]
            primary_key="id",
            properties={"a": "a"},
        )


def test_schema_mapping_from_yaml_round_trip(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  - label: "Person"
    source_table: "person"
    primary_key: "id"
    properties:
      name: "full_name"
edges: []
"""
    p = tmp_path / "m.yaml"
    p.write_text(yaml_text)
    mapping = SchemaMapping.from_yaml(p)
    assert mapping.nodes[0].label == "Person"
    assert mapping.edges == []


def test_schema_rejects_duplicate_node_labels() -> None:
    with pytest.raises(ValidationError, match="Duplicate node label"):
        SchemaMapping(
            nodes=[
                NodeMapping(label="Person", source_table="t1", primary_key="id", properties={"a": "a"}),
                NodeMapping(label="Person", source_table="t2", primary_key="id", properties={"b": "b"}),
            ],
            edges=[],
        )


def test_schema_rejects_blank_label() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(label="   ", source_table="t", primary_key="id", properties={"a": "a"})


def test_schema_rejects_blank_primary_key() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(label="X", source_table="t", primary_key="", properties={"a": "a"})


def test_schema_rejects_blank_property_value() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(label="X", source_table="t", primary_key="id", properties={"a": ""})


def test_schema_rejects_blank_edge_field() -> None:
    with pytest.raises(ValidationError):
        EdgeMapping(
            type="X",
            source_node="A",
            target_node="B",
            source_table="t",
            source_foreign_key="",
            target_primary_key="id",
        )


def test_schema_rejects_fully_duplicate_edges() -> None:
    with pytest.raises(ValidationError, match="Duplicate edge"):
        SchemaMapping(
            nodes=[NodeMapping(label="Person", source_table="t", primary_key="id", properties={"a": "a"})],
            edges=[
                EdgeMapping(
                    type="KNOWS",
                    source_node="Person",
                    target_node="Person",
                    source_table="knows",
                    source_foreign_key="friend_id",
                    target_primary_key="id",
                ),
                EdgeMapping(
                    type="KNOWS",
                    source_node="Person",
                    target_node="Person",
                    source_table="knows",
                    source_foreign_key="friend_id",
                    target_primary_key="id",
                ),
            ],
        )


def test_schema_allows_same_type_different_target() -> None:
    # Two LIKES edges from Person to different targets must NOT be rejected
    # (legitimate multi-junction pattern, as in ldbc.yaml).
    mapping = SchemaMapping(
        nodes=[
            NodeMapping(label="Person", source_table="person", primary_key="id", properties={"id": "id"}),
            NodeMapping(label="Post", source_table="post", primary_key="id", properties={"id": "id"}),
            NodeMapping(label="Comment", source_table="comment", primary_key="id", properties={"id": "id"}),
        ],
        edges=[
            EdgeMapping(
                type="LIKES",
                source_node="Person",
                target_node="Post",
                source_table="likes_post",
                source_foreign_key="post_id",
                target_primary_key="id",
            ),
            EdgeMapping(
                type="LIKES",
                source_node="Person",
                target_node="Comment",
                source_table="likes_comment",
                source_foreign_key="comment_id",
                target_primary_key="id",
            ),
        ],
    )
    assert len(mapping.edges) == 2


def test_schema_mapping_accessors() -> None:
    mapping = SchemaMapping(
        nodes=[
            NodeMapping(
                label="Person", source_table="person", primary_key="id", properties={"id": "id", "name": "full_name"}
            ),
            NodeMapping(label="Post", source_table="post", primary_key="id", properties={"id": "id"}),
        ],
        edges=[
            EdgeMapping(
                type="HAS_CREATOR",
                source_node="Post",
                target_node="Person",
                source_table="post",
                source_foreign_key="creator_id",
                target_primary_key="id",
                properties={"weight": "w"},
            ),
        ],
    )
    assert mapping.node_labels() == {"Person", "Post"}
    assert mapping.edge_types() == {"HAS_CREATOR"}
    assert mapping.properties_for_label("Person") == {"id", "name"}
    assert mapping.properties_for_label("Unknown") == set()
    assert mapping.properties_for_edge("HAS_CREATOR") == {"weight"}


def test_shipped_mappings_still_load() -> None:
    # Regression guard: the stricter validators must not reject the bundled
    # example mappings.
    mappings_dir = Path(__file__).resolve().parent.parent / "examples" / "mappings"
    for name in ("tpch.yaml", "ldbc.yaml"):
        mapping = SchemaMapping.from_yaml(mappings_dir / name)
        assert mapping.nodes
        assert mapping.edges


# ---------------------------------------------------------------------------
# Env-var interpolation
# ---------------------------------------------------------------------------


def test_interpolate_env_substitutes_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", "s3cret")
    assert interpolate_env("hello ${MY_SECRET}!") == "hello s3cret!"


def test_interpolate_env_walks_nested_structures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", "s3cret")
    data = {"a": ["${MY_SECRET}", "${MY_SECRET}"], "b": {"c": "${MY_SECRET}"}}
    assert interpolate_env(data) == {"a": ["s3cret", "s3cret"], "b": {"c": "s3cret"}}


def test_interpolate_env_raises_on_missing_variable() -> None:
    if "DEFINITELY_NOT_SET_VAR" in os.environ:
        del os.environ["DEFINITELY_NOT_SET_VAR"]
    with pytest.raises(KeyError, match=r"DEFINITELY_NOT_SET_VAR"):
        interpolate_env("password=${DEFINITELY_NOT_SET_VAR}")


# ---------------------------------------------------------------------------
# Model-config discriminator dispatch
# ---------------------------------------------------------------------------


def test_load_model_config_dispatches_to_ollama(tmp_path: Path) -> None:
    p = tmp_path / "ollama.yaml"
    p.write_text('provider: "ollama"\nmodel: "llama3.2"\nhost: "http://x:1"\n')
    config = load_model_config(p)
    assert isinstance(config, OllamaConfig)
    assert config.model == "llama3.2"


def test_load_model_config_dispatches_to_anthropic(tmp_path: Path) -> None:
    p = tmp_path / "anthropic.yaml"
    p.write_text('provider: "anthropic"\napi_key: "sk-ant-abc"\nmodel: "claude-x"\n')
    config = load_model_config(p)
    assert isinstance(config, AnthropicConfig)
    assert config.api_key == "sk-ant-abc"
    assert config.model == "claude-x"


def test_load_model_config_rejects_unknown_provider(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text('provider: "openai"\nmodel: "gpt-4"\n')
    with pytest.raises(ValidationError):
        load_model_config(p)


def test_load_model_config_anthropic_api_key_defaults_to_none(tmp_path: Path) -> None:
    # api_key is optional; when omitted the SDK falls back to $ANTHROPIC_API_KEY.
    p = tmp_path / "no_key.yaml"
    p.write_text('provider: "anthropic"\nmodel: "claude-x"\n')
    config = load_model_config(p)
    assert isinstance(config, AnthropicConfig)
    assert config.api_key is None


# ---------------------------------------------------------------------------
# Server-config discriminator dispatch
# ---------------------------------------------------------------------------


def test_load_server_config_dispatches_to_neo4j(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PW", "open-sesame")
    p = tmp_path / "neo4j.yaml"
    p.write_text('type: "neo4j"\npassword: "${TEST_PW}"\n')
    config = load_server_config(p)
    assert isinstance(config, Neo4jConfig)
    assert config.password == "open-sesame"


def test_load_server_config_dispatches_to_arangodb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PW", "open-sesame")
    p = tmp_path / "arangodb.yaml"
    p.write_text('type: "arangodb"\npassword: "${TEST_PW}"\n')
    config = load_server_config(p)
    assert isinstance(config, ArangoDBConfig)
    assert config.password == "open-sesame"


def test_load_server_config_dispatches_to_gremlin(tmp_path: Path) -> None:
    p = tmp_path / "gremlin.yaml"
    p.write_text('type: "gremlin"\nurl: "ws://localhost:8182/gremlin"\ntraversal_source: "g"\n')
    config = load_server_config(p)
    assert isinstance(config, GremlinConfig)
    assert config.url == "ws://localhost:8182/gremlin"
    assert config.traversal_source == "g"


def test_load_server_config_rejects_unknown_type(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text('type: "redis"\npassword: "x"\n')
    with pytest.raises(ValidationError):
        load_server_config(p)


def test_load_server_config_propagates_missing_env_var(tmp_path: Path) -> None:
    if "REALLY_NOT_SET_PASSWORD" in os.environ:
        del os.environ["REALLY_NOT_SET_PASSWORD"]
    p = tmp_path / "neo4j.yaml"
    p.write_text('type: "neo4j"\npassword: "${REALLY_NOT_SET_PASSWORD}"\n')
    with pytest.raises(KeyError, match="REALLY_NOT_SET_PASSWORD"):
        load_server_config(p)


# ---------------------------------------------------------------------------
# make_target factory
# ---------------------------------------------------------------------------


def test_make_target_cypher() -> None:
    t = make_target("cypher")
    assert isinstance(t, CypherTarget)
    assert t.name == "cypher"


def test_make_target_aql() -> None:
    t = make_target("aql")
    assert isinstance(t, AqlTarget)
    assert t.name == "aql"
    section = t.system_prompt_section(frozenset())
    # AQL uses bare edge-collection traversals plus an anti-pattern block,
    # not the named-graph form.
    assert "OUTBOUND" in section
    assert "These are NOT valid AQL" in section


def test_make_target_gremlin() -> None:
    t = make_target("gremlin")
    assert isinstance(t, GremlinTarget)
    assert t.name == "gremlin"


def test_make_target_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown target language"):
        make_target("sparql")


# ---------------------------------------------------------------------------
# make_validator factory
# ---------------------------------------------------------------------------


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
    with patch("rows2graph.validators.cypher.server.GraphDatabase") as mock_gdb:
        mock_gdb.driver = MagicMock()
        v = make_validator(
            "cypher",
            "server",
            server_config=Neo4jConfig(password="secret"),
        )
        from rows2graph.validators.cypher.server import CypherServerValidator

        assert isinstance(v, CypherServerValidator)
        mock_gdb.driver.assert_called_once()


def test_neo4j_config_notifications_min_severity_defaults_to_none() -> None:
    assert Neo4jConfig(password="x").notifications_min_severity is None


def test_neo4j_config_rejects_invalid_notifications_min_severity() -> None:
    with pytest.raises(ValidationError):
        Neo4jConfig(password="x", notifications_min_severity="LOUD")  # type: ignore[arg-type]


def test_cypher_server_validator_forwards_notifications_min_severity() -> None:
    from rows2graph.validators.cypher.server import CypherServerValidator

    with patch("rows2graph.validators.cypher.server.GraphDatabase") as mock_gdb:
        CypherServerValidator(Neo4jConfig(password="secret", notifications_min_severity="OFF"))
        assert mock_gdb.driver.call_args.kwargs.get("notifications_min_severity") == "OFF"


def test_cypher_server_validator_omits_notifications_min_severity_when_unset() -> None:
    from rows2graph.validators.cypher.server import CypherServerValidator

    with patch("rows2graph.validators.cypher.server.GraphDatabase") as mock_gdb:
        CypherServerValidator(Neo4jConfig(password="secret"))
        assert "notifications_min_severity" not in mock_gdb.driver.call_args.kwargs


def test_make_validator_aql_server_constructs_with_arangodb() -> None:
    with patch("rows2graph.validators.aql.server.ArangoClient") as mock_client:
        v = make_validator(
            "aql",
            "server",
            server_config=ArangoDBConfig(password="secret"),
        )
        from rows2graph.validators.aql.server import AqlServerValidator

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
    with patch("rows2graph.validators.gremlin.server.Client") as mock_client:
        v = make_validator(
            "gremlin",
            "server",
            server_config=GremlinConfig(),
        )
        from rows2graph.validators.gremlin.server import GremlinServerValidator

        assert isinstance(v, GremlinServerValidator)
        mock_client.assert_called_once()


def test_make_validator_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="Unknown validation mode"):
        make_validator("cypher", "telepathy")


def test_make_validator_managed_dispatches_without_docker() -> None:
    """managed mode returns a ManagedServerValidator without touching Docker."""
    from rows2graph.validators.provision import ManagedServerValidator

    v = make_validator("cypher", "managed")
    assert isinstance(v, ManagedServerValidator)
    # No container is started until the first validate(); closing an unstarted
    # validator must be a no-op and idempotent.
    v.close()
    v.close()


def test_make_validator_managed_ignores_server_config() -> None:
    """managed mode ignores server_config rather than type-checking it."""
    from rows2graph.validators.provision import ManagedServerValidator

    v = make_validator("cypher", "managed", server_config=GremlinConfig())
    assert isinstance(v, ManagedServerValidator)
    v.close()


def test_make_validator_managed_rejects_unknown_target() -> None:
    with pytest.raises(ValueError, match="Unknown target language"):
        make_validator("sparql", "managed")


def test_make_async_validator_managed_dispatches() -> None:
    from rows2graph import make_async_validator
    from rows2graph.validators.provision import AsyncManagedServerValidator

    assert isinstance(make_async_validator("gremlin", "managed"), AsyncManagedServerValidator)


# ---------------------------------------------------------------------------
# make_llm factory (constructors are mocked; we just verify dispatch)
# ---------------------------------------------------------------------------


def test_make_llm_ollama_dispatch() -> None:
    with patch("rows2graph.llm.ollama.Client") as mock_client:
        llm = make_llm(OllamaConfig(model="m", host="http://x:1"))
        from rows2graph.llm.ollama import OllamaLLMClient

        assert isinstance(llm, OllamaLLMClient)
        mock_client.assert_called_once_with(host="http://x:1")


def test_make_llm_ollama_defaults_host_to_none() -> None:
    """Unset host -> None, so the ollama SDK reads $OLLAMA_HOST (else localhost)."""
    assert OllamaConfig(model="m").host is None
    with patch("rows2graph.llm.ollama.Client") as mock_client:
        make_llm(OllamaConfig(model="m"))
        mock_client.assert_called_once_with(host=None)


def test_ollama_chat_retries_on_request_error_then_succeeds() -> None:
    """Connection-layer failures (RequestError) are retried with backoff."""
    from ollama import RequestError

    from rows2graph.llm.ollama import OllamaLLMClient

    with (
        patch("rows2graph.llm.ollama.Client") as mock_client_cls,
        patch("rows2graph.llm.ollama.time.sleep") as mock_sleep,
    ):
        mock_response = MagicMock()
        mock_response.message.content = "ok"
        mock_response.prompt_eval_count = 7
        mock_response.eval_count = 3
        mock_client_cls.return_value.chat.side_effect = [
            RequestError("connection refused"),
            RequestError("connection refused"),
            mock_response,
        ]
        client = OllamaLLMClient(OllamaConfig(model="m", host="http://x:1", max_retries=3))
        reply = client.chat([{"role": "user", "content": "hi"}])
        assert reply.text == "ok"
        assert reply.usage.input_tokens == 7
        assert reply.usage.output_tokens == 3
        assert mock_client_cls.return_value.chat.call_count == 3
        # First failure → sleep 1s; second failure → sleep 2s.
        assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 2.0]


def test_ollama_chat_retries_on_5xx_response_error() -> None:
    from ollama import ResponseError

    from rows2graph.llm.ollama import OllamaLLMClient

    with (
        patch("rows2graph.llm.ollama.Client") as mock_client_cls,
        patch("rows2graph.llm.ollama.time.sleep"),
    ):
        mock_response = MagicMock()
        mock_response.message.content = "ok"
        mock_response.prompt_eval_count = 7
        mock_response.eval_count = 3
        mock_client_cls.return_value.chat.side_effect = [
            ResponseError("server overloaded", 503),
            mock_response,
        ]
        client = OllamaLLMClient(OllamaConfig(model="m", host="http://x:1", max_retries=3))
        assert client.chat([{"role": "user", "content": "hi"}]).text == "ok"
        assert mock_client_cls.return_value.chat.call_count == 2


def test_ollama_chat_does_not_retry_on_4xx_response_error() -> None:
    """4xx errors are client-side bugs; retrying just wastes time."""
    from ollama import ResponseError

    from rows2graph.llm.ollama import OllamaLLMClient

    with (
        patch("rows2graph.llm.ollama.Client") as mock_client_cls,
        patch("rows2graph.llm.ollama.time.sleep") as mock_sleep,
    ):
        mock_client_cls.return_value.chat.side_effect = ResponseError("unknown model", 404)
        client = OllamaLLMClient(OllamaConfig(model="m", host="http://x:1", max_retries=3))
        with pytest.raises(ResponseError):
            client.chat([{"role": "user", "content": "hi"}])
        assert mock_client_cls.return_value.chat.call_count == 1
        mock_sleep.assert_not_called()


def test_ollama_chat_exhausts_retries_and_reraises() -> None:
    from ollama import RequestError

    from rows2graph.llm.ollama import OllamaLLMClient

    with (
        patch("rows2graph.llm.ollama.Client") as mock_client_cls,
        patch("rows2graph.llm.ollama.time.sleep"),
    ):
        mock_client_cls.return_value.chat.side_effect = RequestError("nope")
        client = OllamaLLMClient(OllamaConfig(model="m", host="http://x:1", max_retries=2))
        with pytest.raises(RequestError):
            client.chat([{"role": "user", "content": "hi"}])
        # max_retries=2 → 1 initial + 2 retries = 3 total attempts.
        assert mock_client_cls.return_value.chat.call_count == 3


def test_ollama_config_rejects_negative_max_retries() -> None:
    with pytest.raises(ValidationError):
        OllamaConfig(max_retries=-1)


def test_make_llm_anthropic_dispatch() -> None:
    with patch("rows2graph.llm.anthropic.Anthropic") as mock_anthropic:
        llm = make_llm(AnthropicConfig(api_key="sk-ant-test", model="claude-x"))
        from rows2graph.llm.anthropic import AnthropicLLMClient

        assert isinstance(llm, AnthropicLLMClient)
        mock_anthropic.assert_called_once_with(api_key="sk-ant-test", max_retries=3)


def test_make_llm_anthropic_forwards_custom_max_retries() -> None:
    with patch("rows2graph.llm.anthropic.Anthropic") as mock_anthropic:
        make_llm(AnthropicConfig(api_key="sk-ant-test", model="claude-x", max_retries=7))
        mock_anthropic.assert_called_once_with(api_key="sk-ant-test", max_retries=7)


def test_anthropic_config_rejects_negative_max_retries() -> None:
    with pytest.raises(ValidationError):
        AnthropicConfig(max_retries=-1)


def test_anthropic_chat_marks_system_prompt_cacheable() -> None:
    """System block must carry cache_control=ephemeral so multi-iteration
    translations reuse the schema+rules prompt instead of re-sending it.
    """
    from rows2graph.llm.anthropic import AnthropicLLMClient

    with patch("rows2graph.llm.anthropic.Anthropic") as mock_anthropic:
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="ok")]
        mock_response.usage = None
        mock_anthropic.return_value.messages.create.return_value = mock_response

        client = AnthropicLLMClient(AnthropicConfig(api_key="sk-ant-test", model="claude-x"))
        client.chat(
            [
                {"role": "system", "content": "you are a translator"},
                {"role": "user", "content": "translate this"},
            ]
        )

        call_kwargs = mock_anthropic.return_value.messages.create.call_args.kwargs
        assert call_kwargs["system"] == [
            {
                "type": "text",
                "text": "you are a translator",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert call_kwargs["messages"] == [{"role": "user", "content": "translate this"}]


def test_anthropic_chat_omits_system_when_no_system_messages() -> None:
    """When the flat message list has no system entries, the `system`
    kwarg is omitted entirely: adding an empty cacheable block would be
    both wasteful and (for an empty string) likely rejected by the API.
    """
    from rows2graph.llm.anthropic import AnthropicLLMClient

    with patch("rows2graph.llm.anthropic.Anthropic") as mock_anthropic:
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="ok")]
        mock_response.usage = None
        mock_anthropic.return_value.messages.create.return_value = mock_response

        client = AnthropicLLMClient(AnthropicConfig(api_key="sk-ant-test", model="claude-x"))
        client.chat([{"role": "user", "content": "hello"}])

        call_kwargs = mock_anthropic.return_value.messages.create.call_args.kwargs
        assert "system" not in call_kwargs


# ---------------------------------------------------------------------------
# Query extractors
# ---------------------------------------------------------------------------


def test_extract_cypher_from_fence() -> None:
    text = "Here you go:\n```cypher\nMATCH (n) RETURN n\n```\nDone."
    assert CypherTarget().extract_query(text) == "MATCH (n) RETURN n"


def test_extract_cypher_from_keyword() -> None:
    text = "Sure!\nMATCH (n:Person) RETURN n.name"
    assert CypherTarget().extract_query(text) == "MATCH (n:Person) RETURN n.name"


def test_extract_cypher_fallback_returns_stripped() -> None:
    assert CypherTarget().extract_query("  not a query  ") == "not a query"


def test_extract_aql_from_fence() -> None:
    text = "```aql\nFOR p IN persons RETURN p\n```"
    assert AqlTarget().extract_query(text) == "FOR p IN persons RETURN p"


def test_extract_aql_from_keyword() -> None:
    text = "OK:\nFOR p IN persons FILTER p.age > 18 RETURN p"
    assert AqlTarget().extract_query(text) == "FOR p IN persons FILTER p.age > 18 RETURN p"


def test_extract_aql_fallback_returns_stripped() -> None:
    assert AqlTarget().extract_query("  nothing here  ") == "nothing here"


def test_extract_gremlin_from_gremlin_fence() -> None:
    text = "Here you go:\n```gremlin\ng.V().hasLabel('Person').valueMap()\n```\nDone."
    assert GremlinTarget().extract_query(text) == "g.V().hasLabel('Person').valueMap()"


def test_extract_gremlin_from_groovy_fence() -> None:
    text = "```groovy\ng.V().has('Person', 'id', 933).valueMap()\n```"
    assert GremlinTarget().extract_query(text) == "g.V().has('Person', 'id', 933).valueMap()"


def test_extract_gremlin_from_keyword() -> None:
    text = "Sure!\ng.V().hasLabel('Person').count()"
    assert GremlinTarget().extract_query(text) == "g.V().hasLabel('Person').count()"


def test_extract_gremlin_fallback_returns_stripped() -> None:
    assert GremlinTarget().extract_query("  not a query  ") == "not a query"


def test_extract_aql_from_mislabeled_arangodb_fence() -> None:
    # Regression: the model fences AQL as ```arangodb (not ```aql). The body
    # must still be extracted cleanly, without the closing ``` leaking in.
    text = "```arangodb\nFOR f IN Forum\n  RETURN f\n```"
    result = AqlTarget().extract_query(text)
    assert result == "FOR f IN Forum\n  RETURN f"
    assert "```" not in result


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
@pytest.mark.parametrize("tag", ["sql", "text", "json"])
def test_extract_handles_arbitrary_fence_tag(target_cls: type, tag: str) -> None:
    # Any info-string after ``` is treated as a fence for every target, so a
    # mislabeled fence never falls through and leaks its closing delimiter.
    text = f"```{tag}\nQUERY BODY HERE\n```"
    result = target_cls().extract_query(text)
    assert result == "QUERY BODY HERE"
    assert "```" not in result


def test_extract_strips_trailing_fence_on_keyword_fallback() -> None:
    # No opening fence, but a stray closing ``` trails the keyword query (the
    # shape that previously reached the validator). It must be stripped.
    text = "FOR p IN persons RETURN p\n```"
    result = AqlTarget().extract_query(text)
    assert result == "FOR p IN persons RETURN p"
    assert "```" not in result


# ---------------------------------------------------------------------------
# Syntax validators
# ---------------------------------------------------------------------------


def test_cypher_syntax_passes_valid_query() -> None:
    assert CypherSyntaxValidator().validate("MATCH (n) RETURN n") == []


def test_cypher_syntax_passes_single_quoted_string_with_bracket() -> None:
    # A single-quoted string containing a paren is valid Cypher. The old
    # bracket-counting heuristic wrongly rejected it; the grammar accepts it.
    assert CypherSyntaxValidator().validate("MATCH (n {name: 'a)b'}) RETURN n") == []


def test_cypher_syntax_flags_bad_start() -> None:
    assert CypherSyntaxValidator().validate("WHERE x = 1")


def test_cypher_syntax_flags_malformed_pattern() -> None:
    assert CypherSyntaxValidator().validate("MATCH (n)-[r]-( RETURN n")


def test_cypher_syntax_flags_missing_projection() -> None:
    assert CypherSyntaxValidator().validate("MATCH (n) RETURN")


def test_cypher_syntax_flags_empty_query() -> None:
    assert CypherSyntaxValidator().validate("   ") == ["Query is empty"]


def test_gremlin_syntax_passes_valid_query() -> None:
    assert GremlinSyntaxValidator().validate("g.V().hasLabel('Person').valueMap()") == []


def test_gremlin_syntax_passes_with_anonymous_traversal() -> None:
    query = "g.V().hasLabel('Person').where(__.out('KNOWS')).valueMap()"
    assert GremlinSyntaxValidator().validate(query) == []


def test_gremlin_syntax_flags_bad_start() -> None:
    assert GremlinSyntaxValidator().validate("MATCH (n) RETURN n")


def test_gremlin_syntax_flags_unbalanced_parens() -> None:
    assert GremlinSyntaxValidator().validate("g.V().hasLabel('Person'.valueMap()")


def test_gremlin_syntax_flags_unbalanced_brackets() -> None:
    assert GremlinSyntaxValidator().validate("g.V().has('age', P.within([1, 2).count()")


def test_gremlin_syntax_flags_trailing_dot() -> None:
    assert GremlinSyntaxValidator().validate("g.V().hasLabel('Person').")


def test_gremlin_syntax_flags_empty_query() -> None:
    assert GremlinSyntaxValidator().validate("   ") == ["Query is empty"]


def test_gremlin_syntax_flags_unbalanced_quotes() -> None:
    assert GremlinSyntaxValidator().validate("g.V().has('label, 'x').count()")


@pytest.mark.parametrize(
    "query",
    [
        "RETURN 1",
        "RETURN DISTINCT x",
        "FOR u IN users FILTER u.age >= 20 AND u.age < 30 SORT u.name DESC LIMIT 10 RETURN u.name",
        "FOR u IN users LET n = u.name RETURN { name: n, id: u._id }",
        "FOR u IN users COLLECT city = u.city INTO g RETURN { city, count: LENGTH(g) }",
        "FOR u IN users COLLECT WITH COUNT INTO total RETURN total",
        "FOR u IN users COLLECT AGGREGATE ma = MAX(u.age) RETURN ma",
        "INSERT { name: 'x' } INTO users",
        "UPDATE 'key' WITH { a: 1 } IN users OPTIONS { waitForSync: true }",
        "UPSERT { a: 1 } INSERT { a: 1 } UPDATE { b: 2 } IN users",
        "FOR v, e, p IN 1..3 OUTBOUND 'start/1' GRAPH 'g' RETURN v",
        "FOR v, e IN OUTBOUND SHORTEST_PATH 'a/1' TO 'b/2' GRAPH 'g' RETURN v",
        "RETURN u.items[* FILTER CURRENT.age > 2]",
        "RETURN x NOT IN [1, 2, 3]",
        "RETURN a ? b : c",
        "FOR d IN @@coll FILTER d.k == @value RETURN d",
        "RETURN LENGTH(FOR x IN c RETURN x)",
        "WITH users FOR u IN users RETURN u",
    ],
)
def test_aql_syntax_passes_valid_query(query: str) -> None:
    assert AqlSyntaxValidator().validate(query) == []


@pytest.mark.parametrize(
    "query",
    [
        "FOR u IN RETURN u",
        "RETURN",
        "RETURN (1 + )",
        "RETURN [1, 2",
        "MATCH (n) RETURN n",
        "SELECT * FROM users",
        "FOR u IN users RETURN u EXTRA",
    ],
)
def test_aql_syntax_flags_invalid_query(query: str) -> None:
    assert AqlSyntaxValidator().validate(query)


def test_aql_syntax_flags_empty_query() -> None:
    assert AqlSyntaxValidator().validate("   ") == ["Query is empty"]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_build_system_prompt_cypher() -> None:
    prompt = build_system_prompt(_schema(), CypherTarget(), frozenset())
    assert "cypher" in prompt
    assert "MATCH" in prompt
    assert "Person" in prompt  # schema is embedded


def test_build_system_prompt_aql_uses_edge_collection_form() -> None:
    prompt = build_system_prompt(_schema(), AqlTarget(), frozenset())
    assert "aql" in prompt
    assert "FOR" in prompt
    assert "FILTER" in prompt
    assert "Person" in prompt  # schema is embedded
    # AQL uses bare edge-collection traversals, and the prompt warns against
    # the Cypher edge syntax small models tend to emit.
    assert "OUTBOUND" in prompt
    assert "These are NOT valid AQL" in prompt


def test_build_system_prompt_gremlin() -> None:
    prompt = build_system_prompt(_schema(), GremlinTarget(), frozenset())
    assert "gremlin" in prompt
    assert "g.V()" in prompt
    assert "Person" in prompt  # schema is embedded


def test_gremlin_base_rules_teach_projection_and_forbidden_patterns() -> None:
    # A plain SELECT detects no SqlFeature, so the always-on base block must
    # itself carry the read/projection guidance and the anti-hallucination
    # list, the two failure modes seen in the captured error logs.
    prompt = build_system_prompt(_schema(), GremlinTarget(), frozenset())
    # Reading + projecting columns into one traversal.
    assert ".project(" in prompt
    assert ".values(" in prompt
    # id COLUMN vs the graph's internal element id.
    assert "internal element id" in prompt
    # Anonymous steps are method calls (`__.id()`, not `__.id`).
    assert "__.id()" in prompt
    # Explicit forbidden list that names the hallucinated read steps.
    assert "NOT valid Gremlin" in prompt
    assert "WRITES a property" in prompt


def test_gremlin_base_rules_demand_single_traversal_no_prose() -> None:
    # The model tends to emit the right query, then keep talking with prose and
    # alternative versions; the base block must forbid that explicitly.
    prompt = build_system_prompt(_schema(), GremlinTarget(), frozenset())
    assert "Output EXACTLY ONE traversal" in prompt
    assert "no alternative versions" in prompt


def test_build_generate_prompt_includes_sql() -> None:
    assert "SELECT 1" in build_generate_prompt("SELECT 1")


def test_build_fix_prompt_includes_errors() -> None:
    prompt = build_fix_prompt(
        sql_query="SELECT 1",
        generated_query="MATCH (n",
        errors=["Unbalanced parentheses"],
    )
    assert "SELECT 1" in prompt
    assert "MATCH (n" in prompt
    assert "Unbalanced parentheses" in prompt


def test_build_fix_prompt_default_keeps_do_not_restructure() -> None:
    prompt = build_fix_prompt(sql_query="SELECT 1", generated_query="MATCH (n", errors=["e"])
    assert "Do not change the query structure unnecessarily." in prompt


def test_build_fix_prompt_repair_hint_replaces_default() -> None:
    prompt = build_fix_prompt(
        sql_query="SELECT 1",
        generated_query="MATCH (n",
        errors=["e"],
        repair_hint="MOVE THE RETURN LAST.",
    )
    assert "MOVE THE RETURN LAST." in prompt
    # The hint *replaces* the default "don't restructure" instruction.
    assert "Do not change the query structure unnecessarily." not in prompt


def test_build_escalation_prompt_names_the_repetition_and_hint() -> None:
    prompt = build_escalation_prompt(
        sql_query="SELECT 1",
        generated_query="FOR f IN Forum RETURN f SORT f.x",
        errors=["unexpected SORT declaration"],
        repair_hint="MOVE THE RETURN LAST.",
    )
    assert "DIFFERENT" in prompt  # tells the model not to repeat itself
    assert "FOR f IN Forum RETURN f SORT f.x" in prompt
    assert "MOVE THE RETURN LAST." in prompt


def test_aql_repair_hint_fires_on_clause_ordering_error() -> None:
    errors = [
        "[HTTP 400][ERR 1501] syntax error, unexpected SORT declaration, "
        "expecting end of query string near 'SORT LENGTH(members) DESC' at position 4:1",
    ]
    hint = AqlTarget().repair_hint(errors)
    assert hint is not None
    assert "RETURN" in hint and "before" in hint.lower()


def test_aql_repair_hint_fires_on_offline_grammar_ordering_error() -> None:
    # The offline ANTLR validator phrases the clause-after-RETURN error as
    # "mismatched input 'SORT' expecting <EOF>", not the server's "unexpected
    # SORT". repair_hint must recognise both so the corrective still fires.
    errors = AqlSyntaxValidator().validate("FOR u IN users RETURN u.name SORT u.name DESC")
    assert errors and "mismatched input 'SORT'" in errors[0]
    hint = AqlTarget().repair_hint(errors)
    assert hint is not None
    assert "RETURN" in hint and "before" in hint.lower()


def test_aql_repair_hint_none_for_unrelated_error() -> None:
    assert AqlTarget().repair_hint(["Unbalanced parentheses"]) is None
    # A real offline parse error that is NOT a clause-ordering problem.
    assert AqlTarget().repair_hint(AqlSyntaxValidator().validate("RETURN (1 + )")) is None


def test_cypher_and_gremlin_repair_hint_always_none() -> None:
    err = ["unexpected SORT declaration, expecting end of query string"]
    assert CypherTarget().repair_hint(err) is None
    assert GremlinTarget().repair_hint(err) is None


def test_error_signature_is_position_independent() -> None:
    a = ["[ERR 1501] syntax error, unexpected SORT declaration near 'x' at position 4:3"]
    b = ["[ERR 1501] syntax error, unexpected SORT declaration near 'y' at position 4:1"]
    # Same ArangoDB error code + shape, different position/near-text → same signature.
    assert error_signature(a) == error_signature(b)
    assert error_signature(a) != error_signature(["[ERR 1577] something else"])


def test_normalize_query_collapses_whitespace() -> None:
    assert normalize_query("FOR f\n  RETURN f") == normalize_query("FOR f RETURN f")


# ---------------------------------------------------------------------------
# SQLTranslator end-to-end with fake LLM
# ---------------------------------------------------------------------------


class _FakeLLM:
    """In-process double for the LLMClient Protocol."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.temperatures: list[float | None] = []
        self.closed = False

    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> ChatReply:
        self.calls.append(list(messages))
        self.temperatures.append(temperature)
        # Fixed per-call usage (15 total tokens) lets loop tests assert accumulation.
        return ChatReply(text=self._responses.pop(0), usage=TokenUsage(input_tokens=10, output_tokens=5))

    def close(self) -> None:
        self.closed = True


def _spy_analyze_sql(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str | None]:
    """Record the ``dialect`` each ``analyze_sql`` call in *module* receives.

    Delegates to the real implementation and returns the accumulating list, so a
    test can assert the translator forwarded its constructor ``dialect`` into the
    single pre-flight parse without depending on any sqlglot-version dialect quirk.
    """
    seen: list[str | None] = []
    real = module.analyze_sql

    def spy(sql_query: str, *, dialect: str | None = None) -> Any:
        seen.append(dialect)
        return real(sql_query, dialect=dialect)

    monkeypatch.setattr(module, "analyze_sql", spy)
    return seen


def test_translator_forwards_dialect_to_analyze_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """The constructor ``dialect`` reaches the pre-flight ``analyze_sql`` call.

    Forwarding the sqlglot dialect is the whole point of the parameter: it lets a
    valid vendor-specific query parse (keeping the unmapped-table/column checks
    live) instead of false-failing under the neutral parser.
    """
    import rows2graph.translator as translator_mod

    seen = _spy_analyze_sql(monkeypatch, translator_mod)
    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        dialect="postgres",
    ) as translator:
        translator.translate("SELECT * FROM persons")

    assert seen == ["postgres"]


def test_translator_dialect_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting ``dialect`` keeps the pre-flight parse dialect-neutral (``None``),
    i.e. identical to the behaviour before the parameter existed."""
    import rows2graph.translator as translator_mod

    seen = _spy_analyze_sql(monkeypatch, translator_mod)
    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons")

    assert seen == [None]


def test_translator_returns_result_on_first_try_success() -> None:
    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    ) as translator:
        result = translator.translate("SELECT * FROM persons")

    assert isinstance(result, TranslationResult)
    assert result.validation_passed is True
    assert result.status == "success"
    assert result.iterations_used == 1
    assert result.generated_query == "MATCH (p:Person) RETURN p"
    assert len(fake.calls) == 1
    assert fake.closed is True
    # One LLM call → one TokenUsage (10 in + 5 out).
    assert result.token_usage.total_tokens == 15
    assert result.token_usage.input_tokens == 10
    assert result.token_usage.output_tokens == 5


def test_translator_runs_fix_loop_on_validation_failure() -> None:
    # First response: malformed. Second response: valid.
    fake = _FakeLLM(
        [
            "MATCH (p:Person",  # malformed: unbalanced parenthesis
            "MATCH (p:Person) RETURN p",
        ]
    )
    translator = SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    )
    try:
        result = translator.translate("SELECT * FROM persons")
    finally:
        translator.close()

    assert result.validation_passed is True
    assert result.iterations_used == 2
    assert len(fake.calls) == 2
    # Usage accumulates across both LLM calls: 2 × (10 in + 5 out).
    assert result.token_usage.total_tokens == 30
    assert result.token_usage.input_tokens == 20
    assert result.token_usage.output_tokens == 10


def test_translator_hits_max_iterations() -> None:
    fake = _FakeLLM(["MATCH (p:Person"] * 3)  # always invalid
    translator = SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    )
    try:
        result = translator.translate("SELECT * FROM persons")
    finally:
        translator.close()

    assert result.validation_passed is False
    assert result.status == "max_iterations_reached"
    assert result.iterations_used == 3
    assert result.generated_query == "MATCH (p:Person"


def test_translator_escalates_on_stall_then_recovers() -> None:
    """A repeated (stalled) candidate triggers one fresh-context, hot retry that recovers."""
    from rows2graph import StalledEvent, TranslationEvent

    # gen=bad, fix=identical bad (→ stall), escalation=good.
    fake = _FakeLLM(["MATCH (p:Person", "MATCH (p:Person", "MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=5,
    ) as translator:
        result = translator.translate("SELECT * FROM persons", on_event=events.append)

    assert result.status == "success"
    assert result.generated_query == "MATCH (p:Person) RETURN p"
    # Exactly one escalation was signalled.
    assert sum(isinstance(e, StalledEvent) for e in events) == 1
    # Three LLM calls: generate, normal fix, escalation.
    assert len(fake.calls) == 3
    # The escalation ran hotter than the (default) generate/fix calls...
    assert fake.temperatures == [None, None, 0.6]
    # ...and on a CLEAN context: system turn + the single escalation user turn.
    assert len(fake.calls[2]) == 2
    assert fake.calls[2][0]["role"] == "system"


def test_translator_aborts_early_when_stalled_instead_of_burning_iterations() -> None:
    """When even the escalation makes no progress, abort as 'stalled', not 10 identical tries."""
    from rows2graph import StalledEvent, TranslationEvent

    fake = _FakeLLM(["MATCH (p:Person"] * 4)  # always invalid; one response left unused
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=10,
    ) as translator:
        result = translator.translate("SELECT * FROM persons", on_event=events.append)

    assert result.status == "stalled"
    assert result.validation_passed is False
    # generate + normal fix + escalation = 3 calls, then it gives up (not 10).
    assert result.iterations_used == 3
    assert len(fake.calls) == 3
    assert sum(isinstance(e, StalledEvent) for e in events) == 1


def test_translator_context_manager_closes_components() -> None:
    fake = _FakeLLM(["MATCH (p) RETURN p"])
    validator = CypherSyntaxValidator()
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=validator,
    ) as translator:
        translator.translate("SELECT 1")
    assert fake.closed is True


def test_translator_returns_result_for_gremlin_target() -> None:
    fake = _FakeLLM(["```gremlin\ng.V().hasLabel('Person').valueMap()\n```"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=GremlinTarget(),
        validator=GremlinSyntaxValidator(),
        max_iterations=3,
    ) as translator:
        result = translator.translate("SELECT * FROM persons")

    assert isinstance(result, TranslationResult)
    assert result.validation_passed is True
    assert result.status == "success"
    assert result.target_language == "gremlin"
    assert result.generated_query == "g.V().hasLabel('Person').valueMap()"


# ---------------------------------------------------------------------------
# Typed iteration events (rows2graph.events)
# ---------------------------------------------------------------------------


def test_translator_emits_event_sequence_on_first_try_success() -> None:
    """One-shot success: Generated → Validated(passed=True) → Completed."""
    from rows2graph import CompletedEvent, GeneratedEvent, TranslationEvent, ValidatedEvent

    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons", on_event=events.append)

    assert len(events) == 3
    assert isinstance(events[0], GeneratedEvent)
    assert events[0].iteration == 1
    assert events[0].query == "MATCH (p:Person) RETURN p"
    assert isinstance(events[1], ValidatedEvent)
    assert events[1].iteration == 1
    assert events[1].passed is True
    assert events[1].errors == []
    assert isinstance(events[2], CompletedEvent)
    assert events[2].result.status == "success"


def test_translator_emits_event_sequence_on_fix_loop() -> None:
    """One fix cycle: Generated → Validated(failed) → FixGenerated → Validated(passed) → Completed."""
    from rows2graph import (
        CompletedEvent,
        FixGeneratedEvent,
        GeneratedEvent,
        TranslationEvent,
        ValidatedEvent,
    )

    fake = _FakeLLM(
        [
            "MATCH (p:Person",  # malformed: unbalanced parenthesis
            "MATCH (p:Person) RETURN p",
        ]
    )
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons", on_event=events.append)

    types = [type(e).__name__ for e in events]
    assert types == [
        "GeneratedEvent",
        "ValidatedEvent",
        "FixGeneratedEvent",
        "ValidatedEvent",
        "CompletedEvent",
    ]
    assert isinstance(events[0], GeneratedEvent) and events[0].iteration == 1
    assert isinstance(events[1], ValidatedEvent) and events[1].iteration == 1 and events[1].passed is False
    assert events[1].errors  # non-empty
    assert isinstance(events[2], FixGeneratedEvent) and events[2].iteration == 1
    assert events[2].query == "MATCH (p:Person) RETURN p"
    assert isinstance(events[3], ValidatedEvent) and events[3].iteration == 2 and events[3].passed is True
    assert isinstance(events[4], CompletedEvent)


def test_translator_emits_max_iterations_event_when_loop_gives_up() -> None:
    from rows2graph import CompletedEvent, MaxIterationsReachedEvent, TranslationEvent

    fake = _FakeLLM(["MATCH (p:Person"] * 3)  # always invalid
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        max_iterations=3,
    ) as translator:
        translator.translate("SELECT 1", on_event=events.append)

    max_events = [e for e in events if isinstance(e, MaxIterationsReachedEvent)]
    assert len(max_events) == 1
    assert max_events[0].iteration == 3
    assert max_events[0].errors  # non-empty
    # CompletedEvent is always the last event, even on max-iterations failure.
    assert isinstance(events[-1], CompletedEvent)
    assert events[-1].result.status == "max_iterations_reached"


def test_translator_translates_without_on_event_handler() -> None:
    """Backwards-compat: omitting on_event must not change behavior."""
    fake = _FakeLLM(["MATCH (p) RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT 1")
    assert result.status == "success"


def test_translator_swallows_handler_exceptions() -> None:
    """A misbehaving handler must not abort the translation."""

    def boom(_event: object) -> None:
        raise RuntimeError("handler bug")

    fake = _FakeLLM(["MATCH (p) RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT 1", on_event=boom)
    assert result.status == "success"
    assert result.generated_query == "MATCH (p) RETURN p"


# ---------------------------------------------------------------------------
# SQL feature detection (sql_features)
# ---------------------------------------------------------------------------


def test_detect_features_plain_select_is_empty() -> None:
    assert detect_features("SELECT a, b FROM t") == frozenset()


def test_detect_features_like() -> None:
    assert SqlFeature.LIKE in detect_features("SELECT a FROM t WHERE name LIKE '%x%'")


def test_detect_features_ilike() -> None:
    assert SqlFeature.LIKE in detect_features("SELECT a FROM t WHERE name ILIKE '%x%'")


def test_detect_features_join() -> None:
    assert SqlFeature.JOIN in detect_features("SELECT a.x FROM a JOIN b ON a.id = b.aid")


def test_detect_features_group_by() -> None:
    feats = detect_features("SELECT k, COUNT(*) FROM t GROUP BY k")
    assert SqlFeature.AGGREGATION in feats


def test_detect_features_aggregate_without_group_by() -> None:
    assert SqlFeature.AGGREGATION in detect_features("SELECT COUNT(*) FROM t")


def test_detect_features_having_implies_aggregation() -> None:
    assert SqlFeature.AGGREGATION in detect_features("SELECT k, COUNT(*) c FROM t GROUP BY k HAVING COUNT(*) > 5")


def test_detect_features_order_limit() -> None:
    feats = detect_features("SELECT a FROM t ORDER BY a LIMIT 10")
    assert SqlFeature.ORDER_LIMIT in feats


def test_detect_features_cte() -> None:
    feats = detect_features("WITH c AS (SELECT * FROM t) SELECT * FROM c")
    assert SqlFeature.CTE in feats
    # A bare CTE without any nested SELECT inside an expression must NOT
    # light up SUBQUERY: distinguishing the two clusters is the whole point.
    assert SqlFeature.SUBQUERY not in feats


def test_detect_features_union() -> None:
    assert SqlFeature.UNION in detect_features("SELECT a FROM t UNION SELECT b FROM u")


def test_detect_features_window() -> None:
    feats = detect_features("SELECT a, ROW_NUMBER() OVER (ORDER BY b) FROM t")
    assert SqlFeature.WINDOW in feats


def test_detect_features_case() -> None:
    feats = detect_features("SELECT CASE WHEN a > 0 THEN 1 ELSE 0 END FROM t")
    assert SqlFeature.CASE in feats


def test_detect_features_scalar_subquery() -> None:
    feats = detect_features("SELECT (SELECT MAX(b) FROM u) FROM t")
    assert SqlFeature.SUBQUERY in feats


def test_detect_features_in_subquery() -> None:
    feats = detect_features("SELECT a FROM t WHERE a IN (SELECT b FROM u)")
    assert SqlFeature.SUBQUERY in feats


def test_detect_features_exists_subquery() -> None:
    feats = detect_features("SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.x = t.x)")
    assert SqlFeature.SUBQUERY in feats


def test_detect_features_distinct() -> None:
    assert SqlFeature.DISTINCT in detect_features("SELECT DISTINCT a FROM t")


def test_detect_features_temporal_date_literal() -> None:
    # A comparison against an ISO date/timestamp string literal must light up
    # TEMPORAL so the date()/datetime() wrapping rule ships.
    assert SqlFeature.TEMPORAL in detect_features("SELECT a FROM lineitem WHERE shipdate >= '1995-03-01'")
    assert SqlFeature.TEMPORAL in detect_features(
        "SELECT a FROM post WHERE creation_date >= '2010-06-01' AND creation_date < '2010-07-01'"
    )


def test_detect_features_temporal_not_fired_on_non_date() -> None:
    # An integer key, ordinary string equality, and a plain numeric predicate
    # must NOT light up TEMPORAL: the detector keys on date-shaped literals.
    assert SqlFeature.TEMPORAL not in detect_features("SELECT a FROM supplier WHERE suppkey = 1337")
    assert SqlFeature.TEMPORAL not in detect_features("SELECT a FROM supplier WHERE name = 'Supplier#000000666'")
    assert SqlFeature.TEMPORAL not in detect_features("SELECT a, b FROM t WHERE x = 1")


def test_detect_features_scalar_functions() -> None:
    # String/number/coalesce scalars and a non-temporal CAST light up SCALAR so
    # the per-target function-mapping chunk ships only when it is needed.
    assert SqlFeature.SCALAR in detect_features("SELECT UPPER(name) FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT a || b FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT COALESCE(a, b) FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT LENGTH(name) FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT CAST(x AS INTEGER) FROM t")


def test_detect_features_scalar_not_fired_on_plain_or_temporal() -> None:
    # A plain select has no scalar functions; a *temporal* cast belongs to
    # TEMPORAL and must not also drag in the scalar-function chunk.
    assert SqlFeature.SCALAR not in detect_features("SELECT a, b FROM t WHERE x = 1")
    assert SqlFeature.SCALAR not in detect_features("SELECT a FROM t WHERE d >= CAST('1995-03-01' AS DATE)")


def test_detect_features_null_predicate() -> None:
    # IS NULL / IS NOT NULL light up NULL so the per-target null-handling chunk ships.
    assert SqlFeature.NULL in detect_features("SELECT a FROM t WHERE col IS NULL")
    assert SqlFeature.NULL in detect_features("SELECT a FROM t WHERE col IS NOT NULL")


def test_detect_features_null_not_fired_without_null_test() -> None:
    # Ordinary predicates must not light up NULL.
    assert SqlFeature.NULL not in detect_features("SELECT a FROM t WHERE x = 1")
    assert SqlFeature.NULL not in detect_features("SELECT a FROM t WHERE name = 'x'")


def test_detect_features_parse_failure_returns_all() -> None:
    # Garbage SQL must yield the full feature set so no rule chunk is
    # silently stripped from the prompt.
    assert detect_features("SELECT ;;; FROM") == ALL_FEATURES


# ---------------------------------------------------------------------------
# analyze_sql: features + source tables + parse status (rows2graph.sql_features)
# ---------------------------------------------------------------------------


def test_analyze_sql_reports_features_tables_and_parse_ok() -> None:
    a = analyze_sql("SELECT * FROM orders o JOIN customer c ON o.custkey = c.custkey")
    assert a.parse_ok is True
    assert a.source_tables == frozenset({"orders", "customer"})
    assert SqlFeature.JOIN in a.features


def test_analyze_sql_parse_failure_keeps_all_features_and_flags_parse() -> None:
    # The load-bearing fallback: an unparseable query still ships every rule
    # (so the prompt isn't silently trimmed) but parse_ok records the failure
    # and the table set is empty (so any coverage check is a no-op).
    a = analyze_sql("SELECT ;;; FROM")
    assert a.parse_ok is False
    assert a.features == ALL_FEATURES
    assert a.source_tables == frozenset()


def test_analyze_sql_excludes_cte_and_derived_table_names() -> None:
    # A CTE name and a derived-table alias look like tables to a naive
    # find_all(exp.Table); the scope-based extractor must report only the real
    # underlying tables.
    cte = analyze_sql("WITH recent AS (SELECT * FROM orders) SELECT * FROM recent r JOIN customer c ON r.x = c.x")
    assert cte.source_tables == frozenset({"orders", "customer"})
    derived = analyze_sql("SELECT * FROM (SELECT * FROM orders) sub JOIN customer c ON 1 = 1")
    assert derived.source_tables == frozenset({"orders", "customer"})


def test_analyze_sql_strips_schema_qualifier_and_preserves_casing() -> None:
    a = analyze_sql('SELECT * FROM public.Orders, "Customer"')
    # Schema qualifier dropped (bare name); original casing preserved so an
    # error message echoes what the user wrote.
    assert a.source_tables == frozenset({"Orders", "Customer"})


def test_analyze_sql_no_tables_for_tableless_and_dml() -> None:
    assert analyze_sql("SELECT 1").source_tables == frozenset()
    # DML/DDL enumerate no SELECT-scope sources, so coverage never fires on them.
    assert analyze_sql("INSERT INTO persons (id) VALUES (1)").source_tables == frozenset()


# ---------------------------------------------------------------------------
# Schema-mapping coverage (SchemaMapping.source_tables / find_unmapped_tables)
# ---------------------------------------------------------------------------


def test_schema_mapping_source_tables_unions_nodes_and_edges() -> None:
    # _schema() maps persons + forums (nodes) and knows (edge source table).
    assert _schema().source_tables() == {"persons", "forums", "knows"}


def test_find_unmapped_tables_is_case_insensitive_and_sorted() -> None:
    mapping = _schema()
    # "persons" is covered (case-insensitively); "orders" and "Lineitem" are not.
    unmapped = find_unmapped_tables(frozenset({"PERSONS", "orders", "Lineitem"}), mapping)
    assert unmapped == ["Lineitem", "orders"]  # sorted, original casing kept


def test_find_unmapped_tables_empty_when_all_covered() -> None:
    assert find_unmapped_tables(frozenset({"persons", "knows"}), _schema()) == []


# ---------------------------------------------------------------------------
# Translator input pre-flight (parse-failure warn / unmapped-tables reject)
# ---------------------------------------------------------------------------


def test_translator_rejects_unmapped_tables_without_calling_llm() -> None:
    from rows2graph import CompletedEvent, TranslationEvent, UnmappedTablesEvent

    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])  # must never be consumed
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT * FROM nonexistent_table", on_event=events.append)

    assert result.status == "unmapped_tables"
    assert result.validation_passed is False
    assert result.unmapped_tables == ["nonexistent_table"]
    assert result.generated_query is None
    assert result.iterations_used == 0
    assert result.token_usage.total_tokens == 0
    assert len(fake.calls) == 0  # the LLM was skipped entirely
    # Exactly the rejection event then the always-last CompletedEvent.
    assert [type(e).__name__ for e in events] == ["UnmappedTablesEvent", "CompletedEvent"]
    assert isinstance(events[0], UnmappedTablesEvent)
    assert isinstance(events[-1], CompletedEvent)


def test_translator_warns_on_parse_failure_but_still_translates() -> None:
    from rows2graph import GeneratedEvent, ParseFailedEvent, TranslationEvent

    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT ;;; FROM", on_event=events.append)

    # Warn, don't block: the LLM still ran and produced a result.
    assert result.status == "success"
    assert len(fake.calls) == 1
    assert result.validation_errors == []  # warn must not pollute validation errors
    parse_events = [e for e in events if isinstance(e, ParseFailedEvent)]
    assert len(parse_events) == 1
    # The warning precedes the initial generation event.
    assert events.index(parse_events[0]) < next(i for i, e in enumerate(events) if isinstance(e, GeneratedEvent))


def test_translator_no_preflight_events_for_mapped_query() -> None:
    from rows2graph import ParseFailedEvent, TranslationEvent, UnmappedTablesEvent

    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons", on_event=events.append)

    assert not any(isinstance(e, (ParseFailedEvent, UnmappedTablesEvent)) for e in events)


def test_translator_does_not_flag_cte_name_as_unmapped() -> None:
    # A CTE alias must not be mistaken for an unmapped table: the underlying
    # 'persons' is mapped, so this translates normally.
    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("WITH recent AS (SELECT * FROM persons) SELECT * FROM recent")

    assert result.status == "success"
    assert result.unmapped_tables == []


def test_translator_ignore_action_keeps_legacy_behavior() -> None:
    # With both actions IGNORE, an unmapped table is translated as before
    # (the LLM is called, no preflight events, no rejection).
    from rows2graph import ParseFailedEvent, TranslationEvent, UnmappedTablesEvent

    fake = _FakeLLM(["MATCH (x) RETURN x"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        parse_error_action=PreflightAction.IGNORE,
        unmapped_tables_action=PreflightAction.IGNORE,
    ) as translator:
        result = translator.translate("SELECT * FROM nonexistent_table", on_event=events.append)

    assert result.status == "success"
    assert len(fake.calls) == 1
    assert not any(isinstance(e, (ParseFailedEvent, UnmappedTablesEvent)) for e in events)


# ---------------------------------------------------------------------------
# Column-reference extraction (analyze_sql.column_refs)
# ---------------------------------------------------------------------------

_GAP_SQL = (
    "SELECT f.id, f.title, COUNT(fhm.person_id) AS member_count "
    "FROM forum f LEFT JOIN forum_has_member fhm ON fhm.forum_id = f.id "
    "GROUP BY f.id, f.title ORDER BY member_count DESC"
)


def test_analyze_sql_extracts_qualified_column_refs() -> None:
    # Qualified columns resolve to their real table; the SELECT alias
    # (member_count) must NOT appear as a column.
    assert analyze_sql(_GAP_SQL).column_refs == frozenset(
        {("forum", "id"), ("forum", "title"), ("forum_has_member", "forum_id"), ("forum_has_member", "person_id")}
    )


def test_analyze_sql_attributes_single_table_unqualified_columns() -> None:
    # In a single-table leaf scope, bare columns attribute to the sole table.
    assert analyze_sql("SELECT id, title FROM forum").column_refs == frozenset({("forum", "id"), ("forum", "title")})


def test_analyze_sql_does_not_leak_subquery_columns() -> None:
    # The leaf-scope gate: the outer scope has a child subquery, so its
    # unqualified columns are NOT attributed; `person_id` must bind to `knows`
    # (its own leaf), never to `persons`.
    refs = analyze_sql("SELECT p.full_name FROM persons p WHERE p.id IN (SELECT person_id FROM knows)").column_refs
    assert ("persons", "person_id") not in refs
    assert ("knows", "person_id") in refs
    assert ("persons", "full_name") in refs


def test_analyze_sql_no_column_refs_for_star_cte_alias_and_dml() -> None:
    assert analyze_sql("SELECT * FROM persons").column_refs == frozenset()
    assert analyze_sql("SELECT 1").column_refs == frozenset()
    assert analyze_sql("INSERT INTO persons (id) VALUES (1)").column_refs == frozenset()
    # A CTE alias is not a real table, so a column off it is excluded.
    assert analyze_sql("WITH r AS (SELECT * FROM forum) SELECT r.id FROM r").column_refs == frozenset()


# ---------------------------------------------------------------------------
# Column coverage (find_unmapped_columns)
# ---------------------------------------------------------------------------


def _forum_no_title() -> SchemaMapping:
    return SchemaMapping(
        nodes=[NodeMapping(label="Forum", source_table="forum", primary_key="id", properties={"id": "id"})],
        edges=[],
    )


def test_find_unmapped_columns_flags_missing_property() -> None:
    mapping = _forum_no_title()
    assert find_unmapped_columns(frozenset({("forum", "title")}), mapping) == ["forum.title"]
    assert find_unmapped_columns(frozenset({("forum", "id")}), mapping) == []


def test_find_unmapped_columns_skips_pure_junction_tables() -> None:
    # `knows` in _schema() is only an edge source (never a node), so its columns
    # are not checkable; a junction FK must never be flagged.
    assert find_unmapped_columns(frozenset({("knows", "forum_id"), ("knows", "anything")}), _schema()) == []


def test_find_unmapped_columns_absorbs_join_keys_of_node_plus_edge_table() -> None:
    # A table that is BOTH a node source and an edge source: the edge's FK/PK
    # join columns are absorbed as covered, so they aren't false-flagged.
    mapping = SchemaMapping(
        nodes=[NodeMapping(label="Forum", source_table="forum", primary_key="id", properties={"id": "id"})],
        edges=[
            EdgeMapping(
                type="OWNS",
                source_node="Forum",
                target_node="Forum",
                source_table="forum",
                source_foreign_key="owner_id",
                target_primary_key="id",
            )
        ],
    )
    assert find_unmapped_columns(frozenset({("forum", "owner_id"), ("forum", "id")}), mapping) == []
    assert find_unmapped_columns(frozenset({("forum", "title")}), mapping) == ["forum.title"]


def test_find_unmapped_columns_is_case_insensitive_and_sorted() -> None:
    # Covered comparison casefolds both sides; output keeps SQL casing, sorted.
    assert find_unmapped_columns(frozenset({("PERSONS", "Full_Name")}), _schema()) == []
    assert find_unmapped_columns(frozenset({("forums", "z_missing"), ("forums", "a_missing")}), _schema()) == [
        "forums.a_missing",
        "forums.z_missing",
    ]


# ---------------------------------------------------------------------------
# Translator unmapped-column pre-flight (warn default / reject opt-in)
# ---------------------------------------------------------------------------


def test_translator_warns_on_unmapped_column_when_configured_to_warn() -> None:
    from rows2graph import GeneratedEvent, TranslationEvent, UnmappedColumnsEvent

    fake = _FakeLLM(["MATCH (f:Forum) RETURN f"])
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
        unmapped_columns_action=PreflightAction.WARN,  # opt out of the reject default
    ) as translator:
        result = translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)

    assert result.status == "success"
    assert len(fake.calls) == 1  # warn does not block the LLM
    assert result.validation_errors == []  # must not pollute validation errors
    assert result.unmapped_columns == ["forums.bogus"]  # self-describing result
    parse = [e for e in events if isinstance(e, UnmappedColumnsEvent)]
    assert len(parse) == 1
    assert parse[0].columns == ["forums.bogus"]
    # The warning precedes the initial generation event.
    assert events.index(parse[0]) < next(i for i, e in enumerate(events) if isinstance(e, GeneratedEvent))


def test_translator_rejects_unmapped_column_by_default() -> None:
    from rows2graph import CompletedEvent, TranslationEvent

    fake = _FakeLLM(["MATCH (f:Forum) RETURN f"])  # must never be consumed
    events: list[TranslationEvent] = []
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        result = translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)

    assert result.status == "unmapped_columns"
    assert result.unmapped_columns == ["forums.bogus"]
    assert result.generated_query is None
    assert result.token_usage.total_tokens == 0
    assert len(fake.calls) == 0
    assert [type(e).__name__ for e in events] == ["UnmappedColumnsEvent", "CompletedEvent"]
    assert isinstance(events[-1], CompletedEvent)


def test_translator_no_column_signal_for_mapped_or_star_queries() -> None:
    from rows2graph import TranslationEvent, UnmappedColumnsEvent

    for sql in ("SELECT * FROM persons", "SELECT full_name FROM persons WHERE id = 1"):
        fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
        events: list[TranslationEvent] = []
        with SQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=CypherSyntaxValidator(),
        ) as translator:
            result = translator.translate(sql, on_event=events.append)
        assert not any(isinstance(e, UnmappedColumnsEvent) for e in events), sql
        assert result.unmapped_columns == [], sql


# ---------------------------------------------------------------------------
# Feature-gated prompt assembly
# ---------------------------------------------------------------------------


def test_cypher_prompt_includes_like_chunk_only_when_feature_detected() -> None:
    with_like = build_system_prompt(_schema(), CypherTarget(), frozenset({SqlFeature.LIKE}))
    without_like = build_system_prompt(_schema(), CypherTarget(), frozenset())
    assert "CONTAINS" in with_like
    assert "CONTAINS" not in without_like


def test_cypher_prompt_omits_window_when_not_detected() -> None:
    prompt = build_system_prompt(_schema(), CypherTarget(), frozenset({SqlFeature.LIKE}))
    assert "window function" not in prompt.lower()


def test_cypher_prompt_includes_window_chunk_when_detected() -> None:
    prompt = build_system_prompt(_schema(), CypherTarget(), frozenset({SqlFeature.WINDOW}))
    assert "window function" in prompt.lower()


def test_cypher_prompt_includes_temporal_chunk_only_when_detected() -> None:
    with_temporal = build_system_prompt(_schema(), CypherTarget(), frozenset({SqlFeature.TEMPORAL}))
    without_temporal = build_system_prompt(_schema(), CypherTarget(), frozenset())
    # `datetime(` is unique to the temporal chunk and absent from the base block.
    assert "datetime(" in with_temporal
    assert "datetime(" not in without_temporal


def test_cypher_base_rules_carry_anti_pattern_block() -> None:
    # The always-on base block must now mirror AQL/Gremlin: a concrete data
    # model plus an explicit "NOT valid Cypher" anti-pattern list and an
    # output-format mandate, present even with no features detected.
    prompt = build_system_prompt(_schema(), CypherTarget(), frozenset())
    assert "NOT valid Cypher" in prompt
    assert "Output ONLY the query" in prompt
    assert "MATCH" in prompt


def test_aql_prompt_includes_collect_only_when_aggregation_detected() -> None:
    with_agg = build_system_prompt(_schema(), AqlTarget(), frozenset({SqlFeature.AGGREGATION}))
    without_agg = build_system_prompt(_schema(), AqlTarget(), frozenset())
    assert "COLLECT" in with_agg
    assert "COLLECT" not in without_agg


def test_gremlin_prompt_includes_textp_only_when_like_detected() -> None:
    with_like = build_system_prompt(_schema(), GremlinTarget(), frozenset({SqlFeature.LIKE}))
    without_like = build_system_prompt(_schema(), GremlinTarget(), frozenset())
    assert "TextP.containing" in with_like
    assert "TextP.containing" not in without_like


def test_gremlin_prompt_includes_dedup_only_when_distinct_detected() -> None:
    with_distinct = build_system_prompt(_schema(), GremlinTarget(), frozenset({SqlFeature.DISTINCT}))
    without_distinct = build_system_prompt(_schema(), GremlinTarget(), frozenset())
    assert ".dedup()" in with_distinct
    assert ".dedup()" not in without_distinct


def test_aql_prompt_includes_temporal_chunk_only_when_detected() -> None:
    # Every target now defines a TEMPORAL chunk (no silent gaps). For AQL the
    # chunk is gated on `DATE_TIMESTAMP`, a substring absent from the base block.
    with_temporal = build_system_prompt(_schema(), AqlTarget(), frozenset({SqlFeature.TEMPORAL}))
    without_temporal = build_system_prompt(_schema(), AqlTarget(), frozenset())
    assert "DATE_TIMESTAMP" in with_temporal
    assert "DATE_TIMESTAMP" not in without_temporal


def test_gremlin_prompt_includes_temporal_chunk_only_when_detected() -> None:
    # Gremlin's TEMPORAL chunk is gated on `epoch`, absent from the base block.
    with_temporal = build_system_prompt(_schema(), GremlinTarget(), frozenset({SqlFeature.TEMPORAL}))
    without_temporal = build_system_prompt(_schema(), GremlinTarget(), frozenset())
    assert "epoch" in with_temporal
    assert "epoch" not in without_temporal


def test_all_targets_build_with_all_features() -> None:
    # TEMPORAL is in ALL_FEATURES (emitted on parse-failure); every target must
    # build the full rule set without raising now that coverage is total.
    for target in (CypherTarget(), AqlTarget(), GremlinTarget()):
        prompt = build_system_prompt(_schema(), target, ALL_FEATURES)
        assert prompt  # built without raising


@pytest.mark.parametrize(
    ("target_cls", "marker"),
    [
        (CypherTarget, "toInteger"),
        (AqlTarget, "TO_NUMBER"),
        (GremlinTarget, ".toUpper()"),
    ],
)
def test_scalar_chunk_gated_per_target(target_cls: type, marker: str) -> None:
    # The scalar-function mapping table must appear only when SCALAR is detected,
    # and its marker substring must stay OUT of the always-on base block (so the
    # token-saving gate actually holds).
    with_scalar = build_system_prompt(_schema(), target_cls(), frozenset({SqlFeature.SCALAR}))
    without_scalar = build_system_prompt(_schema(), target_cls(), frozenset())
    assert marker in with_scalar
    assert marker not in without_scalar


@pytest.mark.parametrize(
    ("target_cls", "marker"),
    [
        (CypherTarget, "IS NOT NULL"),
        (AqlTarget, "!= null"),
        (GremlinTarget, ".hasNot("),
    ],
)
def test_null_chunk_gated_per_target(target_cls: type, marker: str) -> None:
    # The null-handling chunk must appear only when NULL is detected and its
    # marker must be absent from the base block.
    with_null = build_system_prompt(_schema(), target_cls(), frozenset({SqlFeature.NULL}))
    without_null = build_system_prompt(_schema(), target_cls(), frozenset())
    assert marker in with_null
    assert marker not in without_null


def test_generic_join_rule_is_feature_gated() -> None:
    with_join = build_system_prompt(_schema(), CypherTarget(), frozenset({SqlFeature.JOIN}))
    without_join = build_system_prompt(_schema(), CypherTarget(), frozenset())
    assert "Map SQL JOINs" in with_join
    assert "Map SQL JOINs" not in without_join


def test_gremlin_join_projection_guidance_only_when_join_detected() -> None:
    # The "label-as-you-go then select(...).by(...), walk the path once"
    # pattern is the fix for multi-table SELECT joins; it should appear only
    # when a JOIN is present, not on a single-table query.
    with_join = build_system_prompt(_schema(), GremlinTarget(), frozenset({SqlFeature.JOIN}))
    without_join = build_system_prompt(_schema(), GremlinTarget(), frozenset())
    assert "Walk the path ONCE" in with_join
    assert "Walk the path ONCE" not in without_join


def test_translator_omits_unused_rules_from_system_message() -> None:
    # SQL has only LIKE; the system prompt should carry the LIKE chunk
    # and omit the WINDOW chunk.
    fake = _FakeLLM(["MATCH (p:Person) WHERE p.name CONTAINS 'a' RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons WHERE full_name LIKE '%a%'")

    system_msg = fake.calls[0][0]
    assert system_msg["role"] == "system"
    assert "CONTAINS" in system_msg["content"]
    assert "window function" not in system_msg["content"].lower()


# ---------------------------------------------------------------------------
# Cross-target rule-schema parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_mod", _ALL_TARGET_MODULES)
def test_target_feature_rules_cover_every_sql_feature(target_mod: Any) -> None:
    # The keystone parity guard: every target must define a rule chunk for every
    # SqlFeature, so a half-landed feature (a chunk in one target but silently
    # missing from another, as TEMPORAL once was) fails the suite instead of
    # disappearing into a tolerant `.get()` lookup.
    assert set(target_mod._FEATURE_RULES) == set(SqlFeature)


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
def test_target_base_block_has_uniform_sections(target_cls: type) -> None:
    # Every target's always-on base block renders the same five-section
    # skeleton with the same headers, regardless of detected features.
    base = build_system_prompt(_schema(), target_cls(), frozenset())
    for header in ("Data model:", "Core syntax:", "These are NOT valid", "Examples:"):
        assert header in base, f"{target_cls.__name__} base missing section {header!r}"


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
def test_target_base_block_renders_shared_examples(target_cls: type) -> None:
    # Every target shows its translation of the same shared SQL inputs, so the
    # worked examples line up across languages.
    base = build_system_prompt(_schema(), target_cls(), frozenset())
    assert EX_POINT_LOOKUP_SQL in base
    assert EX_JOIN_FILTER_SQL in base


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
def test_order_limit_chunk_has_worked_example(target_cls: type) -> None:
    # Every target's ORDER_LIMIT chunk carries a worked sort+limit example (parity);
    # it is gated, so it appears only when ORDER_LIMIT is detected.
    sql = "SELECT name FROM table_a ORDER BY value DESC LIMIT 10"
    with_ol = build_system_prompt(_schema(), target_cls(), frozenset({SqlFeature.ORDER_LIMIT}))
    without_ol = build_system_prompt(_schema(), target_cls(), frozenset())
    assert sql in with_ol
    assert sql not in without_ol


# ---------------------------------------------------------------------------
# AQL clause-ordering and set-operation rules (regression: qwen3-coder failures)
# ---------------------------------------------------------------------------


def test_aql_base_teaches_sort_limit_before_return() -> None:
    # The fatal AQL failure mode (aql-11/aql-12): SORT/LIMIT placed after RETURN.
    # The always-on base block must teach that RETURN terminates the FOR block,
    # with a BAD->GOOD anti-pattern showing the correct ordering.
    base = build_system_prompt(_schema(), AqlTarget(), frozenset())
    assert "must come BEFORE `RETURN`" in base
    assert "SORT LENGTH(items) DESC LIMIT 10 RETURN" in base  # GOOD ordering shown


def test_aql_base_teaches_junction_table_is_an_edge() -> None:
    # Parity with Cypher/Gremlin: AQL's always-on data model must warn that a
    # junction/link table is an edge collection, not a vertex collection, so the
    # model does not invent `FOR x IN PartSupp`.
    base = build_system_prompt(_schema(), AqlTarget(), frozenset())
    assert "junction / link table is an EDGE collection" in base
    assert "never `FILTER` on `*key`/`*_id` columns" in base


def test_aql_union_rule_warns_function_not_infix() -> None:
    # aql-13 wasted iterations writing `FOR...RETURN UNION_DISTINCT FOR...RETURN`
    # (SQL-style infix). The UNION chunk must warn it is a function, not infix.
    with_union = build_system_prompt(_schema(), AqlTarget(), frozenset({SqlFeature.UNION}))
    without_union = build_system_prompt(_schema(), AqlTarget(), frozenset())
    assert "NOT an infix" in with_union
    assert "NOT an infix" not in without_union


# ---------------------------------------------------------------------------
# AsyncSQLTranslator end-to-end with fake async LLM
# ---------------------------------------------------------------------------


class _FakeAsyncLLM:
    """In-process double for the AsyncLLMClient Protocol.

    When ``stream_to`` is supplied, emits the response character-by-character
    through the callback before returning the full text, enough to exercise
    the streaming plumbing without needing a real LLM.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.temperatures: list[float | None] = []
        self.stream_calls: int = 0
        self.closed = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream_to: Any = None,
        temperature: float | None = None,
    ) -> ChatReply:
        self.calls.append(list(messages))
        self.temperatures.append(temperature)
        response = self._responses.pop(0)
        if stream_to is not None:
            self.stream_calls += 1
            for char in response:
                stream_to(char)
        # Fixed per-call usage (15 total tokens) lets loop tests assert accumulation.
        return ChatReply(text=response, usage=TokenUsage(input_tokens=10, output_tokens=5))

    async def close(self) -> None:
        self.closed = True


def test_async_translator_returns_result_on_first_try_success() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = _FakeAsyncLLM(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            result = await translator.translate("SELECT * FROM persons")
        assert fake.closed is True
        return result

    result = asyncio.run(run())
    assert result.validation_passed is True
    assert result.status == "success"
    assert result.iterations_used == 1
    assert result.generated_query == "MATCH (p:Person) RETURN p"


def test_async_translator_forwards_dialect_to_analyze_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async mirror of the sync forwarding test: the constructor ``dialect`` reaches
    ``async_translator.analyze_sql`` (kept in lockstep with the sync path)."""
    import asyncio

    import rows2graph.async_translator as async_translator_mod
    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    seen = _spy_analyze_sql(monkeypatch, async_translator_mod)

    async def run() -> None:
        fake = _FakeAsyncLLM(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            dialect="mysql",
        ) as translator:
            await translator.translate("SELECT * FROM persons")

    asyncio.run(run())
    assert seen == ["mysql"]


def test_async_translator_runs_fix_loop_on_validation_failure() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = _FakeAsyncLLM(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            return await translator.translate("SELECT * FROM persons")

    result = asyncio.run(run())
    assert result.validation_passed is True
    assert result.iterations_used == 2
    # Usage accumulates across both async LLM calls: 2 × (10 in + 5 out).
    assert result.token_usage.total_tokens == 30


def test_async_translator_rejects_unmapped_tables_without_calling_llm() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator, CompletedEvent, TranslationEvent
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[TranslationResult, _FakeAsyncLLM, list[TranslationEvent]]:
        fake = _FakeAsyncLLM(["MATCH (p:Person) RETURN p"])  # must never be consumed
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            result = await translator.translate("SELECT * FROM nonexistent_table", on_event=events.append)
        return result, fake, events

    result, fake, events = asyncio.run(run())
    assert result.status == "unmapped_tables"
    assert result.unmapped_tables == ["nonexistent_table"]
    assert result.generated_query is None
    assert result.token_usage.total_tokens == 0
    assert len(fake.calls) == 0
    assert [type(e).__name__ for e in events] == ["UnmappedTablesEvent", "CompletedEvent"]
    assert isinstance(events[-1], CompletedEvent)


def test_async_translator_unmapped_column_warn_and_reject() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator, TranslationEvent, UnmappedColumnsEvent
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def warn() -> tuple[TranslationResult, _FakeAsyncLLM, list[TranslationEvent]]:
        fake = _FakeAsyncLLM(["MATCH (f:Forum) RETURN f"])
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            unmapped_columns_action=PreflightAction.WARN,  # opt out of the reject default
        ) as translator:
            result = await translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)
        return result, fake, events

    async def reject() -> tuple[TranslationResult, _FakeAsyncLLM, list[TranslationEvent]]:
        # No explicit action: reject is the default.
        fake = _FakeAsyncLLM(["MATCH (f:Forum) RETURN f"])
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=_schema(), llm=fake, target=CypherTarget(), validator=AsyncCypherSyntaxValidator()
        ) as translator:
            result = await translator.translate("SELECT f.title, f.bogus FROM forums f", on_event=events.append)
        return result, fake, events

    wr, wfake, wevents = asyncio.run(warn())
    assert wr.status == "success"
    assert len(wfake.calls) == 1
    assert wr.unmapped_columns == ["forums.bogus"]
    assert any(isinstance(e, UnmappedColumnsEvent) for e in wevents)

    rr, rfake, revents = asyncio.run(reject())
    assert rr.status == "unmapped_columns"
    assert rr.unmapped_columns == ["forums.bogus"]
    assert len(rfake.calls) == 0
    assert [type(e).__name__ for e in revents] == ["UnmappedColumnsEvent", "CompletedEvent"]


def test_async_translator_hits_max_iterations() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = _FakeAsyncLLM(["MATCH (p:Person"] * 3)
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            max_iterations=3,
        ) as translator:
            return await translator.translate("SELECT * FROM persons")

    result = asyncio.run(run())
    assert result.validation_passed is False
    assert result.status == "max_iterations_reached"
    assert result.iterations_used == 3


def test_async_translator_escalates_and_aborts_when_stalled() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[TranslationResult, _FakeAsyncLLM]:
        fake = _FakeAsyncLLM(["MATCH (p:Person"] * 4)  # always invalid
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
            max_iterations=10,
        ) as translator:
            return await translator.translate("SELECT * FROM persons"), fake

    result, fake = asyncio.run(run())
    assert result.status == "stalled"
    assert result.iterations_used == 3
    assert len(fake.calls) == 3
    # The escalation call (3rd) ran at the hot escalation temperature.
    assert fake.temperatures == [None, None, 0.6]


def test_async_translator_emits_same_event_sequence_as_sync() -> None:
    """The async translator emits the same event sequence as the sync one
    for an identical input: events are part of the cross-translator contract."""
    import asyncio

    from rows2graph import (
        AsyncSQLTranslator,
        CompletedEvent,
        FixGeneratedEvent,
        GeneratedEvent,
        TranslationEvent,
        ValidatedEvent,
    )
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> list[TranslationEvent]:
        fake = _FakeAsyncLLM(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])
        events: list[TranslationEvent] = []
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons", on_event=events.append)
        return events

    events = asyncio.run(run())
    types = [type(e).__name__ for e in events]
    assert types == [
        "GeneratedEvent",
        "ValidatedEvent",
        "FixGeneratedEvent",
        "ValidatedEvent",
        "CompletedEvent",
    ]
    assert isinstance(events[0], GeneratedEvent) and events[0].iteration == 1
    assert isinstance(events[1], ValidatedEvent) and events[1].passed is False
    assert isinstance(events[2], FixGeneratedEvent) and events[2].iteration == 1
    assert isinstance(events[3], ValidatedEvent) and events[3].passed is True
    assert isinstance(events[4], CompletedEvent)


def test_make_async_llm_dispatches_correctly() -> None:
    from rows2graph import make_async_llm
    from rows2graph.llm.anthropic import AsyncAnthropicLLMClient
    from rows2graph.llm.ollama import AsyncOllamaLLMClient

    with patch("rows2graph.llm.anthropic.AsyncAnthropic"):
        llm = make_async_llm(AnthropicConfig(api_key="sk-ant-test"))
        assert isinstance(llm, AsyncAnthropicLLMClient)

    with patch("rows2graph.llm.ollama.AsyncClient"):
        llm = make_async_llm(OllamaConfig(model="m", host="http://x:1"))
        assert isinstance(llm, AsyncOllamaLLMClient)


def test_make_async_validator_dispatches_correctly() -> None:
    from rows2graph import make_async_validator
    from rows2graph.validators import (
        AsyncCypherSyntaxValidator,
        AsyncNoopValidator,
    )

    assert isinstance(make_async_validator("cypher", "none"), AsyncNoopValidator)
    assert isinstance(make_async_validator("cypher", "syntax"), AsyncCypherSyntaxValidator)


def test_async_cypher_syntax_validator_matches_sync() -> None:
    """Async syntax validator returns the same errors as its sync sibling."""
    import asyncio

    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async_v = AsyncCypherSyntaxValidator()
    sync_v = CypherSyntaxValidator()
    cases = [
        "MATCH (p:Person) RETURN p",
        "MATCH (p:Person",  # malformed: unbalanced parenthesis
        "",  # empty
        "MATCH (p:Person RETURN p",  # unbalanced paren
    ]
    for q in cases:
        sync_errors = sync_v.validate(q)
        async_errors = asyncio.run(async_v.validate(q))
        assert async_errors == sync_errors, f"divergence for query: {q!r}"


def test_async_translator_forwards_stream_to_into_each_llm_call() -> None:
    """The translator must invoke the stream callback for every LLM call:
    once for the initial generate, once per fix iteration."""
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[list[str], _FakeAsyncLLM]:
        fake = _FakeAsyncLLM(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])
        chunks: list[str] = []
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate(
                "SELECT * FROM persons",
                stream_to=chunks.append,
            )
        return chunks, fake

    chunks, fake = asyncio.run(run())
    # Both LLM responses must have streamed in their entirety. The fake LLM
    # emits one chunk per character.
    streamed = "".join(chunks)
    assert "MATCH (p:Person" in streamed
    assert "MATCH (p:Person) RETURN p" in streamed
    assert fake.stream_calls == 2  # initial generate + 1 fix


def test_async_translator_omits_stream_to_by_default() -> None:
    """Without stream_to, the fake LLM records zero stream calls, confirms
    the streaming path is opt-in."""
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> _FakeAsyncLLM:
        fake = _FakeAsyncLLM(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons")
        return fake

    fake = asyncio.run(run())
    assert fake.stream_calls == 0


def test_translator_exposes_last_messages_conversation() -> None:
    """last_messages captures the full system↔model exchange (incl. a fix loop)."""
    fake = _FakeLLM(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])  # fail, then fix
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons")

    roles = [m["role"] for m in translator.last_messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert translator.last_messages[-1]["content"] == "MATCH (p:Person) RETURN p"
    assert all(set(m) == {"role", "content"} for m in translator.last_messages)


def test_async_translator_exposes_last_messages_conversation() -> None:
    """The async translator exposes the same last_messages conversation."""
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> list[dict[str, str]]:
        fake = _FakeAsyncLLM(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons")
        return translator.last_messages

    messages = asyncio.run(run())
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    assert messages[-1]["content"] == "MATCH (p:Person) RETURN p"


def test_async_translator_on_conversation_streams_snapshots() -> None:
    """on_conversation fires growing snapshots, including a partial assistant turn."""
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[list[list[dict[str, str]]], list[dict[str, str]]]:
        fake = _FakeAsyncLLM(["MATCH (p:Person", "MATCH (p:Person) RETURN p"])  # fail, then fix
        snaps: list[list[dict[str, str]]] = []
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=AsyncCypherSyntaxValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons", on_conversation=snaps.append)
        return snaps, translator.last_messages

    snaps, last = asyncio.run(run())
    # The final snapshot is the full conversation and matches last_messages.
    assert snaps[-1] == last
    assert [m["role"] for m in snaps[-1]] == ["system", "user", "assistant", "user", "assistant"]
    # Per-token streaming produces many more snapshots than there are turns.
    assert len(snaps) > len(last)
    # At least one snapshot shows a *partial* assistant turn (mid-stream).
    partials = [s[-1]["content"] for s in snaps if s and s[-1]["role"] == "assistant"]
    assert any(0 < len(p) < len("MATCH (p:Person") for p in partials)


def test_translator_warms_up_validator_before_validation() -> None:
    """A validator exposing warmup() is warmed up exactly once, before the first validate."""
    calls: list[str] = []

    class _WarmupValidator:
        def warmup(self) -> None:
            calls.append("warmup")

        def validate(self, _query: str) -> list[str]:
            calls.append("validate")
            return []

        def close(self) -> None:
            calls.append("close")

    fake = _FakeLLM(["MATCH (p:Person) RETURN p"])
    with SQLTranslator(
        schema_mapping=_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=_WarmupValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons")

    assert calls[0] == "warmup"
    assert calls.count("warmup") == 1
    assert calls.index("warmup") < calls.index("validate")


def test_async_translator_warms_up_validator_before_validation() -> None:
    """The async translator awaits the validator's warmup before the first validate."""
    import asyncio

    from rows2graph import AsyncSQLTranslator

    calls: list[str] = []

    class _AsyncWarmupValidator:
        async def warmup(self) -> None:
            calls.append("warmup")

        async def validate(self, _query: str) -> list[str]:
            calls.append("validate")
            return []

        async def close(self) -> None:
            calls.append("close")

    async def run() -> None:
        fake = _FakeAsyncLLM(["MATCH (p:Person) RETURN p"])
        async with AsyncSQLTranslator(
            schema_mapping=_schema(),
            llm=fake,
            target=CypherTarget(),
            validator=_AsyncWarmupValidator(),
        ) as translator:
            await translator.translate("SELECT * FROM persons")

    asyncio.run(run())
    assert calls[0] == "warmup"
    assert calls.count("warmup") == 1
    assert calls.index("warmup") < calls.index("validate")
