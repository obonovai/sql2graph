"""SchemaMapping validation, YAML round-trip, accessors, and shipped-mapping load.

Static: constructs mappings in-process and asserts the pydantic validators and
accessor helpers, with no network, LLM, or database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sql2graph import EdgeMapping, ListProperty, NodeMapping, SchemaMapping, SemanticType


def test_schema_rejects_unknown_edge_source_node() -> None:
    with pytest.raises(ValidationError, match=r"undefined source_node 'Alien'"):
        SchemaMapping(
            nodes=[NodeMapping(label="Person", source_table="t", primary_key=["id"], properties={"a": "a"})],
            edges=[
                EdgeMapping(
                    type="X",
                    source_node="Alien",
                    target_node="Person",
                    source_table="t",
                    source_foreign_key=["fk"],
                    target_primary_key=["id"],
                )
            ],
        )


def test_schema_rejects_unknown_edge_target_node() -> None:
    with pytest.raises(ValidationError, match=r"undefined target_node 'Alien'"):
        SchemaMapping(
            nodes=[NodeMapping(label="Person", source_table="t", primary_key=["id"], properties={"a": "a"})],
            edges=[
                EdgeMapping(
                    type="X",
                    source_node="Person",
                    target_node="Alien",
                    source_table="t",
                    source_foreign_key=["fk"],
                    target_primary_key=["id"],
                )
            ],
        )


def test_schema_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        # source_tabel is a typo for source_table; strict mode catches it
        NodeMapping(
            label="X",
            source_tabel="t",  # type: ignore[call-arg]
            primary_key=["id"],
            properties={"a": "a"},
        )


def test_schema_mapping_from_yaml_round_trip(tmp_path: Path) -> None:
    yaml_text = """
nodes:
  - label: "Person"
    source_table: "person"
    primary_key: "id"
    properties:
      name: "full_name"
edges: []
"""
    p = tmp_path / "m.yaml"
    p.write_text(yaml_text)
    mapping = SchemaMapping.from_yaml(p)
    assert mapping.nodes[0].label == "Person"
    assert mapping.edges == []


def test_schema_rejects_duplicate_node_labels() -> None:
    with pytest.raises(ValidationError, match="Duplicate node label"):
        SchemaMapping(
            nodes=[
                NodeMapping(label="Person", source_table="t1", primary_key=["id"], properties={"a": "a"}),
                NodeMapping(label="Person", source_table="t2", primary_key=["id"], properties={"b": "b"}),
            ],
            edges=[],
        )


def test_schema_rejects_blank_label() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(label="   ", source_table="t", primary_key=["id"], properties={"a": "a"})


def test_schema_rejects_blank_primary_key() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(label="X", source_table="t", primary_key=[""], properties={"a": "a"})


def test_composite_primary_key_loads_from_list() -> None:
    node = NodeMapping(
        label="LineItem",
        source_table="lineitem",
        primary_key=["orderkey", "linenumber"],
        properties={"orderkey": "orderkey", "linenumber": "linenumber"},
    )
    assert node.primary_key == ["orderkey", "linenumber"]


def test_scalar_primary_key_normalizes_to_list() -> None:
    # Backward compat: a legacy scalar key (as in every pre-composite YAML) loads
    # and is normalized to a one-element list.
    node = NodeMapping.model_validate(
        {"label": "Person", "source_table": "t", "primary_key": "id", "properties": {"id": "id"}}
    )
    assert node.primary_key == ["id"]


def test_empty_list_primary_key_rejected() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(label="X", source_table="t", primary_key=[], properties={"a": "a"})


def test_composite_edge_join_loads() -> None:
    edge = EdgeMapping(
        type="REFERS_TO",
        source_node="Child",
        target_node="LineItem",
        source_table="child",
        source_foreign_key=["order_id", "line_no"],
        target_primary_key=["orderkey", "linenumber"],
    )
    assert edge.source_foreign_key == ["order_id", "line_no"]
    assert edge.target_primary_key == ["orderkey", "linenumber"]


def test_scalar_edge_keys_normalize_to_lists() -> None:
    # Backward compat: legacy scalar join keys load and normalize to one-element lists.
    edge = EdgeMapping.model_validate(
        {
            "type": "X",
            "source_node": "A",
            "target_node": "B",
            "source_table": "a",
            "source_foreign_key": "fk",
            "target_primary_key": "id",
        }
    )
    assert edge.source_foreign_key == ["fk"]
    assert edge.target_primary_key == ["id"]


def test_edge_rejects_join_arity_mismatch() -> None:
    with pytest.raises(ValidationError, match=r"length-matched"):
        EdgeMapping(
            type="X",
            source_node="A",
            target_node="B",
            source_table="a",
            source_foreign_key=["fk1", "fk2"],
            target_primary_key=["id"],
        )


def test_schema_rejects_blank_property_value() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(label="X", source_table="t", primary_key=["id"], properties={"a": ""})


def test_schema_rejects_blank_edge_field() -> None:
    with pytest.raises(ValidationError):
        EdgeMapping(
            type="X",
            source_node="A",
            target_node="B",
            source_table="t",
            source_foreign_key=[""],
            target_primary_key=["id"],
        )


def test_schema_rejects_fully_duplicate_edges() -> None:
    with pytest.raises(ValidationError, match="Duplicate edge"):
        SchemaMapping(
            nodes=[NodeMapping(label="Person", source_table="t", primary_key=["id"], properties={"a": "a"})],
            edges=[
                EdgeMapping(
                    type="KNOWS",
                    source_node="Person",
                    target_node="Person",
                    source_table="knows",
                    source_foreign_key=["friend_id"],
                    target_primary_key=["id"],
                ),
                EdgeMapping(
                    type="KNOWS",
                    source_node="Person",
                    target_node="Person",
                    source_table="knows",
                    source_foreign_key=["friend_id"],
                    target_primary_key=["id"],
                ),
            ],
        )


def test_schema_allows_same_type_different_target() -> None:
    # Two LIKES edges from Person to different targets must NOT be rejected
    # (legitimate multi-junction pattern, as in ldbc.yaml).
    mapping = SchemaMapping(
        nodes=[
            NodeMapping(label="Person", source_table="person", primary_key=["id"], properties={"id": "id"}),
            NodeMapping(label="Post", source_table="post", primary_key=["id"], properties={"id": "id"}),
            NodeMapping(label="Comment", source_table="comment", primary_key=["id"], properties={"id": "id"}),
        ],
        edges=[
            EdgeMapping(
                type="LIKES",
                source_node="Person",
                target_node="Post",
                source_table="likes_post",
                source_foreign_key=["post_id"],
                target_primary_key=["id"],
            ),
            EdgeMapping(
                type="LIKES",
                source_node="Person",
                target_node="Comment",
                source_table="likes_comment",
                source_foreign_key=["comment_id"],
                target_primary_key=["id"],
            ),
        ],
    )
    assert len(mapping.edges) == 2


def test_list_property_loads_and_covers_source_table() -> None:
    yaml_text = """
