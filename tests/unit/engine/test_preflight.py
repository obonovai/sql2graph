"""Pre-flight helpers: source-table union and unmapped table/column detection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sql2graph import EdgeMapping, NodeMapping, SchemaMapping
from sql2graph.engine.preflight import find_unmapped_columns, find_unmapped_tables


def test_schema_mapping_source_tables_unions_nodes_and_edges(person_forum_schema: Callable[..., Any]) -> None:
    # _schema() maps persons + forums (nodes) and knows (edge source table).
    assert person_forum_schema().source_tables() == {"persons", "forums", "knows"}


def test_find_unmapped_tables_is_case_insensitive_and_sorted(person_forum_schema: Callable[..., Any]) -> None:
    mapping = person_forum_schema()
    # "persons" is covered (case-insensitively); "orders" and "Lineitem" are not.
    unmapped = find_unmapped_tables(frozenset({"PERSONS", "orders", "Lineitem"}), mapping)
    assert unmapped == ["Lineitem", "orders"]  # sorted, original casing kept


def test_find_unmapped_tables_empty_when_all_covered(person_forum_schema: Callable[..., Any]) -> None:
    assert find_unmapped_tables(frozenset({"persons", "knows"}), person_forum_schema()) == []


def test_find_unmapped_columns_flags_missing_property(forum_no_title_schema: Callable[..., Any]) -> None:
    mapping = forum_no_title_schema()
    assert find_unmapped_columns(frozenset({("forum", "title")}), mapping) == ["forum.title"]
    assert find_unmapped_columns(frozenset({("forum", "id")}), mapping) == []


def test_find_unmapped_columns_skips_pure_junction_tables(person_forum_schema: Callable[..., Any]) -> None:
    # `knows` in _schema() is only an edge source (never a node), so its columns
    # are not checkable; a junction FK must never be flagged.
    assert find_unmapped_columns(frozenset({("knows", "forum_id"), ("knows", "anything")}), person_forum_schema()) == []


def test_find_unmapped_columns_absorbs_join_keys_of_node_plus_edge_table() -> None:
    # A table that is BOTH a node source and an edge source: the edge's FK/PK
    # join columns are absorbed as covered, so they aren't false-flagged.
    mapping = SchemaMapping(
        nodes=[NodeMapping(label="Forum", source_table="forum", primary_key=["id"], properties={"id": "id"})],
        edges=[
            EdgeMapping(
                type="OWNS",
                source_node="Forum",
                target_node="Forum",
                source_table="forum",
                source_foreign_key=["owner_id"],
                target_primary_key=["id"],
            )
        ],
    )
    assert find_unmapped_columns(frozenset({("forum", "owner_id"), ("forum", "id")}), mapping) == []
    assert find_unmapped_columns(frozenset({("forum", "title")}), mapping) == ["forum.title"]


def test_find_unmapped_columns_is_case_insensitive_and_sorted(person_forum_schema: Callable[..., Any]) -> None:
    # Covered comparison casefolds both sides; output keeps SQL casing, sorted.
    assert find_unmapped_columns(frozenset({("PERSONS", "Full_Name")}), person_forum_schema()) == []
    assert find_unmapped_columns(
        frozenset({("forums", "z_missing"), ("forums", "a_missing")}), person_forum_schema()
    ) == [
        "forums.a_missing",
        "forums.z_missing",
    ]
