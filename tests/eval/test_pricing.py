"""Unit tests for the evaluation harness cost/token accounting.

``harness`` lives under ``eval/`` (added to ``sys.path`` by the notebooks);
the conftest mirrors that here so this runs under the default
``testpaths=["tests"]`` pytest config without hitting any LLM.
"""

from __future__ import annotations

from harness.pricing import billed_input_tokens, rate_for, usd_cost

# Recorded totals for the ldbc/cypher/claude-opus-4-8 run
# (eval/outputs/records/records_ldbc_cypher_claude-opus-4-8.json, 14 records).
OPUS_INPUT = 1734
OPUS_OUTPUT = 1315
OPUS_CACHE_READ = 13781
OPUS_CACHE_CREATE = 49339


def test_billed_input_adds_both_cache_buckets() -> None:
    # Matches platform.claude.com "64,854 tokens in".
    assert billed_input_tokens(OPUS_INPUT, OPUS_CACHE_READ, OPUS_CACHE_CREATE) == 64854


def test_opus_cost_matches_platform() -> None:
    # Cache writes at 1.25x input, reads at 0.10x input -> ~$0.357, i.e. the
    # $0.35 platform.claude.com reported. The old formula priced only the
    # uncached input+output and returned ~$0.04.
    cost = usd_cost(
        "anthropic",
        "claude-opus-4-8",
        OPUS_INPUT,
        OPUS_OUTPUT,
        OPUS_CACHE_READ,
        OPUS_CACHE_CREATE,
    )
    assert abs(cost - 0.3568) < 1e-3


def test_ollama_is_free() -> None:
    assert usd_cost("ollama", "qwen3-coder:30b", 40156, 823, 0, 0) == 0.0


def test_unknown_model_falls_back_to_provider_rate() -> None:
    assert rate_for("anthropic", "claude-something-unreleased") == (5.0, 25.0)
    assert rate_for("ollama", "llama3") == (0.0, 0.0)


def test_cache_tokens_dominate_when_prompt_is_cached() -> None:
    # Guard against a regression to the old behaviour: dropping the cache
    # buckets must change the cost by a large factor, not a rounding error.
    with_cache = usd_cost(
        "anthropic", "claude-opus-4-8", OPUS_INPUT, OPUS_OUTPUT, OPUS_CACHE_READ, OPUS_CACHE_CREATE
    )
    without_cache = usd_cost("anthropic", "claude-opus-4-8", OPUS_INPUT, OPUS_OUTPUT, 0, 0)
    assert with_cache > 5 * without_cache
