"""Anthropic LLM backend (direct API).

Anthropic's Python SDK exposes Claude through several routing backends —
the direct API at ``api.anthropic.com``, AWS Bedrock, and Google Vertex AI
— all sharing the same ``Messages`` interface. This module wraps the
*direct* variant (``Anthropic``), which authenticates with an API key
issued from the Anthropic console.

Naming note: an earlier revision of this module used ``AnthropicVertex``
(``anthropic[vertex]`` extra) so that authentication went through Google
Application Default Credentials. The thesis project later migrated to a
purchased Anthropic license routed through the direct API; the class name
stayed ``AnthropicLLMClient`` because the LLM is what it always was — only
the routing changed.

The class implements the :class:`rows2graph.llm.LLMClient` Protocol.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class AnthropicConfig(BaseModel):
    """Configuration for Claude via the direct Anthropic API.

    ``api_key`` is optional in the YAML: when omitted (or set to ``None``)
    the upstream SDK falls back to the ``ANTHROPIC_API_KEY`` environment
    variable. Keeping the key in the shell environment rather than in the
    YAML is the recommended posture — the YAML file can then be committed
    to version control without leaking secrets.

    The discriminator field ``provider="anthropic"`` is what
    :data:`rows2graph.llm.ModelConfig` uses to dispatch
    :func:`rows2graph.llm.load_model_config` to this class when parsing a
    YAML model config.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic"] = "anthropic"
    api_key: str | None = None
    model: str = "claude-opus-4-7"
    temperature: float = 0.1
    max_output_tokens: int = 4096


class AnthropicLLMClient:
    """Synchronous chat client for the direct Anthropic API.

    Anthropic's Messages API distinguishes system turns from user/assistant
    turns by parameter, not by message role. :meth:`chat` therefore pulls
    every ``role == "system"`` message out of the framework's flat message
    list and concatenates them into the request's ``system`` parameter,
    leaving only user/assistant turns in ``messages``.

    Token usage from each response is logged at INFO level so the demo's
    ``-v`` flag surfaces per-call consumption — useful for tracking spend
    against a budget cap.
    """

    def __init__(self, config: AnthropicConfig) -> None:
        # `api_key=None` triggers the SDK's ANTHROPIC_API_KEY fallback.
        self._client = Anthropic(api_key=config.api_key)
        self._model = config.model
        self._temperature = config.temperature
        self._max_tokens = config.max_output_tokens

    def chat(self, messages: list[dict[str, Any]]) -> str:
        system_parts: list[str] = []
        chat_messages: list[dict[str, Any]] = []
        for message in messages:
            if message["role"] == "system":
                system_parts.append(message["content"])
            else:
                chat_messages.append({"role": message["role"], "content": message["content"]})

        system = "\n\n".join(system_parts) if system_parts else None

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "messages": chat_messages,
        }
        if system is not None:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)

        if response.usage is not None:
            logger.info(
                "Anthropic call: input=%d output=%d tokens (model=%s)",
                response.usage.input_tokens,
                response.usage.output_tokens,
                self._model,
            )

        for block in response.content:
            if getattr(block, "type", None) == "text":
                return block.text or ""
        return ""

    def close(self) -> None:
        """No-op: ``Anthropic`` manages its own HTTP session lifecycle."""
        return None
