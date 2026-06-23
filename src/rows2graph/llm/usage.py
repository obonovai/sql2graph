"""Token-usage accounting for LLM calls.

This is a leaf module: it imports only :mod:`pydantic` and the standard
library, so every other module in :mod:`rows2graph.llm` — and
:mod:`rows2graph.state` — can import it without risking an import cycle.

Two small value types live here:

* :class:`TokenUsage` — the backend-agnostic token counts for one or more
  LLM calls. ``input_tokens``/``output_tokens`` are the common core; the two
  cache fields are Anthropic-specific (always ``0`` for Ollama, which has no
  prompt cache). Instances are additive, so the generate–validate–fix loop can
  accumulate per-call usage with ``+``.
* :class:`ChatReply` — what an :class:`~rows2graph.llm.LLMClient` returns from
  ``chat``: the assistant turn's text plus the :class:`TokenUsage` it cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, computed_field


class TokenUsage(BaseModel):
    """Token counts accumulated over one or more LLM calls.

    ``input_tokens`` and ``output_tokens`` are backend-agnostic. For Anthropic,
    ``input_tokens`` is the *uncached* prompt portion only — tokens served from
    or written to the prompt cache are reported separately in
    ``cache_read_tokens`` and ``cache_creation_tokens``. Both cache fields are
    always ``0`` for Ollama, which has no prompt cache. ``total_tokens`` is the
    sum of all four, i.e. every token billed across the call(s).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    # @computed_field surfaces total_tokens in model_dump()/model_dump_json()
    # for eval + display. mypy can't model the property/decorator stack, hence
    # the targeted ignore (pydantic's documented guidance).
    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


@dataclass(frozen=True)
class ChatReply:
    """A completed assistant turn: the response text plus its token usage."""

    text: str
    usage: TokenUsage
