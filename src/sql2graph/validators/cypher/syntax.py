"""Cypher syntax validator (grammar-based, deployment-free).

Parses each candidate query with Neo4j's own ANTLR grammar (the ``Cypher25``
lexer/parser vendored under
:mod:`sql2graph.validators._grammar.generated.cypher`) and reports any syntax
errors. Unlike the previous regex heuristic it understands real Cypher
structure (clause shape, node/relationship patterns, expressions, string
literals), so it catches the malformed queries an LLM produces (truncated
patterns, stray tokens, bad expressions) while accepting every *syntactically*
valid query, including single-quoted strings that contain brackets (which the
old bracket-counting heuristic wrongly rejected).

It runs entirely in-process: no Neo4j instance, only the pure-Python
``antlr4-python3-runtime``. It validates *syntax*, not schema, so it does not
catch label / relationship-type / property hallucinations; that remains the
job of the server validator
(:class:`sql2graph.validators.cypher.server.CypherServerValidator`).
"""

from __future__ import annotations

from sql2graph.validators._grammar.generated.cypher.Cypher25Lexer import Cypher25Lexer
from sql2graph.validators._grammar.generated.cypher.Cypher25Parser import Cypher25Parser
from sql2graph.validators._grammar.runtime import parse_errors

# Entry rule of Neo4j's Cypher grammar; anchors EOF, so trailing input is reported.
_START_RULE = "statements"


def _cypher_syntax_errors(query: str) -> list[str]:
    """Shared grammar-based syntax check used by both sync and async validators."""
    if not query.strip():
        return ["Query is empty"]
    return parse_errors(query, Cypher25Lexer, Cypher25Parser, _START_RULE)


class CypherSyntaxValidator:
    """Grammar-based syntax checks for Cypher queries (no database required)."""

    def validate(self, query: str) -> list[str]:
        return _cypher_syntax_errors(query)

    def close(self) -> None:
        return None


class AsyncCypherSyntaxValidator:
    """Async sibling of :class:`CypherSyntaxValidator`.

    Parsing is pure CPU and fast, so the work runs inline rather than being
    shipped to a thread pool; the latter would add scheduling overhead without
    unblocking the event loop in any meaningful way.
    """

    async def validate(self, query: str) -> list[str]:
        return _cypher_syntax_errors(query)

    async def close(self) -> None:
        return None
