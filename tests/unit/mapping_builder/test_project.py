"""Relational-to-graph projection: junction predicate, key choice, validity."""

from __future__ import annotations

from rows2graph import SchemaMapping
from rows2graph.mapping_builder.ddl import extract_schema_from_ddl
from rows2graph.mapping_builder.project import (
    choose_primary_key,
    is_junction_table,
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
