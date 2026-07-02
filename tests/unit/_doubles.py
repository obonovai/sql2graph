"""In-process LLM doubles for the static (unit) suite.

These fakes stand in for the ``LLMClient`` / ``AsyncLLMClient`` protocols so the
translator and mapping-builder loops can be exercised without a real model. They
are exposed to tests through factory-fixtures (see the ``conftest.py`` files);
tests never import these classes directly.

Two behavioral families, kept deliberately distinct:

* ``Scripted*`` - a *queue* of responses with fixed per-call usage
  (``TokenUsage(10, 5)``), so translator loop tests can assert token
  accumulation across iterations. The async variant also streams char-by-char
  through ``stream_to`` and counts the streaming calls.
* ``OneShot*`` - a single canned reply (or a raised error) with zero usage, used
  by the mapping-builder refinement tests. The async variant streams the reply
  in two halves.
"""

from __future__ import annotations

from typing import Any

from rows2graph.llm.usage import ChatReply, TokenUsage


class ScriptedLLM:
    """In-process double for the LLMClient Protocol."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.temperatures: list[float | None] = []
        self.closed = False

    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> ChatReply:
        self.calls.append(list(messages))
        self.temperatures.append(temperature)
        # Fixed per-call usage (15 total tokens) lets loop tests assert accumulation.
        return ChatReply(text=self._responses.pop(0), usage=TokenUsage(input_tokens=10, output_tokens=5))

    def close(self) -> None:
        self.closed = True


class ScriptedAsyncLLM:
    """In-process double for the AsyncLLMClient Protocol.

    When ``stream_to`` is supplied, emits the response character-by-character
    through the callback before returning the full text, enough to exercise
    the streaming plumbing without needing a real LLM.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.temperatures: list[float | None] = []
        self.stream_calls: int = 0
        self.closed = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream_to: Any = None,
        temperature: float | None = None,
    ) -> ChatReply:
        self.calls.append(list(messages))
        self.temperatures.append(temperature)
        response = self._responses.pop(0)
        if stream_to is not None:
            self.stream_calls += 1
            for char in response:
                stream_to(char)
        # Fixed per-call usage (15 total tokens) lets loop tests assert accumulation.
        return ChatReply(text=response, usage=TokenUsage(input_tokens=10, output_tokens=5))

    async def close(self) -> None:
        self.closed = True


class OneShotLLM:
    """A one-shot LLM double returning a canned reply (or raising)."""

    def __init__(self, reply: str | None = None, *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> ChatReply:  # noqa: ARG002
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        return ChatReply(text=self._reply, usage=TokenUsage())

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


class OneShotAsyncLLM:
    """An async LLM double that streams a canned reply in two chunks."""

    def __init__(self, reply: str | None = None, *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error

    async def chat(
        self,
        messages: list[dict[str, Any]],  # noqa: ARG002
        *,
        stream_to: Any = None,
        temperature: float | None = None,  # noqa: ARG002
    ) -> ChatReply:
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        if stream_to is not None:
            mid = len(self._reply) // 2
            stream_to(self._reply[:mid])
            stream_to(self._reply[mid:])
        return ChatReply(text=self._reply, usage=TokenUsage())

    async def close(self) -> None:  # pragma: no cover - nothing to release
        pass
