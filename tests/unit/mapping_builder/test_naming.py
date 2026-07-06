"""Naming heuristics: table -> label and foreign-key / junction -> edge type."""

from __future__ import annotations

import pytest

from sql2graph.mapping_builder.naming import (
    edge_type_for_fk,
    junction_to_edge_type,
    table_to_label,
)
from sql2graph.mapping_builder.relational import ForeignKey


@pytest.mark.parametrize(
    ("table", "label"),
    [
        ("region", "Region"),
        ("orders", "Order"),
        ("line_item", "LineItem"),
        ("line_items", "LineItem"),
        ("categories", "Category"),
        ("boxes", "Box"),
        ("people", "Person"),
        ("status", "Status"),  # stop-set: not a plural
        ("series", "Series"),
        ("address", "Address"),
        ("forum_has_member", "ForumHasMember"),
        ("lineitem", "Lineitem"),  # documented gap: no separator to split on
    ],
)
def test_table_to_label(table: str, label: str) -> None:
    assert table_to_label(table) == label


def test_table_to_label_non_ascii_falls_back_to_identifier() -> None:
    # All-non-ASCII or all-symbol names have no ASCII tokens to PascalCase; the
    # label must still be non-blank or NodeMapping's NonBlankStr would reject it.
    assert table_to_label("用户") == "用户"  # CJK
    assert table_to_label("_") == "_"


def test_edge_type_for_fk() -> None:
    assert edge_type_for_fk(ForeignKey(("regionkey",), "region"), target_label="Region") == "HAS_REGION"
    assert edge_type_for_fk(ForeignKey(("moderator_person_id",), "person"), target_label="Person") == "MODERATOR_PERSON"
    assert (
        edge_type_for_fk(ForeignKey(("reply_of_comment_id",), "comment"), target_label="Comment") == "REPLY_OF_COMMENT"
    )
    assert edge_type_for_fk(ForeignKey(("id",), "person"), target_label="Person") == "HAS_PERSON"


def test_junction_to_edge_type() -> None:
    assert junction_to_edge_type("knows") == "KNOWS"
    assert junction_to_edge_type("study_at") == "STUDY_AT"
