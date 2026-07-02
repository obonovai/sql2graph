"""Config loading tests: env interpolation and model/server config dispatch."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from rows2graph import (
    AnthropicConfig,
    ArangoDBConfig,
    GremlinConfig,
    Neo4jConfig,
    OllamaConfig,
    load_model_config,
    load_server_config,
)
from rows2graph._env import interpolate_env


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
