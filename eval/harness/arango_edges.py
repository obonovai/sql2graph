"""Query-time expansion of unified edge names to graphonauts's split collections.

The gold AQL in eval/gold/ldbc.yaml (and examples/mappings/ldbc.yaml, which the
translator reasons over) uses *unified* SCREAMING_SNAKE edge names -- KNOWS, HAS_CREATOR,
HAS_TAG, IS_LOCATED_IN, LIKES, REPLY_OF, ... graphonauts's loader instead created
*snake_case, source-split* edge collections (knows, post_has_creator, comment_has_creator,
post_has_tag/comment_has_tag/forum_has_tag, ...). ArangoDB collection names are physical and
case-sensitive, so a traversal over a unified name hits "collection or view not found".

Rather than *materialise* the 15 unified collections into the database (which mutates
graphonauts's shared benchmark data), we rewrite each unified edge name in the AQL to the
comma-separated list of its source collections just before execution. ArangoDB's anonymous
traversal accepts a collection list after the start vertex --
``FOR v IN min..max OUTBOUND|INBOUND|ANY start coll1, coll2, ...`` -- and unions them, which
is exactly what graphonauts's own queries do (e.g. ``INBOUND f._id post_has_creator,
comment_has_creator``). The traversed edge set, ``_from``/``_to`` directions, and edge
properties are identical to a materialised unified collection, so result rows are unchanged;
downstream ``FILTER IS_SAME_COLLECTION('Post', v)`` still works on the real vertices.

This runs in the eval execution path only (harness.execution.run_aql). No database is
modified. The rewrite is a no-op for a query that names no unified edge, and is idempotent
(the split names it emits are not themselves unified names).
"""

from __future__ import annotations

import re

# Unified mapping edge type -> graphonauts source edge collection(s). The five multi-source
# entries are the merges the split snake_case schema cannot express as one collection.
# Directions (_from/_to) already match the mapping's source_node -> target_node for every
# source, so OUTBOUND/INBOUND traversals expand unchanged.
UNIFIED_EDGES: dict[str, list[str]] = {
    "KNOWS": ["knows"],
    "HAS_MEMBER": ["has_member"],
    "HAS_INTEREST": ["has_interest"],
    "CONTAINER_OF": ["container_of"],
    "IS_PART_OF": ["place_is_part_of"],
    "HAS_MODERATOR": ["has_moderator"],
    "HAS_TYPE": ["has_type"],
    "IS_SUBCLASS_OF": ["is_subclass_of"],
    "STUDY_AT": ["study_at"],
    "WORK_AT": ["work_at"],
    "HAS_CREATOR": ["post_has_creator", "comment_has_creator"],
    "HAS_TAG": ["forum_has_tag", "post_has_tag", "comment_has_tag"],
    "IS_LOCATED_IN": [
        "person_is_located_in",
        "post_is_located_in",
        "comment_is_located_in",
        "organisation_is_located_in",
    ],
    "LIKES": ["likes_post", "likes_comment"],
    "REPLY_OF": ["reply_of_post", "reply_of_comment"],
}

# One scanner that matches, in priority order: string literals and comments (protected spans,
# emitted unchanged) then a unified edge name as a whole identifier. Putting the protected
# spans first means a unified name inside a string/comment is consumed as that span and never
# rewritten. Names are alternated longest-first and fenced with identifier-boundary lookarounds
# ([A-Za-z0-9_], AQL's identifier characters) so e.g. HAS_TAG never matches inside HAS_TYPE and
# a name is never rewritten as a substring of a longer identifier.
_NAME_ALT = "|".join(re.escape(n) for n in sorted(UNIFIED_EDGES, key=len, reverse=True))
_SCANNER = re.compile(
    r"""
      (?P<sqstr>'(?:\\.|[^'\\])*')                        # single-quoted string
    | (?P<dqstr>"(?:\\.|[^"\\])*")                        # double-quoted string
    | (?P<line>//[^\n]*)                                  # // line comment
    | (?P<block>/\*.*?\*/)                                # /* block comment */
    | (?P<name>(?<![A-Za-z0-9_])(?:""" + _NAME_ALT + r""")(?![A-Za-z0-9_]))  # unified edge name
    """,
    re.VERBOSE | re.DOTALL,
)


def expand_unified_edges(aql: str) -> str:
    """Rewrite unified SCREAMING_SNAKE edge names to graphonauts's comma-separated split
    source collections for anonymous multi-edge-collection traversal.

    A no-op when the query names no unified edge; never rewrites inside a string literal or
    comment; idempotent (the emitted split names are not themselves unified names).
    """

    def _sub(m: re.Match) -> str:
        name = m.group("name")
        if name is None:  # a protected string/comment span: leave untouched
            return m.group(0)
        return ", ".join(UNIFIED_EDGES[name])

    return _SCANNER.sub(_sub, aql)
