"""Anthropic LLM backend (direct API).

Anthropic's Python SDK exposes Claude through several routing backends
(the direct API at ``api.anthropic.com``, AWS Bedrock, and Google Vertex AI)
all sharing the same ``Messages`` interface. This module wraps the
*direct* variant (``Anthropic``), which authenticates with an API key
issued from the Anthropic console.

Naming note: an earlier revision of this module used ``AnthropicVertex``
(``anthropic[vertex]`` extra) so that authentication went through Google
Application Default Credentials. The thesis project later migrated to a
purchased Anthropic license routed through the direct API; the class name
stayed ``AnthropicLLMClient`` because the LLM is what it always was; only
the routing changed.

The class implements the :class:`sql2graph.llm.LLMClient` Protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from anthropic import Anthropic, AsyncAnthropic
from pydantic import BaseModel, ConfigDict, Field

from sql2graph.llm.usage import ChatReply, TokenUsage

logger = logging.getLogger(__name__)


class AnthropicConfig(BaseModel):
    """Configuration for Claude via the direct Anthropic API.

    ``api_key`` is optional in the YAML: when omitted (or set to ``None``)
    the upstream SDK falls back to the ``ANTHROPIC_API_KEY`` environment
    variable. Keeping the key in the shell environment rather than in the
    YAML is the recommended posture: the YAML file can then be committed
    to version control without leaking secrets.

    The discriminator field ``provider="anthropic"`` is what
    :data:`sql2graph.llm.ModelConfig` uses to dispatch
    :func:`sql2graph.llm.load_model_config` to this class when parsing a
    YAML model config.

    ``max_retries`` is forwarded to the upstream SDK's
    :class:`anthropic.Anthropic` constructor; the SDK does exponential
    backoff with jitter on 408/409/429/5xx and connection errors. The
    default of 3 is one above the SDK's own default of 2, a small
    deliberate bump because losing several iterations of a translation to
    a single transient blip is much more painful than retrying once more.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic"] = "anthropic"
    api_key: str | None = None
    model: str = "claude-opus-4-8"
    temperature: float = 0.1
    max_output_tokens: int = 4096
    max_retries: int = Field(default=3, ge=0)


class AnthropicLLMClient:
    """Synchronous chat client for the direct Anthropic API.

    Anthropic's Messages API distinguishes system turns from user/assistant
    turns by parameter, not by message role. :meth:`chat` therefore pulls
    every ``role == "system"`` message out of the framework's flat message
    list and concatenates them into the request's ``system`` parameter,
    leaving only user/assistant turns in ``messages``.

    The system block is marked ``cache_control: ephemeral`` so Anthropic's
    prompt cache reuses it across the generate-validate-fix iterations of a
    single translation (where the schema mapping + target rules + feature
    rules are byte-identical on every call). The cache silently no-ops below
    the per-model minimum (1024 tokens for most models, 2048 for Haiku) and
    has a 5-minute TTL; both are fine for our use case.

    Token usage from each response is logged at INFO level so verbose
    callers can surface per-call consumption, useful for tracking spend
    against a budget cap. Cache hit/creation counts are logged alongside.
    """

    def __init__(self, config: AnthropicConfig) -> None:
        # `api_key=None` triggers the SDK's ANTHROPIC_API_KEY fallback.
        self._client = Anthropic(api_key=config.api_key, max_retries=config.max_retries)
        self._model = config.model
        self._temperature = config.temperature
        self._max_tokens = config.max_output_tokens

    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> ChatReply:
        kwargs = _build_anthropic_kwargs(
            messages,
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature if temperature is None else temperature,
        )
        response = self._client.messages.create(**kwargs)
        usage = _anthropic_usage(response, self._model)
        return ChatReply(text=_extract_anthropic_text(response), usage=usage)

    def close(self) -> None:
        """No-op: ``Anthropic`` manages its own HTTP session lifecycle."""
        return None


