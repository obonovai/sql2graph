"""Derive mapping-aligned edge collections in graphonauts2's ArangoDB.

The gold AQL in evaluation/datasets/ldbc.yaml (and config/mappings/ldbc.yaml, which the
translator reasons over) uses *unified* SCREAMING_SNAKE edge names -- KNOWS, HAS_CREATOR,
HAS_TAG, IS_LOCATED_IN, LIKES, REPLY_OF, ... graphonauts2's loader instead created
*snake_case, source-split* edge collections (knows, post_has_creator, comment_has_creator,
post_has_tag/comment_has_tag/forum_has_tag, ...). ArangoDB collection names are
case-sensitive, so the gold (and any LLM candidate) traversals hit "collection or view not
found" against those.

This script additively builds the 15 unified edge collections the mapping declares by
copying every edge from its source collection(s), keeping ``_from``/``_to`` and edge
properties but dropping ``_id``/``_key``/``_rev`` so merged keys can't collide. Vertex
collections (Person, Post, ...) already match the mapping, so they are left untouched, as
are graphonauts2's original split collections and named graph (this is purely additive).

Idempotent: each unified collection is created-if-missing then truncated before refilling,
so re-running does not duplicate rows.

Run:
    ARANGO_PASSWORD=password uv run python evaluation/build_arango_unified_edges.py
"""

from __future__ import annotations

import os
import sys

from arango import ArangoClient

# --- connection config (ArangoDB holding the LDBC SNB SF1 data) ---
ARANGO_URL = os.environ.get("ARANGO_URL", "http://localhost:8529")
ARANGO_USER = os.environ.get("ARANGO_USER", "root")
ARANGO_PASSWORD = os.environ.get("ARANGO_PASSWORD", "password")
ARANGO_DB = os.environ.get("ARANGO_DB", "graphonauts")
# Copying the multi-source edges (a few million rows) runs server-side well past the
# python-arango default 60s HTTP read timeout, so raise it generously.
ARANGO_TIMEOUT = int(os.environ.get("ARANGO_REQUEST_TIMEOUT", "600"))

# Unified mapping edge type -> graphonauts2 source edge collection(s). The five
# multi-source entries are the merges that the split snake_case schema cannot express as
# one collection. Directions (_from/_to) already match the mapping's source_node ->
# target_node for every source, so gold OUTBOUND/INBOUND traversals work unchanged.
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

# Copy edges keeping _from/_to and properties, dropping identity so merged keys never clash.
COPY_AQL = "FOR e IN @@src INSERT UNSET(e, '_id', '_key', '_rev') INTO @@dst OPTIONS { waitForSync: false }"


def main() -> int:
    client = ArangoClient(hosts=ARANGO_URL, request_timeout=ARANGO_TIMEOUT)
    db = client.db(ARANGO_DB, username=ARANGO_USER, password=ARANGO_PASSWORD)

    ok = True
    for unified, sources in UNIFIED_EDGES.items():
        missing = [s for s in sources if not db.has_collection(s)]
        if missing:
            print(f"  SKIP {unified}: source collection(s) not found: {missing}")
            ok = False
            continue

        expected = sum(db.collection(s).count() for s in sources)

        # Idempotent fast-path: already built with the right row count -> leave it.
        if db.has_collection(unified) and db.collection(unified).count() == expected and expected > 0:
            print(f"  {unified:16} <- {'+'.join(sources):55} {expected:>10,} rows  already built (skip)")
            continue

        if db.has_collection(unified):
            db.collection(unified).truncate()
        else:
            db.create_collection(unified, edge=True)

        for src in sources:
            db.aql.execute(COPY_AQL, bind_vars={"@src": src, "@dst": unified})

        got = db.collection(unified).count()
        status = "ok" if got == expected else "COUNT MISMATCH"
        if got != expected:
            ok = False
        print(f"  {unified:16} <- {'+'.join(sources):55} {got:>10,} rows (expected {expected:,})  {status}")

    print()
    print(f"Built {len(UNIFIED_EDGES)} unified edge collections in db '{ARANGO_DB}'." if ok
          else "One or more collections failed; see COUNT MISMATCH / SKIP above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
