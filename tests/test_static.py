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
)
from rows2graph._env import interpolate_env
from rows2graph.prompts import build_fix_prompt, build_generate_prompt, build_system_prompt
from rows2graph.sql_features import ALL_FEATURES, SqlFeature, detect_features

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
        # source_tabel is a typo for source_table — strict mode catches it
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
    p.write_text('type: "arangodb"\npassword: "${TEST_PW}"\ngraph_name: "g1"\n')
    config = load_server_config(p)
    assert isinstance(config, ArangoDBConfig)
    assert config.graph_name == "g1"


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


def test_make_target_aql_carries_graph_name() -> None:
    t = make_target("aql", graph_name="myGraph")
    assert isinstance(t, AqlTarget)
    assert t.name == "aql"
    assert "myGraph" in t.system_prompt_section(frozenset())


def test_make_target_aql_without_graph_name_falls_back() -> None:
    t = make_target("aql")
    assert "configured named graph" in t.system_prompt_section(frozenset())


def test_make_target_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown target language"):
        make_target("gremlin")


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


def test_make_validator_cypher_server_requires_neo4j_config() -> None:
    with pytest.raises(ValueError, match="requires a server_config"):
        make_validator("cypher", "server")


def test_make_validator_cypher_server_rejects_arangodb_config() -> None:
    arango_config = ArangoDBConfig(password="p", graph_name="g")
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


def test_make_validator_aql_server_constructs_with_arangodb() -> None:
    with patch("rows2graph.validators.aql.server.ArangoClient") as mock_client:
        v = make_validator(
            "aql",
            "server",
            server_config=ArangoDBConfig(password="secret", graph_name="g"),
        )
        from rows2graph.validators.aql.server import AqlServerValidator

        assert isinstance(v, AqlServerValidator)
        mock_client.assert_called_once()


def test_make_validator_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="Unknown validation mode"):
        make_validator("cypher", "telepathy")


# ---------------------------------------------------------------------------
# make_llm factory (constructors are mocked — we just verify dispatch)
# ---------------------------------------------------------------------------


def test_make_llm_ollama_dispatch() -> None:
    with patch("rows2graph.llm.ollama.Client") as mock_client:
        llm = make_llm(OllamaConfig(model="m", host="http://x:1"))
        from rows2graph.llm.ollama import OllamaLLMClient

        assert isinstance(llm, OllamaLLMClient)
        mock_client.assert_called_once_with(host="http://x:1")


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
        mock_client_cls.return_value.chat.side_effect = [
            RequestError("connection refused"),
            RequestError("connection refused"),
            mock_response,
        ]
        client = OllamaLLMClient(OllamaConfig(model="m", host="http://x:1", max_retries=3))
        assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
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
        mock_client_cls.return_value.chat.side_effect = [
            ResponseError("server overloaded", 503),
            mock_response,
        ]
        client = OllamaLLMClient(OllamaConfig(model="m", host="http://x:1", max_retries=3))
        assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
        assert mock_client_cls.return_value.chat.call_count == 2


def test_ollama_chat_does_not_retry_on_4xx_response_error() -> None:
    """4xx errors are client-side bugs — retrying just wastes time."""
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
    kwarg is omitted entirely — adding an empty cacheable block would be
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
    assert AqlTarget(graph_name="g").extract_query(text) == "FOR p IN persons RETURN p"


def test_extract_aql_from_keyword() -> None:
    text = "OK:\nFOR p IN persons FILTER p.age > 18 RETURN p"
    assert AqlTarget(graph_name="g").extract_query(text) == "FOR p IN persons FILTER p.age > 18 RETURN p"


def test_extract_aql_fallback_returns_stripped() -> None:
    assert AqlTarget(graph_name="g").extract_query("  nothing here  ") == "nothing here"


# ---------------------------------------------------------------------------
# Syntax validators
# ---------------------------------------------------------------------------


def test_cypher_syntax_passes_valid_query() -> None:
    assert CypherSyntaxValidator().validate("MATCH (n) RETURN n") == []


def test_cypher_syntax_flags_bad_start() -> None:
    errors = CypherSyntaxValidator().validate("WHERE x = 1")
    assert any("valid Cypher keyword" in e for e in errors)


def test_cypher_syntax_flags_match_without_return() -> None:
    errors = CypherSyntaxValidator().validate("MATCH (n) WHERE n.age > 18")
    assert any("RETURN" in e for e in errors)


def test_cypher_syntax_flags_empty_query() -> None:
    assert CypherSyntaxValidator().validate("   ") == ["Query is empty"]


def test_aql_syntax_passes_valid_query() -> None:
    assert AqlSyntaxValidator().validate("FOR p IN persons FILTER p.age > 18 RETURN p") == []


def test_aql_syntax_flags_bad_start() -> None:
    errors = AqlSyntaxValidator().validate("SELECT * FROM persons")
    assert any("valid AQL keyword" in e for e in errors)


def test_aql_syntax_flags_missing_return() -> None:
    errors = AqlSyntaxValidator().validate("FOR p IN persons FILTER p.age > 18")
    assert any("RETURN" in e for e in errors)


def test_aql_syntax_flags_unbalanced_brackets() -> None:
    errors = AqlSyntaxValidator().validate("FOR p IN persons RETURN [p")
    assert any("Unbalanced square brackets" in e for e in errors)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_build_system_prompt_cypher() -> None:
    prompt = build_system_prompt(_schema(), CypherTarget(), frozenset())
    assert "cypher" in prompt
    assert "MATCH" in prompt
    assert "Person" in prompt  # schema is embedded


def test_build_system_prompt_aql_includes_graph_name() -> None:
    prompt = build_system_prompt(_schema(), AqlTarget(graph_name="mygraph"), frozenset())
    assert "aql" in prompt
    assert "FOR" in prompt
    assert "mygraph" in prompt
    assert "FILTER" in prompt
    assert "Person" in prompt


