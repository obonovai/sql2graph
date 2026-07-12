"""Offline tests for query-time unified-edge expansion (harness.arango_edges).

These lock the rewrite that lets the eval query graphonauts's split snake_case edge
collections without materialising the unified SCREAMING_SNAKE collections (i.e. without
modifying the database). No ArangoDB needed: the expansion is pure string transformation.
"""

from __future__ import annotations

import re

from harness import load_dataset
from harness.arango_edges import UNIFIED_EDGES, expand_unified_edges

# The 5 unified edges whose split schema needs more than one source collection.
MULTI_SOURCE = {"HAS_CREATOR", "HAS_TAG", "IS_LOCATED_IN", "LIKES", "REPLY_OF"}


def _whole_identifier(name: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def test_unified_edges_mapping_is_pinned() -> None:
    # Guard the mapping's shape so a schema drift can't silently unpick the rewrite.
    assert len(UNIFIED_EDGES) == 15
    assert MULTI_SOURCE <= set(UNIFIED_EDGES)
    assert {k for k, v in UNIFIED_EDGES.items() if len(v) > 1} == MULTI_SOURCE
    assert UNIFIED_EDGES["HAS_CREATOR"] == ["post_has_creator", "comment_has_creator"]
    assert UNIFIED_EDGES["HAS_TAG"] == ["forum_has_tag", "post_has_tag", "comment_has_tag"]
    # Every source name is lowercase snake_case, so it can never re-trigger the rewrite.
    for sources in UNIFIED_EDGES.values():
        for s in sources:
            assert s == s.lower() and s.isidentifier()


def test_every_gold_aql_expands_to_split_collections() -> None:
    # Each gold AQL must expand to a query that names no unified edge as a whole identifier.
    checked = 0
    for q in load_dataset("ldbc"):
        aql = q.expected.get("aql")
        if not aql:
            continue
        checked += 1
        expanded = expand_unified_edges(aql)
        for name in UNIFIED_EDGES:
            assert not _whole_identifier(name).search(expanded), f"{name} survived in {q.id}"
    assert checked == 15  # the full gold set


def test_multi_source_expands_to_comma_list_in_place() -> None:
    # The split collections land right after the start vertex, as a traversal collection list.
    out = expand_unified_edges("FOR p IN 1..1 OUTBOUND po HAS_CREATOR RETURN p")
    assert out == "FOR p IN 1..1 OUTBOUND po post_has_creator, comment_has_creator RETURN p"
    out = expand_unified_edges("FOR m IN 1..1 INBOUND t HAS_TAG RETURN m")
    assert out == "FOR m IN 1..1 INBOUND t forum_has_tag, post_has_tag, comment_has_tag RETURN m"


def test_single_source_is_a_plain_rename() -> None:
    assert expand_unified_edges("FOR f IN 1..1 OUTBOUND p KNOWS RETURN f").endswith("OUTBOUND p knows RETURN f")
    # String start vertex + depth range (gold q15 shape).
    out = expand_unified_edges("FOR a, e, p IN 0..10 OUTBOUND 'Place/111' IS_PART_OF RETURN a")
    assert out == "FOR a, e, p IN 0..10 OUTBOUND 'Place/111' place_is_part_of RETURN a"


def test_string_literals_are_never_rewritten() -> None:
    # A unified name inside a quoted string is data, not a collection reference.
    assert expand_unified_edges("FOR t IN Tag FILTER t.name == 'HAS_TAG' RETURN t") == (
        "FOR t IN Tag FILTER t.name == 'HAS_TAG' RETURN t"
    )
    assert expand_unified_edges('FOR x IN C FILTER x.k == "KNOWS" RETURN x') == (
        'FOR x IN C FILTER x.k == "KNOWS" RETURN x'
    )
    # The IS_SAME_COLLECTION filter's PascalCase string arg (gold q05/q09) is untouched, while
    # the edge name beside it still expands.
    out = expand_unified_edges(
        "FOR c IN 1..1 INBOUND po REPLY_OF FILTER IS_SAME_COLLECTION('Comment', c) RETURN c"
    )
    assert out == (
        "FOR c IN 1..1 INBOUND po reply_of_post, reply_of_comment "
        "FILTER IS_SAME_COLLECTION('Comment', c) RETURN c"
    )


def test_comments_are_never_rewritten() -> None:
    assert expand_unified_edges("// KNOWS is directed\nFOR p IN Person RETURN p") == (
        "// KNOWS is directed\nFOR p IN Person RETURN p"
    )
    assert expand_unified_edges("/* HAS_TAG note */ FOR t IN Tag RETURN t") == (
        "/* HAS_TAG note */ FOR t IN Tag RETURN t"
    )


def test_prefix_collisions_do_not_cross_match() -> None:
    # HAS_TYPE and HAS_TAG are distinct unified edges; expanding one must not touch the other's
    # sources, and neither may match as a substring of a longer identifier.
    out = expand_unified_edges("FOR x IN 1..1 OUTBOUND v HAS_TYPE RETURN x")
    assert "has_type" in out and "forum_has_tag" not in out
    both = expand_unified_edges("FOR a IN 1..1 OUTBOUND v HAS_TAG FOR b IN 1..1 OUTBOUND v HAS_TYPE RETURN a")
    assert "forum_has_tag, post_has_tag, comment_has_tag" in both and "has_type" in both
    # A longer identifier that merely contains a unified name is left alone.
    assert expand_unified_edges("FOR x IN KNOWSX RETURN x") == "FOR x IN KNOWSX RETURN x"
    assert expand_unified_edges("FOR x IN MY_KNOWS RETURN x") == "FOR x IN MY_KNOWS RETURN x"


def test_no_op_and_idempotence() -> None:
    # A query already using split names is unchanged...
    split = "FOR m IN 1..1 INBOUND f._id post_has_creator, comment_has_creator RETURN m"
    assert expand_unified_edges(split) == split
    # ...a query with no edge at all is byte-identical...
    plain = "FOR p IN Person FILTER p.id == 933 RETURN { id: p.id, firstName: p.firstName }"
    assert expand_unified_edges(plain) == plain
    # ...and expanding twice equals expanding once (emitted split names are not unified names).
    for q in load_dataset("ldbc"):
        aql = q.expected.get("aql")
        if aql:
            once = expand_unified_edges(aql)
            assert expand_unified_edges(once) == once
