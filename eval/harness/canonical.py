"""Target-aware query tokenisation and canonicalisation.

The shared string core behind the structural and distance metrics: the
tokeniser, per-target keyword/clause-head tables, and the canonicaliser
(comment-strip + alpha-rename of pattern bindings + keyword upper-casing).
:mod:`harness.components` (clause-component extraction, notebook 03) and
:mod:`harness.distances` (string/tree distances, notebook 04) both build on
these primitives, so a change to canonicalisation lands in both metric
families at once.

The functions take a ``target`` string ("cypher" | "aql" | "gremlin"). Cypher and
AQL are clause-based and share the clause-head machinery. Gremlin is a method
chain, so it has its own path: canonicalisation alpha-renames ``as('x')`` step
labels (the Gremlin analogue of pattern bindings), and the sibling modules use
:func:`_paren_map` to walk the chain's balanced parentheses.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")
_PUNCT = re.compile(r"[(){}\[\],;]|->|<-|<>|>=|<=|==|!=|=|<|>|\.|:|\+|\-|\*|/|\|")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMMENT_LINE = re.compile(r"//[^\n]*|--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_comments(query: str) -> str:
    query = _COMMENT_BLOCK.sub(" ", query)
    return _COMMENT_LINE.sub(" ", query)


def tokenize(query: str) -> list[str]:
    text = strip_comments(query)
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        for pat in (_STRING_LITERAL, _NUMBER_LITERAL, _IDENT, _PUNCT):
            m = pat.match(text, i)
            if m:
                tokens.append(m.group(0))
                i = m.end()
                break
        else:
            tokens.append(text[i])
            i += 1
    return tokens


def _paren_map(tokens: list[str]) -> dict[int, int]:
    """Index of each '(' -> index of its matching ')' (missing close -> len(tokens)).

    Robust to unbalanced parens (invalid model output): a missing close matches
    to end-of-stream instead of raising. Shared by the Gremlin paths in
    :mod:`harness.components` and :mod:`harness.distances`.
    """
    match: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(tokens):
        if t == "(":
            stack.append(i)
        elif t == ")" and stack:
            match[stack.pop()] = i
    for i in stack:
        match[i] = len(tokens)
    return match


# ---------------------------------------------------------------------------
# Keyword sets and clause heads (per target)
# ---------------------------------------------------------------------------

_CYPHER_KEYWORDS = frozenset([
    "MATCH", "OPTIONAL", "WHERE", "RETURN", "WITH", "ORDER", "BY", "LIMIT", "SKIP",
    "UNWIND", "CALL", "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "UNION",
    "DISTINCT", "AS", "AND", "OR", "NOT", "IN", "IS", "NULL", "TRUE", "FALSE",
    "ASC", "DESC", "CONTAINS", "STARTS", "ENDS", "WHEN", "CASE", "THEN", "ELSE", "END", "FOREACH",
])
_AQL_KEYWORDS = frozenset([
    "FOR", "IN", "LET", "RETURN", "FILTER", "SORT", "LIMIT", "COLLECT",
    "OUTBOUND", "INBOUND", "ANY", "GRAPH", "AGGREGATE", "WITH", "INTO", "DISTINCT",
    "ASC", "DESC", "AND", "OR", "NOT", "TRUE", "FALSE", "NULL",
    "INSERT", "UPDATE", "REPLACE", "REMOVE", "UPSERT", "OPTIONS", "LIKE",
])
# Gremlin step names are case-sensitive method names (hasLabel != HASLABEL), so
# no keyword case-folding: the canonical form preserves case and normalisation
# comes from tokenisation (whitespace/newlines) + as-label alpha-renaming.
_GREMLIN_KEYWORDS: frozenset[str] = frozenset()

_CYPHER_CLAUSE_HEADS = (
    "MATCH", "OPTIONAL MATCH", "WHERE", "RETURN", "WITH", "ORDER BY", "LIMIT", "SKIP",
    "UNWIND", "CALL", "CREATE", "MERGE", "DELETE", "DETACH DELETE", "SET", "REMOVE", "UNION",
)
_AQL_CLAUSE_HEADS = (
    "FOR", "LET", "FILTER", "SORT", "LIMIT", "COLLECT", "RETURN",
    "INSERT", "UPDATE", "REPLACE", "REMOVE", "UPSERT",
)
_GREMLIN_CLAUSE_HEADS: tuple[str, ...] = ()


def keywords_for(target: str) -> frozenset[str]:
    if target == "cypher":
        return _CYPHER_KEYWORDS
    if target == "aql":
        return _AQL_KEYWORDS
    if target == "gremlin":
        return _GREMLIN_KEYWORDS
    raise ValueError(f"Unknown target language: {target!r}")


def clause_heads_for(target: str) -> tuple[str, ...]:
    if target == "cypher":
        return _CYPHER_CLAUSE_HEADS
    if target == "aql":
        return _AQL_CLAUSE_HEADS
    if target == "gremlin":
        return _GREMLIN_CLAUSE_HEADS
    raise ValueError(f"Unknown target language: {target!r}")


def match_clause_head(tokens: list[str], i: int, heads: tuple[str, ...]) -> tuple[str, int] | None:
    """If a clause head starts at ``tokens[i]``, return ``(head, token count)`` else ``None``.

    Multi-word heads ("OPTIONAL MATCH", "ORDER BY") are tried longest-first so they beat
    the single-word head that shares their first token. Shared by the clause-tree parser
    (:mod:`harness.distances`) and the clause-component extractor (:mod:`harness.components`),
    so both segment a query into clauses the same way.
    """
    for h in sorted(heads, key=lambda x: -len(x.split())):
        parts = h.split()
        if i + len(parts) > len(tokens):
            continue
        if all(tokens[i + k].upper() == parts[k] for k in range(len(parts))):
            return h, len(parts)
    return None


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------

_CYPHER_BINDING = re.compile(r"[(\[]([A-Za-z_][A-Za-z0-9_]*)\b")
_AQL_BINDING = re.compile(r"\b(?:FOR|LET)\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)
# Gremlin's bindings are the step labels declared by .as('x') and referenced by
# select('x') / P.eq('x') / from('x') / to('x'). The generic alpha_rename then
# rewrites every \b-delimited occurrence, including inside those string
# literals. Same caveat as the Cypher path: a binding whose name collides with
# an unrelated identifier or string (e.g. as('name') vs by('name')) is renamed
# there too; short conventional labels (a, p1, po) make this a non-issue.
_GREMLIN_BINDING = re.compile(r"\.as\(\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1")


def _binding_names(text: str, target: str) -> list[str]:
    if target == "cypher":
        return [m.group(1) for m in _CYPHER_BINDING.finditer(text)]
    if target == "aql":
        return [m.group(1) for m in _AQL_BINDING.finditer(text)]
    return [m.group(2) for m in _GREMLIN_BINDING.finditer(text)]


def alpha_rename(query: str, bindings: list[str]) -> str:
    rename: dict[str, str] = {}
    for name in bindings:
        if name not in rename:
            rename[name] = f"v{len(rename)}"
    if not rename:
        return query
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in sorted(rename, key=len, reverse=True)) + r")\b"
    )
    return pattern.sub(lambda m: rename[m.group(1)], query)


def canonicalize(query: str, target: str) -> str:
    text = strip_comments(query)
    text = alpha_rename(text, _binding_names(text, target))
    kws = keywords_for(target)
    out: list[str] = []
    for t in tokenize(text):
        if t.startswith("'") or t.startswith('"'):
            out.append(t)
        else:
            up = t.upper()
            out.append(up if up in kws else t)
    return " ".join(out)


def exact_match(translated: str, expected: str, target: str) -> bool:
    return canonicalize(translated, target) == canonicalize(expected, target)
