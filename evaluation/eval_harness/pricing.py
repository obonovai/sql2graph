"""USD cost and billed-token accounting for the evaluation harness.

Pure leaf module (standard library only) so the notebooks and a standalone
pytest can import it without pulling in the rest of the harness.

It encodes two facts the notebooks previously got wrong for Anthropic:

* The translator caches the whole system prompt (``cache_control: ephemeral``),
  so Anthropic reports the prompt split across four usage fields. ``input_tokens``
  is only the *uncached* delta; the bulk lands in ``cache_creation_tokens`` (first
  sight) and ``cache_read_tokens`` (reuse). True billed input is the sum of all
  three, matching what platform.claude.com shows as "tokens in".
* Cache tokens are billed at a multiple of the input rate: writes at 1.25x (the
  5-minute ephemeral TTL the translator uses) and reads at 0.10x.

Ollama runs locally: zero rate, no cache, both cache fields are always 0.
"""

from __future__ import annotations

# USD per million tokens, as (input, output), keyed by model.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Confirmed against platform.claude.com for the recorded opus-4-8 run.
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    # Placeholders: VERIFY against current pricing before trusting non-Opus
    # cost numbers. Only opus-4-8 is in RUN_MATRIX today, so nothing currently
    # reported depends on these rows.
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

# Fallback (input, output) rate when the exact model is not in MODEL_PRICING.
PROVIDER_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "ollama": (0.0, 0.0),  # local, free
    "anthropic": (5.0, 25.0),  # Opus-class default
}

# Anthropic prompt-cache multipliers applied to the *input* rate.
CACHE_WRITE_MULT = 1.25  # cache_creation_tokens (5-minute ephemeral TTL)
CACHE_READ_MULT = 0.10  # cache_read_tokens


def rate_for(provider: str, model: str) -> tuple[float, float]:
    """Return the (input, output) USD/Mtok rate for a model.

    Falls back to the provider default, then to ``(0.0, 0.0)`` for anything
    unknown (which keeps an unrecognised local model free rather than guessing).
    """
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    return PROVIDER_DEFAULT_PRICING.get(provider, (0.0, 0.0))


def billed_input_tokens(
    input_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    """Total prompt tokens billed = uncached input + both cache buckets."""
    return input_tokens + cache_read_tokens + cache_creation_tokens


def usd_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """USD for one call (or a summed set), pricing all four token buckets.

    Cache writes cost ``CACHE_WRITE_MULT`` times the input rate and cache reads
    ``CACHE_READ_MULT`` times it; output uses its own rate. For Ollama every rate
    is 0, so the cache multipliers are moot.
    """
    pin, pout = rate_for(provider, model)
    return (
        input_tokens * pin
        + cache_creation_tokens * pin * CACHE_WRITE_MULT
        + cache_read_tokens * pin * CACHE_READ_MULT
        + output_tokens * pout
    ) / 1e6
