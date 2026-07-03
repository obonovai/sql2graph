"""Structural clause-component extraction and Component F1 (notebook 03).

Pulls the structural pieces out of a query - node labels, edge types,
directions, filter/projection/order tokens, LIMIT, aggregations - and scores a
translation against the gold query per component. Cypher and AQL share the
clause-head machinery from :mod:`harness.canonical`; Gremlin components come
from a balanced-paren scan over the token stream (hasLabel/has -> node labels,
out/in/both -> edge types + directions, filter-step arguments -> where tokens,
projection/order modulators -> return/order tokens).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .canonical import (
    _IDENT,
    _binding_names,
    _paren_map,
    alpha_rename,
    canonicalize,
    clause_heads_for,
    strip_comments,
    tokenize,
)

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
    heads = clause_heads_for("cypher")
    c.where_tokens = set(_clause_body(tokens, heads, ("WHERE",)))
    c.return_tokens = set(_clause_body(tokens, heads, ("RETURN",)))
    c.order_tokens = set(_clause_body(tokens, heads, ("ORDER BY",)))
    limit_body = _clause_body(tokens, heads, ("LIMIT",))
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
    heads = clause_heads_for("aql")
    c.where_tokens = set(_clause_body(tokens, heads, ("FILTER",)))
    c.return_tokens = set(_clause_body(tokens, heads, ("RETURN",)))
    c.order_tokens = set(_clause_body(tokens, heads, ("SORT",)))
    limit_body = _clause_body(tokens, heads, ("LIMIT",))
    c.limit_value = limit_body[0] if limit_body else None
    return c


# --- Gremlin: a method chain has no clause heads, so components come from a
# linear scan over the token stream. A "call" is IDENT followed by '('; the
# precomputed matching-paren map gives each call its argument token slice.

_GREMLIN_EDGE_STEPS = {"out": "OUT", "in": "IN", "both": "BOTH", "outE": "OUT", "inE": "IN", "bothE": "BOTH"}
_GREMLIN_FILTER_STEPS = frozenset(["has", "hasNot", "hasId", "is", "where", "not", "and", "or", "filter"])
_GREMLIN_PROJECTION_STEPS = frozenset(
    ["project", "select", "values", "valueMap", "elementMap", "group", "groupCount"]
)
_GREMLIN_AGG_STEPS = frozenset(["count", "sum", "min", "max", "mean", "group", "groupCount", "fold"])


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
