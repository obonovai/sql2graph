"""Target-aware query canonicalisation, structural components, and distances.

This is the shared implementation behind notebooks 03 (structural metrics) and
04 (distance metrics): the tokeniser, the canonicaliser (comment-strip +
alpha-rename of pattern bindings + keyword upper-casing), clause-component
extraction (node labels / edge types / directions / clause bodies / aggregations),
and the three string distances (token Levenshtein, token Jaccard, normalised
tree-edit distance over a shallow clause tree).

The functions take a ``target`` string ("cypher" | "aql" | "gremlin"). Cypher and
AQL are fully implemented; Gremlin is a stub (method-chain syntax is not
clause-based, so structural/TED metrics for it are future work) that degrades to
keyword-only canonicalisation and empty components rather than crashing.

Keeping this in one module means a change to canonicalisation lands in both
notebooks at once (the old copies in 03/04 had drifted-by-duplication risk).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from apted import APTED, Config

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
# Stub: Gremlin is a method-chain DSL, not a keyword/clause language. A minimal
# step set is upper-cased for crude normalisation; full support is future work.
_GREMLIN_KEYWORDS = frozenset([
    "G", "V", "E", "HAS", "OUT", "IN", "BOTH", "OUTE", "INE", "VALUES", "AS",
    "WHERE", "SELECT", "ORDER", "BY", "LIMIT", "COUNT", "GROUP", "DEDUP", "PROJECT",
])

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


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------

_CYPHER_BINDING = re.compile(r"[(\[]([A-Za-z_][A-Za-z0-9_]*)\b")
_AQL_BINDING = re.compile(r"\b(?:FOR|LET)\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)


def _binding_names(text: str, target: str) -> list[str]:
    if target == "cypher":
        return [m.group(1) for m in _CYPHER_BINDING.finditer(text)]
    if target == "aql":
        return [m.group(1) for m in _AQL_BINDING.finditer(text)]
    return []  # gremlin stub: no alpha-rename


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


# ---------------------------------------------------------------------------
# Component extraction
# ---------------------------------------------------------------------------

_CYPHER_NODE_LABEL = re.compile(r":\s*([A-Z][A-Za-z0-9_]*)")
_CYPHER_EDGE_TYPE = re.compile(r"\[\s*[A-Za-z_]?\w*\s*:\s*([A-Z_][A-Z0-9_]*)\s*[\]\s{]|\[:([A-Z_][A-Z0-9_]*)")
_CYPHER_DIRECTION = re.compile(r"->|<-")
_CYPHER_AGG = re.compile(r"\b(count|sum|min|max|avg|collect)\s*\(", re.IGNORECASE)

_AQL_COLLECTION = re.compile(r"\bFOR\s+[A-Za-z_]\w*\s+IN\s+([A-Z][A-Za-z0-9_]*)", re.IGNORECASE)
_AQL_EDGE_COLLECTION = re.compile(
    r"\b(?:OUTBOUND|INBOUND|ANY)\b\s+(?:[A-Za-z_]\w*\.[A-Za-z_]\w*|[A-Za-z_]\w*)\s+([A-Z][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_AQL_DIRECTION = re.compile(r"\b(OUTBOUND|INBOUND|ANY)\b", re.IGNORECASE)
_AQL_AGG = re.compile(r"\b(COUNT|SUM|MIN|MAX|AVERAGE|AVG|LENGTH|COLLECT|AGGREGATE)\s*\(", re.IGNORECASE)


@dataclass
class Components:
    node_labels: set[str] = field(default_factory=set)
    edge_types: set[str] = field(default_factory=set)
    directions: list[str] = field(default_factory=list)
    where_tokens: set[str] = field(default_factory=set)
    return_tokens: set[str] = field(default_factory=set)
    order_tokens: set[str] = field(default_factory=set)
    limit_value: str | None = None
    aggregations: set[str] = field(default_factory=set)


def _clause_body(tokens: list[str], heads: tuple[str, ...], labels: tuple[str, ...]) -> list[str]:
    """Return the flat token list from any clause whose head matches `labels`."""
    body: list[str] = []
    i = 0
    in_target = False
    while i < len(tokens):
        matched_head: str | None = None
        consumed = 0
        for h in sorted(heads, key=lambda x: -len(x.split())):
            parts = h.split()
            if i + len(parts) <= len(tokens) and all(tokens[i + k].upper() == parts[k] for k in range(len(parts))):
                matched_head = h
                consumed = len(parts)
                break
        if matched_head is not None:
            in_target = matched_head in labels
            i += consumed
            continue
        if in_target:
            body.append(tokens[i])
        i += 1
    return body


def _components_cypher(query: str) -> Components:
    text = strip_comments(query)
    c = Components()
    c.node_labels = {m.group(1) for m in _CYPHER_NODE_LABEL.finditer(text)}
    for m in _CYPHER_EDGE_TYPE.finditer(text):
        edge = m.group(1) or m.group(2)
        if edge:
            c.edge_types.add(edge)
    # Strip node-label hits from edge_types: node labels are TitleCase while
    # edge types are SCREAMING_SNAKE; intersect them out to avoid false hits.
    c.edge_types -= c.node_labels
    c.directions = _CYPHER_DIRECTION.findall(text)
    c.aggregations = {m.group(1).lower() for m in _CYPHER_AGG.finditer(text)}
    canon = canonicalize(query, "cypher")
    tokens = canon.split(" ")
    c.where_tokens = set(_clause_body(tokens, _CYPHER_CLAUSE_HEADS, ("WHERE",)))
    c.return_tokens = set(_clause_body(tokens, _CYPHER_CLAUSE_HEADS, ("RETURN",)))
    c.order_tokens = set(_clause_body(tokens, _CYPHER_CLAUSE_HEADS, ("ORDER BY",)))
    limit_body = _clause_body(tokens, _CYPHER_CLAUSE_HEADS, ("LIMIT",))
    c.limit_value = limit_body[0] if limit_body else None
    return c


def _components_aql(query: str) -> Components:
    text = strip_comments(query)
    c = Components()
    c.node_labels = {m.group(1) for m in _AQL_COLLECTION.finditer(text)}
    c.edge_types = {m.group(1) for m in _AQL_EDGE_COLLECTION.finditer(text)}
    c.directions = [d.upper() for d in _AQL_DIRECTION.findall(text)]
    c.aggregations = {m.group(1).lower() for m in _AQL_AGG.finditer(text)}
    canon = canonicalize(query, "aql")
    tokens = canon.split(" ")
    c.where_tokens = set(_clause_body(tokens, _AQL_CLAUSE_HEADS, ("FILTER",)))
    c.return_tokens = set(_clause_body(tokens, _AQL_CLAUSE_HEADS, ("RETURN",)))
    c.order_tokens = set(_clause_body(tokens, _AQL_CLAUSE_HEADS, ("SORT",)))
    limit_body = _clause_body(tokens, _AQL_CLAUSE_HEADS, ("LIMIT",))
    c.limit_value = limit_body[0] if limit_body else None
    return c


def components_of(query: str, target: str) -> Components:
    if target == "cypher":
        return _components_cypher(query)
    if target == "aql":
        return _components_aql(query)
    if target == "gremlin":
        return Components()  # stub: structural components for Gremlin are future work
    raise ValueError(f"Unknown target language: {target!r}")


def _set_f1(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    tp = len(a & b)
    if tp == 0:
        return 0.0
    p, r = tp / len(a), tp / len(b)
    return 2 * p * r / (p + r)


def _list_f1(a: list[str], b: list[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ac, bc = Counter(a), Counter(b)
    overlap = sum((ac & bc).values())
    if overlap == 0:
        return 0.0
    p, r = overlap / sum(ac.values()), overlap / sum(bc.values())
    return 2 * p * r / (p + r)


def component_f1(translated: str, expected: str, target: str) -> dict[str, float]:
    t = components_of(translated, target)
    e = components_of(expected, target)
    scores = {
        "node_labels": _set_f1(t.node_labels, e.node_labels),
        "edge_types": _set_f1(t.edge_types, e.edge_types),
        "directions": _list_f1(t.directions, e.directions),
        "where": _set_f1(t.where_tokens, e.where_tokens),
        "return": _set_f1(t.return_tokens, e.return_tokens),
        "order": _set_f1(t.order_tokens, e.order_tokens),
        "limit": 1.0 if t.limit_value == e.limit_value else 0.0,
        "aggregations": _set_f1(t.aggregations, e.aggregations),
    }
    scores["overall"] = sum(scores.values()) / len(scores)
    return scores


# ---------------------------------------------------------------------------
# Distances
# ---------------------------------------------------------------------------

def _levenshtein_tokens(a: list[str], b: list[str]) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    curr = [0] * (len(b) + 1)
    for i, ai in enumerate(a, start=1):
        curr[0] = i
        for j, bj in enumerate(b, start=1):
            cost = 0 if ai == bj else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    return prev[-1]


def levenshtein(translated: str, expected: str, target: str) -> float:
    ta = canonicalize(translated, target).split(" ")
    tb = canonicalize(expected, target).split(" ")
    denom = max(len(ta), len(tb))
    return _levenshtein_tokens(ta, tb) / denom if denom else 0.0


def jaccard(translated: str, expected: str, target: str) -> float:
    ta = set(canonicalize(translated, target).split(" "))
    tb = set(canonicalize(expected, target).split(" "))
    if not ta and not tb:
        return 0.0
    union = len(ta | tb)
    if union == 0:
        return 0.0
    return 1.0 - len(ta & tb) / union


@dataclass
class ClauseNode:
    label: str
    children: list["ClauseNode"] = field(default_factory=list)

    def size(self) -> int:
        return 1 + sum(c.size() for c in self.children)


def _match_clause_head(tokens: list[str], i: int, heads: tuple[str, ...]) -> tuple[str, int] | None:
    # Multi-word heads ("OPTIONAL MATCH", "ORDER BY") must beat single-word heads.
    for h in sorted(heads, key=lambda x: -len(x.split())):
        parts = h.split()
        if i + len(parts) > len(tokens):
            continue
        if all(tokens[i + k].upper() == parts[k] for k in range(len(parts))):
            return h, len(parts)
    return None


def parse_to_clause_tree(query: str, target: str) -> ClauseNode:
    tokens = canonicalize(query, target).split(" ")
    heads = clause_heads_for(target)
    root = ClauseNode(label="QUERY")
    current: ClauseNode | None = None
    i = 0
    while i < len(tokens):
        m = _match_clause_head(tokens, i, heads)
        if m is not None:
            label, consumed = m
            current = ClauseNode(label=label)
            root.children.append(current)
            i += consumed
            continue
        if current is None:
            current = ClauseNode(label="<prelude>")
            root.children.append(current)
        current.children.append(ClauseNode(label=tokens[i]))
        i += 1
    return root


class _TreeConfig(Config):
    def rename(self, n1: ClauseNode, n2: ClauseNode) -> int:
        return 0 if n1.label == n2.label else 1

    def children(self, node: ClauseNode) -> list[ClauseNode]:
        return node.children


def normalized_ted(translated: str, expected: str, target: str) -> float:
    t_tree = parse_to_clause_tree(translated, target)
    e_tree = parse_to_clause_tree(expected, target)
    distance = APTED(t_tree, e_tree, _TreeConfig()).compute_edit_distance()
    denom = max(t_tree.size(), e_tree.size())
    return distance / denom if denom else 0.0
