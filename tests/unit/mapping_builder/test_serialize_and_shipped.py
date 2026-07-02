"""Closeness to the shipped tpch.yaml and YAML serialization round-tripping."""

from __future__ import annotations

from pathlib import Path

import pytest

from rows2graph import SchemaMapping
from rows2graph.mapping_builder import mapping_to_yaml
from rows2graph.mapping_builder.ddl import extract_schema_from_ddl
from rows2graph.mapping_builder.project import project_to_mapping


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


@pytest.mark.parametrize("name", ["tpch.yaml", "ldbc.yaml"])
def test_shipped_mapping_round_trips(name: str, mappings_dir: Path) -> None:
    mapping = SchemaMapping.from_yaml(mappings_dir / name)
    assert SchemaMapping.from_yaml_string(mapping_to_yaml(mapping)) == mapping


def test_generated_mapping_round_trips(tpch_ddl: str) -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres")).mapping
    assert SchemaMapping.from_yaml_string(mapping_to_yaml(mapping)) == mapping


def test_empty_edge_properties_are_omitted(tpch_ddl: str) -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres")).mapping
    yaml_text = mapping_to_yaml(mapping)
    # The BELONGS_TO-style direct edges carry no properties; partsupp does.
    assert "availqty" in yaml_text
    # A direct edge block should not emit an empty "properties: {}".
    assert "properties: {}" not in yaml_text
