"""Target-aware query canonicalisation, structural components, and distances.

This is the shared implementation behind notebooks 03 (structural metrics) and
04 (distance metrics): the tokeniser, the canonicaliser (comment-strip +
alpha-rename of pattern bindings + keyword upper-casing), clause-component
extraction (node labels / edge types / directions / clause bodies / aggregations),
and the three string distances (token Levenshtein, token Jaccard, normalised
tree-edit distance over a shallow clause tree).

The functions take a ``target`` string ("cypher" | "aql" | "gremlin"). Cypher and
AQL are clause-based and share the clause-head machinery. Gremlin is a method
chain, so it has its own path: canonicalisation alpha-renames ``as('x')`` step
labels (the Gremlin analogue of pattern bindings), components come from a
balanced-paren scan over the token stream (hasLabel/has -> node labels,
out/in/both -> edge types + directions, filter-step arguments -> where tokens,
projection/order modulators -> return/order tokens), and the TED tree is the
real method-chain structure (one node per step, arguments as children, nested
anonymous traversals as subtrees).

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


# --- Gremlin: a method chain has no clause heads, so components come from a
# linear scan over the token stream. A "call" is IDENT followed by '('; a
# precomputed matching-paren map gives each call its argument token slice.
# Robust to unbalanced parens (invalid model output): a missing close matches
# to end-of-stream instead of raising.

_GREMLIN_EDGE_STEPS = {"out": "OUT", "in": "IN", "both": "BOTH", "outE": "OUT", "inE": "IN", "bothE": "BOTH"}
_GREMLIN_FILTER_STEPS = frozenset(["has", "hasNot", "hasId", "is", "where", "not", "and", "or", "filter"])
_GREMLIN_PROJECTION_STEPS = frozenset(
    ["project", "select", "values", "valueMap", "elementMap", "group", "groupCount"]
)
_GREMLIN_AGG_STEPS = frozenset(["count", "sum", "min", "max", "mean", "group", "groupCount", "fold"])


def _paren_map(tokens: list[str]) -> dict[int, int]:
    """Index of each '(' -> index of its matching ')' (missing close -> len(tokens))."""
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


def _string_value(token: str) -> str | None:
    if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
        return token[1:-1]
    return None


def _top_level_args(tokens: list[str], start: int, end: int) -> list[list[str]]:
    """Split tokens[start:end] (a call's argument slice) at depth-0 commas."""
    args: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for t in tokens[start:end]:
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        if t == "," and depth == 0:
            args.append(current)
            current = []
            continue
        current.append(t)
    if current:
        args.append(current)
    return args


def _components_gremlin(query: str) -> Components:
    text = strip_comments(query)
    text = alpha_rename(text, _binding_names(text, "gremlin"))
    tokens = tokenize(text)
    match = _paren_map(tokens)
    c = Components()
    # Anchor for by() modulator attribution; tracked at chain depth 0 only so a
    # nested select/order inside an argument cannot misattribute later by()s.
    anchor: str | None = None
    depth = 0
    for i, tok in enumerate(tokens):
        if tok == "(":
            depth += 1
            continue
        if tok == ")":
            depth -= 1
            continue
        if not _IDENT.fullmatch(tok) or i + 1 >= len(tokens) or tokens[i + 1] != "(":
            continue
        close = match.get(i + 1, len(tokens))
        arg_tokens = tokens[i + 2 : close]
        if tok == "hasLabel":
            c.node_labels.update(v for t in arg_tokens if (v := _string_value(t)) is not None)
        elif tok == "has":
            args = _top_level_args(tokens, i + 2, close)
            if len(args) >= 3 and len(args[0]) == 1 and (label := _string_value(args[0][0])) is not None:
                c.node_labels.add(label)
        elif tok in _GREMLIN_EDGE_STEPS:
            c.edge_types.update(v for t in arg_tokens if (v := _string_value(t)) is not None)
            c.directions.append(_GREMLIN_EDGE_STEPS[tok])
        if tok in _GREMLIN_FILTER_STEPS:
            c.where_tokens.update(arg_tokens)
        if tok in _GREMLIN_AGG_STEPS:
            c.aggregations.add(tok.lower())
        if depth == 0:
            if tok in _GREMLIN_PROJECTION_STEPS:
                anchor = "return"
                c.return_tokens.update(arg_tokens)
            elif tok == "order":
                anchor = "order"
            elif tok == "by":
                (c.order_tokens if anchor == "order" else c.return_tokens).update(arg_tokens)
            elif tok == "limit" and arg_tokens and c.limit_value is None:
                c.limit_value = arg_tokens[0]
    c.edge_types -= c.node_labels
    return c


def components_of(query: str, target: str) -> Components:
    if target == "cypher":
        return _components_cypher(query)
    if target == "aql":
        return _components_aql(query)
    if target == "gremlin":
        return _components_gremlin(query)
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
    children: list[ClauseNode] = field(default_factory=list)

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


def _gremlin_chain_tree(query: str) -> ClauseNode:
    """Method-chain tree: one node per step call (label = step name), children =
    that call's arguments, where nested calls (anonymous ``__.`` traversals,
    ``P.gt(...)`` predicates) recurse into subtrees and plain literals become
    leaves. Chaining dots and argument commas are structural separators, not
    nodes. TED over this tree measures step-level structure."""
    tokens = canonicalize(query, "gremlin").split(" ")
    match = _paren_map(tokens)

    def parse_seq(i: int, end: int) -> list[ClauseNode]:
        nodes: list[ClauseNode] = []
        while i < end:
            t = tokens[i]
            if t in {".", ","}:
                i += 1
                continue
            if _IDENT.fullmatch(t) and i + 1 < end and tokens[i + 1] == "(":
                close = min(match.get(i + 1, end), end)
                node = ClauseNode(label=t)
                node.children = parse_seq(i + 2, close)
                nodes.append(node)
                i = close + 1
                continue
            nodes.append(ClauseNode(label=t))
            i += 1
        return nodes

    root = ClauseNode(label="QUERY")
    root.children = parse_seq(0, len(tokens))
    return root


def parse_to_clause_tree(query: str, target: str) -> ClauseNode:
    if target == "gremlin":
        return _gremlin_chain_tree(query)
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
