"""Continuous distances between a translation and its gold query (notebook 04).

Three distances in [0, 1] over the canonical form from :mod:`harness.canonical`:
token Levenshtein, token-set Jaccard, and normalised tree-edit distance (APTED)
over a shallow clause tree. For Cypher/AQL the TED tree is one node per clause;
for Gremlin it is the real method-chain structure (one node per step, arguments
as children, nested anonymous traversals as subtrees). The ``apted`` dependency
(from the ``eval`` extra) is confined to this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apted import APTED, Config

from .canonical import _IDENT, _paren_map, canonicalize, clause_heads_for, match_clause_head


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
        m = match_clause_head(tokens, i, heads)
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
