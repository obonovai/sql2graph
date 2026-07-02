"""LLM factory dispatch tests: ``make_llm`` / ``make_async_llm`` route configs
to the right client (constructors are mocked; we just verify dispatch)."""

from __future__ import annotations

from unittest.mock import patch

from rows2graph import AnthropicConfig, OllamaConfig, make_llm


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
