"""Cypher syntax-only validator.

A lightweight, deployment-free validator: it checks a handful of regex-based
invariants over the candidate Cypher string. It does *not* validate against
the graph database's schema (a server-side validator is the right tool for
that), so it will not catch label/relationship-type/property hallucinations.

The trade-off is intentional: with this validator the framework can run
end-to-end in a CI environment or on a thesis reviewer's laptop without
provisioning a Neo4j instance. The server validator
(:class:`rows2graph.validators.cypher.server.CypherServerValidator`) is the
preferred option in any production-quality evaluation.
"""

from __future__ import annotations

import re

# Whitelist of Cypher statements that may legally start a query.
_VALID_START_RE = re.compile(
    r"^\s*(MATCH|CREATE|MERGE|RETURN|WITH|UNWIND|CALL|OPTIONAL\s+MATCH|"
    r"DETACH\s+DELETE|DELETE|SET|REMOVE|FOREACH|LOAD\s+CSV)",
    re.IGNORECASE,
)


def _cypher_syntax_errors(query: str) -> list[str]:
    """Shared regex-based syntax check used by both sync and async validators."""
    errors: list[str] = []

    if not query.strip():
        errors.append("Query is empty")
        return errors

    if not _VALID_START_RE.match(query):
        errors.append("Query does not start with a valid Cypher keyword (MATCH, CREATE, MERGE, RETURN, WITH, etc.)")

    if query.count("(") != query.count(")"):
        errors.append("Unbalanced parentheses")

    if query.count("[") != query.count("]"):
        errors.append("Unbalanced square brackets")

    if query.count("{") != query.count("}"):
        errors.append("Unbalanced curly braces")

    # A MATCH query without a RETURN is almost always an accidental
    # truncation — the model dropped the projection clause.
    if re.match(r"^\s*MATCH\b", query, re.IGNORECASE):
        if not re.search(r"\bRETURN\b", query, re.IGNORECASE):
            errors.append("MATCH query is missing a RETURN clause")

    return errors


class CypherSyntaxValidator:
    """Regex-based sanity checks for Cypher queries."""

    def validate(self, query: str) -> list[str]:
        return _cypher_syntax_errors(query)

    def close(self) -> None:
        return None


class AsyncCypherSyntaxValidator:
    """Async sibling of :class:`CypherSyntaxValidator`.

    Regex matching is pure CPU and microsecond-fast, so the work runs
    inline rather than being shipped to a thread pool — the latter would
    add scheduling overhead without unblocking the event loop in any
    meaningful way.
    """

    async def validate(self, query: str) -> list[str]:
        return _cypher_syntax_errors(query)

    async def close(self) -> None:
        return None
