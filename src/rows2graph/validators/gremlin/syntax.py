"""Gremlin syntax-only validator.

A lightweight, deployment-free validator: it checks a handful of regex-
based invariants over the candidate Gremlin-Groovy string. It does *not*
validate against any graph database's schema (a server-side validator is
the right tool for that), so it will not catch label / property
hallucinations.

The trade-off matches the Cypher and AQL syntax validators: with this
validator the framework can run end-to-end in CI or on a thesis
reviewer's laptop without provisioning a Gremlin Server. The server
validator (:class:`rows2graph.validators.gremlin.server.GremlinServerValidator`)
is the preferred option in any production-quality evaluation.
"""

from __future__ import annotations

import re

# Whitelist of Gremlin entry-point tokens that may legally start a
# script against the configured TraversalSource ``g``. ``g.with(...)``
# is included because configuration steps like ``g.with('evaluationTimeout',
# 60000).V()`` are legal entry points.
_VALID_START_RE = re.compile(
    r"^\s*g\.(V|E|addV|addE|with)\b",
    re.IGNORECASE,
)


def _gremlin_syntax_errors(query: str) -> list[str]:
    """Shared regex-based syntax check used by both sync and async validators."""
    errors: list[str] = []

    stripped = query.strip()
    if not stripped:
        errors.append("Query is empty")
        return errors

    if not _VALID_START_RE.match(query):
        errors.append(
            "Query does not start with a valid Gremlin entry point "
            "(g.V, g.E, g.addV, g.addE, g.with)"
        )

    if query.count("(") != query.count(")"):
        errors.append("Unbalanced parentheses")

    if query.count("[") != query.count("]"):
        errors.append("Unbalanced square brackets")

    if query.count("{") != query.count("}"):
        errors.append("Unbalanced curly braces")

    # A traversal that ends with a `.` is a half-written step and will
    # never compile. This catches the most common LLM truncation mode.
    if stripped.endswith("."):
        errors.append("Query ends with `.`: traversal step is incomplete")

    if query.count("'") % 2 != 0:
        errors.append("Unbalanced single quotes")

    if query.count('"') % 2 != 0:
        errors.append("Unbalanced double quotes")

    return errors


class GremlinSyntaxValidator:
    """Regex-based sanity checks for Gremlin-Groovy traversals."""

    def validate(self, query: str) -> list[str]:
        return _gremlin_syntax_errors(query)

    def close(self) -> None:
        return None


class AsyncGremlinSyntaxValidator:
    """Async sibling of :class:`GremlinSyntaxValidator`.

    Regex matching is pure CPU and microsecond-fast, so the work runs
    inline rather than being shipped to a thread pool; the latter would
    add scheduling overhead without unblocking the event loop in any
    meaningful way.
    """

    async def validate(self, query: str) -> list[str]:
        return _gremlin_syntax_errors(query)

    async def close(self) -> None:
        return None
