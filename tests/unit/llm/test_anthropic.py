"""Anthropic client tests: config validation, prompt-cache marking of the system
block, and system-handling when no system messages are present."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from sql2graph import AnthropicConfig


def test_anthropic_config_rejects_negative_max_retries() -> None:
    with pytest.raises(ValidationError):
        AnthropicConfig(max_retries=-1)


def test_anthropic_chat_marks_system_prompt_cacheable() -> None:
    """System block must carry cache_control=ephemeral so multi-iteration
    translations reuse the schema+rules prompt instead of re-sending it.
    """
    from sql2graph.llm.anthropic import AnthropicLLMClient

    with patch("sql2graph.llm.anthropic.Anthropic") as mock_anthropic:
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
    from sql2graph.llm.anthropic import AnthropicLLMClient

    with patch("sql2graph.llm.anthropic.Anthropic") as mock_anthropic:
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="ok")]
        mock_response.usage = None
        mock_anthropic.return_value.messages.create.return_value = mock_response

        client = AnthropicLLMClient(AnthropicConfig(api_key="sk-ant-test", model="claude-x"))
        client.chat([{"role": "user", "content": "hello"}])

        call_kwargs = mock_anthropic.return_value.messages.create.call_args.kwargs
        assert "system" not in call_kwargs
