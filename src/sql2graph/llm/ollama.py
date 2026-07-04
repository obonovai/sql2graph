"""Ollama LLM backend.

`Ollama <https://ollama.com>`_ is a local-first model server. We wrap its
official Python client with the minimal surface the framework needs: a typed
configuration model, a constructor that opens an HTTP client, and a
:meth:`chat` method that turns the framework-internal message list into an
Ollama request and returns the response text.

The class implements the
:class:`sql2graph.llm.LLMClient` Protocol structurally; there is no
inheritance from a shared base, so a future third-party Ollama-flavoured
client can satisfy the Protocol without importing this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Literal

from ollama import AsyncClient, Client, RequestError, ResponseError
from pydantic import BaseModel, ConfigDict, Field

from sql2graph.llm.usage import ChatReply, TokenUsage

logger = logging.getLogger(__name__)


def _ollama_usage(response: Any) -> TokenUsage:
    """Extract token usage from an Ollama chat response.

    Ollama reports ``prompt_eval_count`` (prompt tokens) and ``eval_count``
    (generated tokens). On a streamed response only the final ``done`` chunk
    carries them, so absent values fall back to 0 (a ``None`` response, an
    empty stream, yields a zeroed :class:`TokenUsage`). Ollama has no prompt
    cache, so the cache fields stay 0.
    """
    return TokenUsage(
        input_tokens=getattr(response, "prompt_eval_count", 0) or 0,
        output_tokens=getattr(response, "eval_count", 0) or 0,
    )


class OllamaConfig(BaseModel):
    """Configuration for the Ollama backend.

    ``host`` is optional in the YAML: when omitted (or set to ``None``) the
    upstream SDK falls back to the ``OLLAMA_HOST`` environment variable, and
    to its own built-in default endpoint when that too is unset. A ``host``
    set in the YAML takes precedence over the environment variable, so the
    same config file can target a local or a remote Ollama server without
    edits.

    The discriminator field ``provider="ollama"`` is what
    :data:`sql2graph.llm.ModelConfig` uses to dispatch
    :func:`sql2graph.llm.load_model_config` to this class when parsing a
    YAML model config.

    ``max_retries`` controls how many additional attempts are made when
    the upstream Ollama call raises a retryable error (connection refused,
    timeout, or a 5xx :class:`ollama.ResponseError`). 4xx responses are
    not retried; they indicate a client-side mistake the loop can't fix.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama"] = "ollama"
    model: str = "llama3.2"
    host: str | None = None
    temperature: float = 0.1
    num_ctx: int = 4096
    # Optional anti-repetition knob. Left unset by default so the framework passes
    # only what the YAML opts into and Ollama's own default governs the rest;
    # ``repeat_penalty`` > 1.0 counters the degenerate-repetition failure mode the
    # fix loop can hit.
    repeat_penalty: float | None = None
    max_retries: int = Field(default=3, ge=0)


def _base_options(config: OllamaConfig) -> dict[str, Any]:
    """Assemble the Ollama ``options`` dict, omitting unset optional knobs."""
    options: dict[str, Any] = {
        "temperature": config.temperature,
        "num_ctx": config.num_ctx,
    }
    if config.repeat_penalty is not None:
        options["repeat_penalty"] = config.repeat_penalty
    return options


def _options_for_call(base: dict[str, Any], temperature: float | None) -> dict[str, Any]:
    """Per-call options: the base dict, with ``temperature`` overridden if given."""
    if temperature is None:
        return base
    return {**base, "temperature": temperature}


