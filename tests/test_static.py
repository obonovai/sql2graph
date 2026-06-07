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
    assert "myGraph" in t.system_prompt_section()


def test_make_target_aql_without_graph_name_falls_back() -> None:
    t = make_target("aql")
    assert "configured named graph" in t.system_prompt_section()


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


def test_make_llm_anthropic_dispatch() -> None:
    with patch("rows2graph.llm.anthropic.Anthropic") as mock_anthropic:
        llm = make_llm(AnthropicConfig(api_key="sk-ant-test", model="claude-x"))
        from rows2graph.llm.anthropic import AnthropicLLMClient

        assert isinstance(llm, AnthropicLLMClient)
        mock_anthropic.assert_called_once_with(api_key="sk-ant-test")


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
    prompt = build_system_prompt(_schema(), CypherTarget())
    assert "cypher" in prompt
    assert "MATCH" in prompt
    assert "Person" in prompt  # schema is embedded


def test_build_system_prompt_aql_includes_graph_name() -> None:
    prompt = build_system_prompt(_schema(), AqlTarget(graph_name="mygraph"))
    assert "aql" in prompt
    assert "FOR" in prompt
    assert "mygraph" in prompt
    assert "FILTER" in prompt
    assert "Person" in prompt


def test_build_system_prompt_aql_without_graph_name_falls_back() -> None:
    prompt = build_system_prompt(_schema(), AqlTarget(graph_name=None))
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
