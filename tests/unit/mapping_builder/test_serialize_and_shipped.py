"""Closeness to the shipped tpch.yaml and YAML serialization round-tripping."""

from __future__ import annotations

from pathlib import Path

import pytest

from sql2graph import SchemaMapping
from sql2graph.mapping_builder import mapping_to_yaml
from sql2graph.mapping_builder.ddl import extract_schema_from_ddl
from sql2graph.mapping_builder.project import project_to_mapping


def test_generated_tpch_matches_shipped_join_semantics(tpch_ddl: str, mappings_dir: Path) -> None:
    generated = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres"))
    shipped = SchemaMapping.from_yaml(mappings_dir / "tpch.yaml")
    junction_tables = set(generated.report.edge_tables)

    gen_nodes = {n.source_table for n in generated.mapping.nodes}
    ship_nodes = {n.source_table for n in shipped.nodes}
    assert gen_nodes == ship_nodes

    def direct_triples(mapping: SchemaMapping) -> set[tuple[str, str, str]]:
        return {
            (e.source_table, e.source_foreign_key, e.target_primary_key)
            for e in mapping.edges
            if e.source_table not in junction_tables and e.source_table != "partsupp"
        }

    # Direct foreign-key edges agree exactly on join semantics (type names and
    # direction are deliberately ignored - those are the LLM pass's concern).
    assert direct_triples(generated.mapping) == direct_triples(shipped)


def test_generated_tpch_junction_edge_matches_shipped(tpch_ddl: str) -> None:
    generated = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres")).mapping
    supplies = next(e for e in generated.edges if e.source_table == "partsupp")
    assert {supplies.source_node, supplies.target_node} == {"Supplier", "Part"}
    assert set(supplies.properties) == {"availqty", "supplycost", "comment"}


def test_bundled_tpch_ddl_matches_shipped(examples_dir: Path, mappings_dir: Path) -> None:
    # The bundled example (examples/ddl/tpch.sql) is what the CLI and the web modal's
    # "Load example" feed in. It must parse with NO dialect (the modal's "Generic
    # SQL" default) and stay structurally faithful to the hand-authored tpch.yaml,
    # so the example can't silently drift from the mapping it is meant to reproduce.
    ddl_path = examples_dir / "ddl" / "tpch.sql"
    generated = project_to_mapping(extract_schema_from_ddl(ddl_path.read_text())).mapping  # dialect=None
    shipped = SchemaMapping.from_yaml(mappings_dir / "tpch.yaml")

    assert {n.source_table for n in generated.nodes} == {n.source_table for n in shipped.nodes}

    def direct_triples(mapping: SchemaMapping) -> set[tuple[str, str, str]]:
        return {
            (e.source_table, e.source_foreign_key, e.target_primary_key)
            for e in mapping.edges
            if e.source_table != "partsupp"
        }

    assert direct_triples(generated) == direct_triples(shipped)


def test_bundled_ldbc_ddl_matches_shipped(examples_dir: Path, mappings_dir: Path) -> None:
    # The gold DDL (examples/ddl/ldbc.sql), built deterministically with NO LLM,
    # must reproduce the official LDBC structure captured in the curated ldbc.yaml:
    # the same 8 node tables, the same 23 edge join triples, and Person's two
    # multi-valued attributes as list properties. This guards the ldbc.sql <->
    # ldbc.yaml correspondence against silent drift.
    generated = project_to_mapping(
        extract_schema_from_ddl((examples_dir / "ddl" / "ldbc.sql").read_text(), dialect="postgres")
    ).mapping
    shipped = SchemaMapping.from_yaml(mappings_dir / "ldbc.yaml")

    assert len(generated.nodes) == 8
    assert len(generated.edges) == 23
    assert {n.source_table for n in generated.nodes} == {n.source_table for n in shipped.nodes}

    def triples(mapping: SchemaMapping) -> set[tuple[str, str, str]]:
        return {(e.source_table, e.source_foreign_key, e.target_primary_key) for e in mapping.edges}

    # Join semantics agree exactly (edge type names/directions aside).
    assert triples(generated) == triples(shipped)

    def list_sides(mapping: SchemaMapping) -> set[tuple[str, str, str]]:
        return {(lp.source_table, lp.foreign_key, lp.column) for n in mapping.nodes for lp in n.list_properties.values()}

    assert list_sides(generated) == list_sides(shipped) == {
        ("person_email", "person_id", "email"),
        ("person_speaks", "person_id", "language"),
    }

    # The forum-post containment FK carries ON DELETE CASCADE, so the builder emits
    # the LDBC-correct Forum -> Post direction (not the default Post -> Forum).
    containment = next(e for e in generated.edges if e.source_table == "post" and e.source_foreign_key == "forum_id")
    assert (containment.source_node, containment.target_node) == ("Forum", "Post")


def test_bundled_ldbc_naive_ddl_reverses_containment(examples_dir: Path) -> None:
    # The naive twin drops ON DELETE CASCADE from post.forum_id; with no composition
    # signal the builder falls back to Post -> Forum -- the sole divergence from LDBC.
    generated = project_to_mapping(
        extract_schema_from_ddl((examples_dir / "ddl" / "ldbc_naive.sql").read_text(), dialect="postgres")
    ).mapping
    assert len(generated.nodes) == 8
    assert len(generated.edges) == 23
    containment = next(e for e in generated.edges if e.source_table == "post" and e.source_foreign_key == "forum_id")
    assert (containment.source_node, containment.target_node) == ("Post", "Forum")


@pytest.mark.parametrize("name", ["tpch.yaml", "ldbc.yaml"])
def test_shipped_mapping_round_trips(name: str, mappings_dir: Path) -> None:
    mapping = SchemaMapping.from_yaml(mappings_dir / name)
    assert SchemaMapping.from_yaml_string(mapping_to_yaml(mapping)) == mapping


def test_generated_mapping_round_trips(tpch_ddl: str) -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres")).mapping
    assert SchemaMapping.from_yaml_string(mapping_to_yaml(mapping)) == mapping


def test_list_properties_serialize_and_round_trip() -> None:
    mapping = project_to_mapping(
        extract_schema_from_ddl(
            """
            CREATE TABLE person (id INT PRIMARY KEY, name TEXT);
            CREATE TABLE person_email (person_id INT REFERENCES person(id), email TEXT, PRIMARY KEY (person_id, email));
            """,
            dialect="postgres",
        )
    ).mapping
    yaml_text = mapping_to_yaml(mapping)
    assert "list_properties:" in yaml_text
    assert "person_email" in yaml_text
    assert SchemaMapping.from_yaml_string(yaml_text) == mapping  # exact round-trip


def test_empty_edge_properties_are_omitted(tpch_ddl: str) -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres")).mapping
    yaml_text = mapping_to_yaml(mapping)
    # The BELONGS_TO-style direct edges carry no properties; partsupp does.
    assert "availqty" in yaml_text
    # A direct edge block should not emit an empty "properties: {}".
    assert "properties: {}" not in yaml_text