nodes:
  - label: "Person"
    source_table: "person"
    primary_key: "id"
    properties:
      id: "id"
    list_properties:
      email:
        source_table: "person_email"
        foreign_key: "person_id"
        column: "email"
        type: "string"
      language:
        source_table: "person_speaks"
        foreign_key: "person_id"
        column: "language"
edges: []
"""
    mapping = SchemaMapping.from_yaml_string(yaml_text)
    person = mapping.nodes[0]
    assert set(person.list_properties) == {"email", "language"}
    assert person.list_properties["email"].source_table == "person_email"
    assert person.list_properties["email"].type is SemanticType.STRING
    assert person.list_properties["language"].type is None  # untyped is allowed
    # The child tables count as covered source tables, so pre-flight won't flag them.
    assert {"person_email", "person_speaks"} <= mapping.source_tables()


def test_list_property_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ListProperty(source_table="t", foreign_key="fk", column="c", typ="string")  # type: ignore[call-arg]


def test_node_rejects_property_declared_scalar_and_list() -> None:
    with pytest.raises(ValidationError, match="both scalar and list"):
        NodeMapping(
            label="Person",
            source_table="person",
            primary_key=["id"],
            properties={"email": "email"},
            list_properties={
                "email": ListProperty(source_table="person_email", foreign_key="person_id", column="email")
            },
        )


def test_schema_mapping_accessors() -> None:
    mapping = SchemaMapping(
        nodes=[
            NodeMapping(
                label="Person", source_table="person", primary_key=["id"], properties={"id": "id", "name": "full_name"}
            ),
            NodeMapping(label="Post", source_table="post", primary_key=["id"], properties={"id": "id"}),
        ],
        edges=[
            EdgeMapping(
                type="HAS_CREATOR",
                source_node="Post",
                target_node="Person",
                source_table="post",
                source_foreign_key=["creator_id"],
                target_primary_key=["id"],
                properties={"weight": "w"},
            ),
        ],
    )
    assert mapping.node_labels() == {"Person", "Post"}
    assert mapping.edge_types() == {"HAS_CREATOR"}
    assert mapping.properties_for_label("Person") == {"id", "name"}
    assert mapping.properties_for_label("Unknown") == set()
    assert mapping.properties_for_edge("HAS_CREATOR") == {"weight"}


def test_shipped_mappings_still_load(mappings_dir: Path) -> None:
    # Regression guard: the stricter validators must not reject the bundled
    # example mappings.
    for name in ("tpch.yaml", "ldbc.yaml"):
        mapping = SchemaMapping.from_yaml(mappings_dir / name)
        assert mapping.nodes
        assert mapping.edges