class OllamaLLMClient:
    """Synchronous Ollama chat client.

    The framework uses synchronous calls because the generate-validate-fix
    loop is inherently sequential; there is no benefit to async dispatch
    when each iteration depends on the previous one's result.

    Retries are handwritten because the ``ollama`` Python SDK does not ship
    its own retry layer. Backoff is exponential with a 1-second base
    (0s, 1s, 2s, 4s, ...) and no jitter; collisions between concurrent
    Ollama clients are not a concern for a local-first server.
    """

    def __init__(self, config: OllamaConfig) -> None:
        self._client = Client(host=config.host)
        self._model = config.model
        self._max_retries = config.max_retries
        self._options = _base_options(config)

    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> ChatReply:
        options = _options_for_call(self._options, temperature)
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.chat(
                    model=self._model,
                    messages=messages,
                    options=options,
                )
                return ChatReply(text=response.message.content or "", usage=_ollama_usage(response))
            except ResponseError as exc:
                # Don't retry 4xx; they indicate a malformed request that
                # another attempt won't fix (unknown model, bad params, ...).
                if exc.status_code < 500:
                    raise
                last_exc = exc
            except RequestError as exc:
                # Network-layer failures: connection refused, timeout, ...
                last_exc = exc

            if attempt < self._max_retries:
                delay = float(1 << attempt)  # 1s, 2s, 4s, ...
                logger.warning(
                    "Ollama call failed (attempt %d/%d): %s, retrying in %.1fs",
                    attempt + 1,
                    self._max_retries + 1,
                    last_exc,
                    delay,
                )
                time.sleep(delay)

        # Exhausted retries: re-raise the last exception so the caller sees
        # the actual cause rather than a wrapped one. last_exc is non-None
        # whenever we reach here.
        assert last_exc is not None
        raise last_exc

    def close(self) -> None:
        """No-op: ``ollama.Client`` does not expose a connection-pool close."""
        return None


class AsyncOllamaLLMClient:
    """Asynchronous Ollama chat client.

    Mirrors :class:`OllamaLLMClient`; the only differences are :meth:`chat`
    is ``async def`` and uses :class:`ollama.AsyncClient`, and the retry
    backoff sleeps via :func:`asyncio.sleep` rather than blocking the event
    loop. Same :class:`OllamaConfig`; both clients can be built from the
    same loaded config.
    """

    def __init__(self, config: OllamaConfig) -> None:
        self._client = AsyncClient(host=config.host)
        self._model = config.model
        self._max_retries = config.max_retries
        self._options = _base_options(config)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream_to: Callable[[str], None] | None = None,
        temperature: float | None = None,
    ) -> ChatReply:
        options = _options_for_call(self._options, temperature)
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            # Stream only on the first attempt. On retries, fall back to
            # non-streaming so the caller's UI buffer (if any) isn't
            # polluted by deltas from a discarded prior attempt; the
            # retry is meant to be invisible from the caller's perspective.
            should_stream = stream_to is not None and attempt == 0
            try:
                if not should_stream:
                    response = await self._client.chat(
                        model=self._model,
                        messages=messages,
                        options=options,
                    )
                    return ChatReply(text=response.message.content or "", usage=_ollama_usage(response))

                # Streaming path. The token counts live on the final ``done``
                # chunk, so keep the last one seen and read usage off it.
                text_buf: list[str] = []
                last_chunk: Any = None
                stream_iter = await self._client.chat(
                    model=self._model,
                    messages=messages,
                    options=options,
                    stream=True,
                )
                assert stream_to is not None  # narrows the type for mypy
                async for chunk in stream_iter:
                    last_chunk = chunk
                    delta = chunk.message.content or ""
                    if delta:
                        text_buf.append(delta)
                        stream_to(delta)
                return ChatReply(text="".join(text_buf), usage=_ollama_usage(last_chunk))
            except ResponseError as exc:
                if exc.status_code < 500:
                    raise
                last_exc = exc
            except RequestError as exc:
                last_exc = exc

            if attempt < self._max_retries:
                delay = float(1 << attempt)
                logger.warning(
                    "Ollama call failed (attempt %d/%d): %s, retrying in %.1fs",
                    attempt + 1,
                    self._max_retries + 1,
                    last_exc,
                    delay,
                )
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc

    async def close(self) -> None:
        """No-op: ``ollama.AsyncClient`` does not expose a connection-pool close."""
        return None
