"""Pass-through validator.

Returns the empty error list for any input, so the translator's
generate–validate–fix loop exits after the first iteration without ever
invoking the LLM a second time. Used by ``--validation none`` when the
caller wants to inspect raw LLM output (typical during prompt engineering
or when benchmarking pure single-shot translation quality).
"""

from __future__ import annotations


class NoopValidator:
    """A validator that always reports success."""

    def validate(self, query: str) -> list[str]:  # noqa: ARG002
        return []

    def close(self) -> None:
        return None


class AsyncNoopValidator:
    """Async sibling of :class:`NoopValidator`."""

    async def validate(self, query: str) -> list[str]:  # noqa: ARG002
        return []

    async def close(self) -> None:
        return None
