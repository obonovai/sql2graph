"""AQL syntax validator (grammar-based, deployment-free).

Parses each candidate query with an ANTLR grammar hand-ported from ArangoDB's
own Flex+Bison parser (``AQLLexer.g4`` / ``AQLParser.g4``, vendored under
:mod:`sql2graph.validators._grammar.generated.aql`) and reports any syntax
errors. Unlike Cypher and Gremlin -- whose grammars are the engines' *own*
published ``.g4`` files -- ArangoDB ships no reusable offline grammar, so this
port reproduces the grammar structure for recognition only. It is therefore
*best-effort*: it may diverge slightly from ArangoDB's real parser (ANTLR has no
``%nonassoc``, so a few non-associative chains are over-accepted). The server
validator (:class:`sql2graph.validators.aql.server.AqlServerValidator`) remains
authoritative and additionally catches collection / attribute problems.

It runs entirely in-process: no ArangoDB, only the pure-Python
``antlr4-python3-runtime``. It validates *syntax*, not schema.
"""

from __future__ import annotations

from sql2graph.validators._grammar.generated.aql.AQLLexer import AQLLexer
from sql2graph.validators._grammar.generated.aql.AQLParser import AQLParser
from sql2graph.validators._grammar.runtime import parse_errors

# Entry rule of the ported AQL grammar; anchors EOF, so trailing input is reported.
_START_RULE = "queryStart"


def _aql_syntax_errors(query: str) -> list[str]:
    """Shared grammar-based syntax check used by both sync and async validators."""
    if not query.strip():
        return ["Query is empty"]
    return parse_errors(query, AQLLexer, AQLParser, _START_RULE)


class AqlSyntaxValidator:
    """Grammar-based syntax checks for AQL queries (no server required)."""

    def validate(self, query: str) -> list[str]:
        return _aql_syntax_errors(query)

    def close(self) -> None:
        return None


class AsyncAqlSyntaxValidator:
    """Async sibling of :class:`AqlSyntaxValidator`.

    Parsing is pure CPU and fast, so the work runs inline rather than being
    shipped to a thread pool; the latter would add scheduling overhead without
    unblocking the event loop in any meaningful way.
    """

    async def validate(self, query: str) -> list[str]:
        return _aql_syntax_errors(query)

    async def close(self) -> None:
        return None
