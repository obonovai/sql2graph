"""AQL syntax-only validator.

Analogous to :class:`rows2graph.validators.cypher.syntax.CypherSyntaxValidator`:
a deployment-free, regex-based sanity check for AQL strings. Catches obvious
structural defects but not collection-name or graph-name hallucinations —
prefer the server validator when an ArangoDB instance is available.
"""

from __future__ import annotations

import re

_VALID_START_RE = re.compile(
    r"^\s*(FOR|LET|INSERT|UPDATE|REPLACE|REMOVE|UPSERT|WITH|RETURN)\b",
    re.IGNORECASE,
)


def _aql_syntax_errors(query: str) -> list[str]:
    """Shared regex-based syntax check used by both sync and async validators."""
    errors: list[str] = []

    if not query.strip():
        errors.append("Query is empty")
        return errors

    if not _VALID_START_RE.match(query):
        errors.append(
            "Query does not start with a valid AQL keyword "
            "(FOR, LET, INSERT, UPDATE, REPLACE, REMOVE, UPSERT, WITH, RETURN)"
        )

    if query.count("(") != query.count(")"):
        errors.append("Unbalanced parentheses")

    if query.count("[") != query.count("]"):
        errors.append("Unbalanced square brackets")

    if query.count("{") != query.count("}"):
        errors.append("Unbalanced curly braces")

    # A top-level FOR without a RETURN is malformed (each FOR level must
    # terminate with a RETURN, COLLECT, or INSERT/UPDATE/REPLACE/REMOVE
    # — RETURN being by far the most common).
    if re.match(r"^\s*FOR\b", query, re.IGNORECASE):
        if not re.search(r"\bRETURN\b", query, re.IGNORECASE):
            errors.append("FOR query is missing a RETURN clause")

    return errors


class AqlSyntaxValidator:
    """Regex-based sanity checks for AQL queries."""

    def validate(self, query: str) -> list[str]:
        return _aql_syntax_errors(query)

    def close(self) -> None:
        return None


class AsyncAqlSyntaxValidator:
    """Async sibling of :class:`AqlSyntaxValidator`.

    See :class:`rows2graph.validators.cypher.syntax.AsyncCypherSyntaxValidator`
    for the rationale: regex is fast enough that ``asyncio.to_thread`` is
    not worth its overhead.
    """

    async def validate(self, query: str) -> list[str]:
        return _aql_syntax_errors(query)

    async def close(self) -> None:
        return None
