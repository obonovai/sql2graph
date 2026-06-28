"""Gremlin syntax validator (grammar-based, deployment-free).

Parses each candidate traversal with Apache TinkerPop's own ANTLR grammar
(``Gremlin.g4``, vendored under
:mod:`rows2graph.validators._grammar.generated.gremlin`) and reports any syntax
errors. Unlike the previous regex heuristic it understands real Gremlin
structure (traversal steps, anonymous ``__`` traversals, predicates, closures,
string and numeric literals), so it catches the malformed traversals an LLM
produces (unterminated steps, a trailing ``.``, unbalanced delimiters, stray
tokens) while accepting every *syntactically* valid traversal.

It runs entirely in-process: no Gremlin Server, only the pure-Python
``antlr4-python3-runtime``. It validates *syntax*, not schema; the server
validator (:class:`rows2graph.validators.gremlin.server.GremlinServerValidator`)
remains the tool for step-compatibility and (on schema-aware backends) label /
property checks.
"""

from __future__ import annotations

from rows2graph.validators._grammar.errors import parse_errors
from rows2graph.validators._grammar.generated.gremlin.GremlinLexer import GremlinLexer
from rows2graph.validators._grammar.generated.gremlin.GremlinParser import GremlinParser

# Entry rule of TinkerPop's Gremlin grammar; anchors EOF, so trailing input is reported.
_START_RULE = "queryList"


def _gremlin_syntax_errors(query: str) -> list[str]:
    """Shared grammar-based syntax check used by both sync and async validators."""
    if not query.strip():
        return ["Query is empty"]
    return parse_errors(query, GremlinLexer, GremlinParser, _START_RULE)


class GremlinSyntaxValidator:
    """Grammar-based syntax checks for Gremlin traversals (no server required)."""

    def validate(self, query: str) -> list[str]:
        return _gremlin_syntax_errors(query)

    def close(self) -> None:
        return None


class AsyncGremlinSyntaxValidator:
    """Async sibling of :class:`GremlinSyntaxValidator`.

    Parsing is pure CPU and fast, so the work runs inline rather than being
    shipped to a thread pool; the latter would add scheduling overhead without
    unblocking the event loop in any meaningful way.
    """

    async def validate(self, query: str) -> list[str]:
        return _gremlin_syntax_errors(query)

    async def close(self) -> None:
        return None
