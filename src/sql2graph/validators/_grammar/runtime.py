"""Shared ANTLR parse plumbing for the grammar-based syntax validators.

The Cypher and Gremlin syntax validators share one routine: run the language's
ANTLR-generated lexer and parser over a candidate query and collect any syntax
errors as human-readable ``line:col`` strings. Those strings are exactly what
the generate-validate-fix loop feeds back to the LLM, so a precise parser
message ("mismatched input 'RETURN' expecting ...") is far better repair signal
than the previous regex heuristics could give.

Only the pure-Python ``antlr4-python3-runtime`` is needed at runtime; the
parsers themselves are committed under
:mod:`sql2graph.validators._grammar.generated` and regenerated from the
vendored ``.g4`` grammars by ``scripts/generate_parsers.sh``.
"""

from __future__ import annotations

from typing import Any

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

# Cap the number of reported errors. ANTLR's default error recovery can emit a
# cascade from a single real mistake; the first few are the actionable ones and
# keep the fix prompt focused.
_MAX_ERRORS = 5


class _CollectingErrorListener(ErrorListener):  # type: ignore[misc]  # antlr4 is untyped
    """ANTLR error listener that accumulates messages instead of printing them."""

    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []

    def syntaxError(self, recognizer: Any, offendingSymbol: Any, line: int, column: int, msg: str, e: Any) -> None:  # noqa: ARG002
        token = getattr(offendingSymbol, "text", None)
        near = f" near {token!r}" if token else ""
        self.errors.append(f"line {line}:{column} {msg}{near}")


def parse_errors(query: str, lexer_cls: Any, parser_cls: Any, start_rule: str) -> list[str]:
    """Parse ``query`` with the given ANTLR lexer/parser; return syntax errors.

    Returns an empty list when the query parses cleanly, otherwise a list of
    ``line L:C ...`` messages. ``start_rule`` is the grammar's entry rule
    (``"statements"`` for Cypher, ``"queryList"`` for Gremlin); both anchor
    ``EOF``, so trailing garbage after an otherwise-valid prefix is reported
    rather than silently accepted.
    """
    listener = _CollectingErrorListener()

    lexer = lexer_cls(InputStream(query))
    lexer.removeErrorListeners()
    lexer.addErrorListener(listener)

    parser = parser_cls(CommonTokenStream(lexer))
    parser.removeErrorListeners()
    parser.addErrorListener(listener)

    getattr(parser, start_rule)()
    errors: list[str] = listener.errors
    return errors[:_MAX_ERRORS]
