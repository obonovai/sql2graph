"""Relational-to-graph projection: junction predicate, key choice, validity."""

from __future__ import annotations

from sql2graph import SchemaMapping, SemanticType
from sql2graph.mapping_builder.ddl import extract_schema_from_ddl
from sql2graph.mapping_builder.project import (
    choose_primary_key,
    is_composition_fk,
    is_junction_table,
    is_multivalue_property_table,
    project_to_mapping,
)


def test_junction_detected_for_association_table(tpch_ddl: str) -> None:
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    partsupp = schema.table("partsupp")
    assert partsupp is not None
    assert is_junction_table(partsupp, schema) is True


def test_junction_detected_for_self_association() -> None:
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE person (id INT PRIMARY KEY);
        CREATE TABLE knows (a INT REFERENCES person(id), b INT REFERENCES person(id), since DATE, PRIMARY KEY (a, b));
        """
    )
    knows = schema.table("knows")
    assert knows is not None
    assert is_junction_table(knows, schema) is True


def test_surrogate_key_two_fk_table_is_not_a_junction() -> None:
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE person (id INT PRIMARY KEY);
        CREATE TABLE organisation (id INT PRIMARY KEY);
        CREATE TABLE study_at (id INT PRIMARY KEY, person_id INT REFERENCES person(id),
            organisation_id INT REFERENCES organisation(id), class_year INT);
        """
    )
    study_at = schema.table("study_at")
    assert study_at is not None
    assert is_junction_table(study_at, schema) is False


def test_referenced_table_is_never_a_junction() -> None:
    # `link` looks like a junction, but `child` references it, so it must stay a node.
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE a (id INT PRIMARY KEY);
        CREATE TABLE b (id INT PRIMARY KEY);
        CREATE TABLE link (aid INT REFERENCES a(id), bid INT REFERENCES b(id), PRIMARY KEY (aid, bid));
        CREATE TABLE child (id INT PRIMARY KEY, link_aid INT REFERENCES link(aid));
        """
    )
    link = schema.table("link")
    assert link is not None
    assert is_junction_table(link, schema) is False


def test_multivalue_table_becomes_list_property() -> None:
    # person_email(person_id, email) keyed entirely by (person_id, email) is a
    # value list, not a node or an edge: it folds into a Person list property.
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE person (id INT PRIMARY KEY, name TEXT);
        CREATE TABLE person_email (person_id INT REFERENCES person(id), email VARCHAR(64), PRIMARY KEY (person_id, email));
        CREATE TABLE person_speaks (person_id INT REFERENCES person(id), language TEXT, PRIMARY KEY (person_id, language));
        """,
        dialect="postgres",
    )
    person_email = schema.table("person_email")
    assert person_email is not None
    assert is_multivalue_property_table(person_email, schema) is True

    result = project_to_mapping(schema)
    assert {n.source_table for n in result.mapping.nodes} == {"person"}  # no PersonEmail/PersonSpeak node
    assert result.mapping.edges == []  # no HAS_PERSON edges
    person = next(n for n in result.mapping.nodes if n.source_table == "person")
    assert set(person.list_properties) == {"email", "language"}
    email = person.list_properties["email"]
    assert (email.source_table, email.foreign_key, email.column) == ("person_email", "person_id", "email")
    assert email.type is SemanticType.STRING  # VARCHAR -> string
    assert sorted(result.report.list_property_tables) == ["person_email", "person_speaks"]


def test_surrogate_key_single_fk_table_stays_a_node() -> None:
    # A one-FK table WITH its own surrogate id is a real entity (its id is not the
    # foreign key), so it must remain a node, not fold into a list property.
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE person (id INT PRIMARY KEY);
        CREATE TABLE post (id INT PRIMARY KEY, content TEXT, creator_id INT REFERENCES person(id));
        """
    )
    post = schema.table("post")
    assert post is not None
    assert is_multivalue_property_table(post, schema) is False
    result = project_to_mapping(schema)
    assert "post" in {n.source_table for n in result.mapping.nodes}


def test_junction_table_is_not_a_multivalue_property() -> None:
    # Two-FK association tables are edges, never list properties.
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE person (id INT PRIMARY KEY);
        CREATE TABLE tag (id INT PRIMARY KEY);
        CREATE TABLE has_interest (person_id INT REFERENCES person(id), tag_id INT REFERENCES tag(id), PRIMARY KEY (person_id, tag_id));
        """
    )
    has_interest = schema.table("has_interest")
    assert has_interest is not None
    assert is_multivalue_property_table(has_interest, schema) is False
    assert is_junction_table(has_interest, schema) is True


