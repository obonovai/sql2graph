"""Ollama LLM backend.

`Ollama <https://ollama.com>`_ is a local-first model server. We wrap its
official Python client with the minimal surface the framework needs: a typed
configuration model, a constructor that opens an HTTP client, and a
:meth:`chat` method that turns the framework-internal message list into an
Ollama request and returns the response text.

The class implements the
:class:`rows2graph.llm.LLMClient` Protocol structurally; there is no
inheritance from a shared base, so a future third-party Ollama-flavoured
client can satisfy the Protocol without importing this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

from ollama import AsyncClient, Client, RequestError, ResponseError
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Default Ollama HTTP endpoint (matches the upstream client default; the
# framework restates it so error messages reference our default, not Ollama's).
_DEFAULT_HOST = "http://localhost:11434"


class OllamaConfig(BaseModel):
    """Configuration for the Ollama backend.

    The discriminator field ``provider="ollama"`` is what
    :data:`rows2graph.llm.ModelConfig` uses to dispatch
    :func:`rows2graph.llm.load_model_config` to this class when parsing a
    YAML model config.

    ``max_retries`` controls how many additional attempts are made when
    the upstream Ollama call raises a retryable error (connection refused,
    timeout, or a 5xx :class:`ollama.ResponseError`). 4xx responses are
    not retried — they indicate a client-side mistake the loop can't fix.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama"] = "ollama"
    model: str = "llama3.2"
    host: str = _DEFAULT_HOST
    temperature: float = 0.1
    num_ctx: int = 4096
    max_retries: int = Field(default=3, ge=0)


class OllamaLLMClient:
    """Synchronous Ollama chat client.

    The framework uses synchronous calls because the generate–validate–fix
    loop is inherently sequential — there is no benefit to async dispatch
    when each iteration depends on the previous one's result.

    Retries are handwritten because the ``ollama`` Python SDK does not ship
    its own retry layer. Backoff is exponential with a 1-second base
    (0s, 1s, 2s, 4s, ...) and no jitter — collisions between concurrent
    Ollama clients are not a concern for a local-first server.
    """

    def __init__(self, config: OllamaConfig) -> None:
        self._client = Client(host=config.host)
        self._model = config.model
        self._max_retries = config.max_retries
        self._options: dict[str, Any] = {
            "temperature": config.temperature,
            "num_ctx": config.num_ctx,
        }

    def chat(self, messages: list[dict[str, Any]]) -> str:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.chat(
                    model=self._model,
                    messages=messages,
                    options=self._options,
                )
                return response.message.content or ""
            except ResponseError as exc:
                # Don't retry 4xx — they indicate a malformed request that
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
                    "Ollama call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    self._max_retries + 1,
                    last_exc,
                    delay,
                )
                time.sleep(delay)

        # Exhausted retries — re-raise the last exception so the caller sees
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
    loop. Same :class:`OllamaConfig` — both clients can be built from the
    same loaded config.
    """

    def __init__(self, config: OllamaConfig) -> None:
        self._client = AsyncClient(host=config.host)
        self._model = config.model
        self._max_retries = config.max_retries
        self._options: dict[str, Any] = {
            "temperature": config.temperature,
            "num_ctx": config.num_ctx,
        }

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.chat(
                    model=self._model,
                    messages=messages,
                    options=self._options,
                )
                return response.message.content or ""
            except ResponseError as exc:
                if exc.status_code < 500:
                    raise
                last_exc = exc
            except RequestError as exc:
                last_exc = exc

            if attempt < self._max_retries:
                delay = float(1 << attempt)
                logger.warning(
                    "Ollama call failed (attempt %d/%d): %s — retrying in %.1fs",
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
