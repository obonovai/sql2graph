"""LLM backends and their typed configuration.

This package abstracts the LLM provider behind a structural :class:`Protocol`,
:class:`LLMClient`. Two concrete implementations ship:

* :class:`rows2graph.llm.ollama.OllamaLLMClient` — local-first via the
  Ollama HTTP API.
* :class:`rows2graph.llm.anthropic.AnthropicLLMClient` — Claude on Google
  Vertex AI.

Each backend ships its own Pydantic configuration class
(:class:`OllamaConfig`, :class:`AnthropicConfig`) carrying a literal
``provider`` discriminator field. The discriminator lets us assemble a
Pydantic-validated *tagged union* :data:`ModelConfig`: a YAML file with
``provider: "ollama"`` deserialises to :class:`OllamaConfig`, one with
``provider: "anthropic"`` to :class:`AnthropicConfig`. The dispatch into the
correct constructor (``make_llm``) is then a single ``isinstance`` check —
the same factory-by-tag pattern as the original design, but with the tag
validated by Pydantic at load time rather than carried in a separate field
of a larger config blob.

Why ``Protocol`` rather than an abstract base class?

* **Zero coupling.** A third-party backend can satisfy the Protocol without
  importing anything from this module.
* **Mypy-verified structural typing.** ``mypy --strict`` checks that any
  instance returned by :func:`make_llm` conforms to the Protocol shape.
* **No diamond-inheritance risk** if a future implementation composes with
  another base class (caching adapter, metrics decorator, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Protocol

import yaml
from pydantic import Field, TypeAdapter

from rows2graph._env import interpolate_env
from rows2graph.llm.anthropic import AnthropicConfig, AnthropicLLMClient, AsyncAnthropicLLMClient
from rows2graph.llm.ollama import AsyncOllamaLLMClient, OllamaConfig, OllamaLLMClient


class LLMClient(Protocol):
    """Structural type for any LLM backend the translator can use.

    The translator only invokes :meth:`chat` (returning the assistant turn's
    text) and :meth:`close` (releasing whatever connection-pool resources
    the backend may hold). Anything else is implementation-private.
    """

    def chat(self, messages: list[dict[str, Any]]) -> str: ...

    def close(self) -> None: ...


class AsyncLLMClient(Protocol):
    """Structural type for the async LLM backends.

    The async translator
    (:class:`rows2graph.async_translator.AsyncSQLTranslator`) consumes this
    Protocol. Implementations must define both :meth:`chat` and
    :meth:`close` as ``async``. Same shape as :class:`LLMClient` otherwise —
    one chat method that takes a flat message list and returns the
    assistant turn's text.
    """

    async def chat(self, messages: list[dict[str, Any]]) -> str: ...

    async def close(self) -> None: ...


ModelConfig = Annotated[OllamaConfig | AnthropicConfig, Field(discriminator="provider")]
"""Tagged union over every supported model config.

Pydantic uses the literal ``provider`` field on each member as the
discriminator. :func:`load_model_config` returns the precise subtype, so
downstream code can ``isinstance``-dispatch without re-parsing the YAML.
"""

_MODEL_CONFIG_ADAPTER: TypeAdapter[OllamaConfig | AnthropicConfig] = TypeAdapter(ModelConfig)


def load_model_config(path: Path | str) -> OllamaConfig | AnthropicConfig:
    """Load and validate a model config YAML file.

    Environment-variable references (``${VAR}``) are interpolated before
    Pydantic validation; an undeclared variable raises :class:`KeyError`.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)
    interpolated = interpolate_env(raw)
    return _MODEL_CONFIG_ADAPTER.validate_python(interpolated)


def make_llm(config: OllamaConfig | AnthropicConfig) -> LLMClient:
    """Construct the appropriate :class:`LLMClient` for a loaded model config."""
    if isinstance(config, OllamaConfig):
        return OllamaLLMClient(config)
    if isinstance(config, AnthropicConfig):
        return AnthropicLLMClient(config)
    # Defensive: the discriminator should prevent reaching here.
    raise TypeError(f"Unknown model config type: {type(config).__name__}")


def make_async_llm(config: OllamaConfig | AnthropicConfig) -> AsyncLLMClient:
    """Construct the appropriate :class:`AsyncLLMClient` for a loaded model config.

    Parallels :func:`make_llm` — same config types, async client returned.
    """
    if isinstance(config, OllamaConfig):
        return AsyncOllamaLLMClient(config)
    if isinstance(config, AnthropicConfig):
        return AsyncAnthropicLLMClient(config)
    raise TypeError(f"Unknown model config type: {type(config).__name__}")


__all__ = [
    "AnthropicConfig",
    "AnthropicLLMClient",
    "AsyncAnthropicLLMClient",
    "AsyncLLMClient",
    "AsyncOllamaLLMClient",
    "LLMClient",
    "ModelConfig",
    "OllamaConfig",
    "OllamaLLMClient",
    "load_model_config",
    "make_async_llm",
    "make_llm",
]