def test_on_delete_cascade_reverses_edge_direction() -> None:
    # ON DELETE CASCADE marks composition (forum owns its posts) -> Forum -> Post,
    # the reverse of the default FK-holder -> referenced direction. The join columns
    # are unchanged; only the labeled endpoints swap.
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE forum (id INT PRIMARY KEY);
        CREATE TABLE post (id INT PRIMARY KEY, forum_id INT NOT NULL REFERENCES forum(id) ON DELETE CASCADE);
        """,
        dialect="postgres",
    )
    post = schema.table("post")
    assert post is not None
    assert is_composition_fk(post, post.single_column_foreign_keys()[0]) is True
    edge = next(e for e in project_to_mapping(schema).mapping.edges if e.source_table == "post")
    assert (edge.source_node, edge.target_node) == ("Forum", "Post")
    assert (edge.source_foreign_key, edge.target_primary_key) == ("forum_id", "id")


def test_plain_fk_keeps_default_child_to_parent_direction() -> None:
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE forum (id INT PRIMARY KEY);
        CREATE TABLE post (id INT PRIMARY KEY, forum_id INT NOT NULL REFERENCES forum(id));
        """,
        dialect="postgres",
    )
    post = schema.table("post")
    assert post is not None
    assert is_composition_fk(post, post.single_column_foreign_keys()[0]) is False
    edge = next(e for e in project_to_mapping(schema).mapping.edges if e.source_table == "post")
    assert (edge.source_node, edge.target_node) == ("Post", "Forum")


def test_identifying_fk_reverses_direction_and_is_reported() -> None:
    # A weak entity (FK is part of the PK, plus a non-key attribute so it is not a
    # pure value-list table) is composition -> parent -> child.
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE orders (id INT PRIMARY KEY);
        CREATE TABLE lineitem (order_id INT REFERENCES orders(id), line_no INT, qty INT, PRIMARY KEY (order_id, line_no));
        """,
        dialect="postgres",
    )
    lineitem = schema.table("lineitem")
    assert lineitem is not None
    assert is_composition_fk(lineitem, lineitem.single_column_foreign_keys()[0]) is True
    result = project_to_mapping(schema)
    edge = next(e for e in result.mapping.edges if e.source_table == "lineitem")
    assert (edge.source_node, edge.target_node) == ("Order", "Lineitem")
    assert any("composition" in w.lower() for w in result.report.warnings)


def test_choose_primary_key_prefers_non_fk_of_composite(tpch_ddl: str) -> None:
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    lineitem = schema.table("lineitem")
    assert lineitem is not None
    assert choose_primary_key(lineitem) == ("linenumber", False)


def test_choose_primary_key_synthesizes_when_absent() -> None:
    table = extract_schema_from_ddl("CREATE TABLE t (a INT, b INT);").table("t")
    assert table is not None
    assert choose_primary_key(table) == ("a", True)


def test_projection_is_valid_by_construction(tpch_ddl: str) -> None:
    # project_to_mapping builds straight into SchemaMapping, so a bad projection
    # would raise pydantic ValidationError here rather than returning.
    result = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres"))
    assert isinstance(result.mapping, SchemaMapping)
    assert {n.source_table for n in result.mapping.nodes} == {
        "region", "nation", "supplier", "customer", "part", "orders", "lineitem"
    }
    assert result.report.edge_tables == ["partsupp"]


def test_label_collision_is_disambiguated() -> None:
    # `order` and `orders` both singularize/pascal to `Order`; the mapping must
    # still be valid (unique labels), with the collision flagged.
    result = project_to_mapping(
        extract_schema_from_ddl(
            "CREATE TABLE order (id INT PRIMARY KEY); CREATE TABLE orders (id INT PRIMARY KEY);"
        )
    )
    labels = sorted(n.label for n in result.mapping.nodes)
    assert labels == ["Order", "Order2"]
    assert any("collision" in w.lower() for w in result.report.warnings)


def test_node_properties_exclude_fk_columns_but_keep_pk(tpch_ddl: str) -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres")).mapping
    supplier = next(n for n in mapping.nodes if n.source_table == "supplier")
    assert "nationkey" not in supplier.properties  # FK -> becomes an edge
    assert supplier.primary_key == "suppkey"
    assert "suppkey" in supplier.properties


def test_projection_duplicate_bare_name_is_valid_not_crash() -> None:
    # Two schema-qualified tables collapsing to the same bare name must not crash
    # the projection (it used to raise a duplicate-label ValidationError).
    result = project_to_mapping(
        extract_schema_from_ddl(
            "CREATE TABLE sales.orders (id INT PRIMARY KEY); CREATE TABLE archive.orders (id INT PRIMARY KEY);"
        )
    )
    assert isinstance(result.mapping, SchemaMapping)
    assert [n.label for n in result.mapping.nodes] == ["Order"]


def test_projection_junction_to_empty_table_drops_edge_not_crash() -> None:
    # A junction whose referenced table has no columns (dropped from nodes) must
    # not raise KeyError; the edge is dropped and the reason recorded.
    result = project_to_mapping(
        extract_schema_from_ddl(
            "CREATE TABLE a (LIKE x); CREATE TABLE b (id INT PRIMARY KEY); "
            "CREATE TABLE j (aid INT REFERENCES a(id), bid INT REFERENCES b(id), PRIMARY KEY (aid, bid));"
        )
    )
    assert isinstance(result.mapping, SchemaMapping)
    assert result.mapping.edges == []
    assert any("junction references a table that is not a node" in reason for _, reason in result.report.dropped_objects)


def test_projection_non_ascii_table_name_yields_non_blank_label() -> None:
    result = project_to_mapping(extract_schema_from_ddl('CREATE TABLE "用户" (id INT PRIMARY KEY);'))
    assert isinstance(result.mapping, SchemaMapping)
    assert all(n.label for n in result.mapping.nodes)  # no blank label (would fail NonBlankStr)