# Current-generation models (Opus 4.7/4.8, Fable/Mythos) removed the sampling
# parameters: sending `temperature`/`top_p`/`top_k` returns HTTP 400. Matched by
# prefix so dated snapshots and aliases are covered.
_NO_SAMPLING_PREFIXES = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
)


def _model_rejects_sampling(model: str) -> bool:
    """True if the model rejects `temperature`/`top_p`/`top_k` (HTTP 400)."""
    return any(model.startswith(prefix) for prefix in _NO_SAMPLING_PREFIXES)


def _build_anthropic_kwargs(
    messages: list[dict[str, Any]],
    *,
    model: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Shared kwargs builder for the sync and async Anthropic clients.

    Separates system turns from user/assistant turns and marks the system
    block ``cache_control: ephemeral`` so the prompt cache is shared across
    the generate-validate-fix iterations of a single translation.
    """
    system_parts: list[str] = []
    chat_messages: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "system":
            system_parts.append(message["content"])
        else:
            chat_messages.append({"role": message["role"], "content": message["content"]})

    system_text = "\n\n".join(system_parts) if system_parts else None

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": chat_messages,
    }
    # Only send `temperature` to models that still accept it; current-generation
    # models 400 on any sampling parameter (the escalation path also routes a
    # higher temperature through here, so this guard covers both call sites).
    if not _model_rejects_sampling(model):
        kwargs["temperature"] = temperature
    if system_text is not None:
        kwargs["system"] = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    return kwargs


def _anthropic_usage(response: Any, model: str) -> TokenUsage:
    """Extract token usage from an Anthropic response, logging it in passing.

    Shared by the sync and async clients. Returns a zeroed :class:`TokenUsage`
    when the response carries no usage block. ``input_tokens`` is the *uncached*
    prompt portion; the API reports cache reads/writes separately, which map
    onto the ``cache_read_tokens`` / ``cache_creation_tokens`` fields.
    """
    if response.usage is None:
        return TokenUsage()
    usage = TokenUsage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
    )
    logger.info(
        "Anthropic call: input=%d output=%d cache_read=%d cache_write=%d tokens (model=%s)",
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
        model,
    )
    return usage


def _extract_anthropic_text(response: Any) -> str:
    """Shared response-extraction helper for sync and async clients."""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text or ""
    return ""


class AsyncAnthropicLLMClient:
    """Asynchronous chat client for the direct Anthropic API.

    Mirrors :class:`AnthropicLLMClient` shape for shape; the only
    differences are :meth:`chat` is ``async def`` and uses
    :class:`anthropic.AsyncAnthropic`, and :meth:`close` actually does work
    (the async SDK exposes ``aclose()`` on its underlying httpx client).

    Behavior (prompt caching, retry/backoff via the SDK, usage logging)
    is identical to the sync client. Same :class:`AnthropicConfig`; both
    clients can be constructed from the same loaded config.
    """

    def __init__(self, config: AnthropicConfig) -> None:
        self._client = AsyncAnthropic(api_key=config.api_key, max_retries=config.max_retries)
        self._model = config.model
        self._temperature = config.temperature
        self._max_tokens = config.max_output_tokens

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream_to: Callable[[str], None] | None = None,
        temperature: float | None = None,
    ) -> ChatReply:
        kwargs = _build_anthropic_kwargs(
            messages,
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature if temperature is None else temperature,
        )

        if stream_to is None:
            response = await self._client.messages.create(**kwargs)
            usage = _anthropic_usage(response, self._model)
            return ChatReply(text=_extract_anthropic_text(response), usage=usage)

        # Streaming path: yields text deltas via ``stream_to`` while still
        # assembling the full text for the return value. ``get_final_message``
        # is what exposes the usage stats once the stream completes.
        text_buf: list[str] = []
        async with self._client.messages.stream(**kwargs) as stream:
            async for delta in stream.text_stream:
                text_buf.append(delta)
                stream_to(delta)
            final = await stream.get_final_message()
        usage = _anthropic_usage(final, self._model)
        return ChatReply(text="".join(text_buf), usage=usage)

    async def close(self) -> None:
        """Release the underlying httpx connection pool."""
        await self._client.close()