def test_build_system_prompt_aql_without_graph_name_falls_back() -> None:
    prompt = build_system_prompt(_schema(), AqlTarget(graph_name=None), frozenset())
    assert "configured named graph" in prompt


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


# ---------------------------------------------------------------------------
# SQLTranslator end-to-end with fake LLM
# ---------------------------------------------------------------------------


class _FakeLLM:
    """In-process double for the LLMClient Protocol."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.closed = False

    def chat(self, messages: list[dict[str, Any]]) -> str:
        self.calls.append(list(messages))
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


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


def test_translator_runs_fix_loop_on_validation_failure() -> None:
    # First response: malformed. Second response: valid.
    fake = _FakeLLM(
        [
            "MATCH (p:Person)",  # missing RETURN
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


def test_translator_hits_max_iterations() -> None:
    fake = _FakeLLM(["MATCH (p:Person)"] * 3)  # always invalid
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
    assert result.generated_query == "MATCH (p:Person)"


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
            "MATCH (p:Person)",  # missing RETURN
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

    fake = _FakeLLM(["MATCH (p:Person)"] * 3)  # always invalid
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
    assert SqlFeature.AGGREGATION in detect_features(
        "SELECT k, COUNT(*) c FROM t GROUP BY k HAVING COUNT(*) > 5"
    )


def test_detect_features_order_limit() -> None:
    feats = detect_features("SELECT a FROM t ORDER BY a LIMIT 10")
    assert SqlFeature.ORDER_LIMIT in feats


def test_detect_features_cte() -> None:
    feats = detect_features("WITH c AS (SELECT * FROM t) SELECT * FROM c")
    assert SqlFeature.CTE in feats
    # A bare CTE without any nested SELECT inside an expression must NOT
    # light up SUBQUERY — distinguishing the two clusters is the whole point.
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
    feats = detect_features(
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.x = t.x)"
    )
    assert SqlFeature.SUBQUERY in feats


def test_detect_features_distinct() -> None:
    assert SqlFeature.DISTINCT in detect_features("SELECT DISTINCT a FROM t")


def test_detect_features_parse_failure_returns_all() -> None:
    # Garbage SQL must yield the full feature set so no rule chunk is
    # silently stripped from the prompt.
    assert detect_features("SELECT ;;; FROM") == ALL_FEATURES


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


def test_aql_prompt_includes_collect_only_when_aggregation_detected() -> None:
    with_agg = build_system_prompt(
        _schema(), AqlTarget(graph_name="g"), frozenset({SqlFeature.AGGREGATION})
    )
    without_agg = build_system_prompt(
        _schema(), AqlTarget(graph_name="g"), frozenset()
    )
    assert "COLLECT" in with_agg
    assert "COLLECT" not in without_agg


def test_generic_join_rule_is_feature_gated() -> None:
    with_join = build_system_prompt(_schema(), CypherTarget(), frozenset({SqlFeature.JOIN}))
    without_join = build_system_prompt(_schema(), CypherTarget(), frozenset())
    assert "Map SQL JOINs" in with_join
    assert "Map SQL JOINs" not in without_join


def test_translator_omits_unused_rules_from_system_message() -> None:
    # SQL has only LIKE — the system prompt should carry the LIKE chunk
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
# AsyncSQLTranslator end-to-end with fake async LLM
# ---------------------------------------------------------------------------


class _FakeAsyncLLM:
    """In-process double for the AsyncLLMClient Protocol.

    When ``stream_to`` is supplied, emits the response character-by-character
    through the callback before returning the full text — enough to exercise
    the streaming plumbing without needing a real LLM.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.stream_calls: int = 0
        self.closed = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream_to: Any = None,
    ) -> str:
        self.calls.append(list(messages))
        response = self._responses.pop(0)
        if stream_to is not None:
            self.stream_calls += 1
            for char in response:
                stream_to(char)
        return response

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


def test_async_translator_runs_fix_loop_on_validation_failure() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = _FakeAsyncLLM(["MATCH (p:Person)", "MATCH (p:Person) RETURN p"])
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


def test_async_translator_hits_max_iterations() -> None:
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> TranslationResult:
        fake = _FakeAsyncLLM(["MATCH (p:Person)"] * 3)
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


def test_async_translator_emits_same_event_sequence_as_sync() -> None:
    """The async translator emits the same event sequence as the sync one
    for an identical input — events are part of the cross-translator contract."""
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
        fake = _FakeAsyncLLM(["MATCH (p:Person)", "MATCH (p:Person) RETURN p"])
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
        "MATCH (p:Person)",  # missing RETURN
        "",  # empty
        "MATCH (p:Person RETURN p",  # unbalanced paren
    ]
    for q in cases:
        sync_errors = sync_v.validate(q)
        async_errors = asyncio.run(async_v.validate(q))
        assert async_errors == sync_errors, f"divergence for query: {q!r}"


def test_async_translator_forwards_stream_to_into_each_llm_call() -> None:
    """The translator must invoke the stream callback for every LLM call —
    once for the initial generate, once per fix iteration."""
    import asyncio

    from rows2graph import AsyncSQLTranslator
    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async def run() -> tuple[list[str], _FakeAsyncLLM]:
        fake = _FakeAsyncLLM(["MATCH (p:Person)", "MATCH (p:Person) RETURN p"])
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
    assert "MATCH (p:Person)" in streamed
    assert "MATCH (p:Person) RETURN p" in streamed
    assert fake.stream_calls == 2  # initial generate + 1 fix


def test_async_translator_omits_stream_to_by_default() -> None:
    """Without stream_to, the fake LLM records zero stream calls — confirms
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
