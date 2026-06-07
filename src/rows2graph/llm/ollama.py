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

from typing import Any, Literal

from ollama import Client
from pydantic import BaseModel, ConfigDict

# Default Ollama HTTP endpoint (matches the upstream client default; the
# framework restates it so error messages reference our default, not Ollama's).
_DEFAULT_HOST = "http://localhost:11434"


class OllamaConfig(BaseModel):
    """Configuration for the Ollama backend.

    The discriminator field ``provider="ollama"`` is what
    :data:`rows2graph.llm.ModelConfig` uses to dispatch
    :func:`rows2graph.llm.load_model_config` to this class when parsing a
    YAML model config.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama"] = "ollama"
    model: str = "llama3.2"
    host: str = _DEFAULT_HOST
    temperature: float = 0.1
    num_ctx: int = 4096


class OllamaLLMClient:
    """Synchronous Ollama chat client.

    The framework uses synchronous calls because the generate–validate–fix
    loop is inherently sequential — there is no benefit to async dispatch
    when each iteration depends on the previous one's result.
    """

    def __init__(self, config: OllamaConfig) -> None:
        self._client = Client(host=config.host)
        self._model = config.model
        self._options: dict[str, Any] = {
            "temperature": config.temperature,
            "num_ctx": config.num_ctx,
        }

    def chat(self, messages: list[dict[str, Any]]) -> str:
        response = self._client.chat(
            model=self._model,
            messages=messages,
            options=self._options,
        )
        return response.message.content or ""

    def close(self) -> None:
        """No-op: ``ollama.Client`` does not expose a connection-pool close."""
        return None
