"""Ollama client tests: retry/backoff on connection and 5xx errors, no retry on
4xx, retry exhaustion, and config validation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from rows2graph import OllamaConfig


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
