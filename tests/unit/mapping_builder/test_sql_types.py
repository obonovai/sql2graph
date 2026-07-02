"""Property data types: SQL-type mapping, serialization, prompt rendering."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rows2graph import NodeMapping, SchemaMapping, SemanticType
from rows2graph.mapping_builder import mapping_to_yaml
from rows2graph.mapping_builder.ddl import extract_schema_from_ddl
from rows2graph.mapping_builder.refine import refine_mapping
from rows2graph.mapping_builder.sql_types import semantic_type_for_sql
from rows2graph.prompts import build_system_prompt, format_schema_context
from rows2graph.sql_features import SqlFeature
from rows2graph.targets import make_target


def test_semantic_type_for_sql_maps_families() -> None:
    cases = {
        "DATE": SemanticType.DATE,
        "TIMESTAMP": SemanticType.DATETIME,
        "TIMESTAMPTZ": SemanticType.DATETIME,
        "DATETIME": SemanticType.DATETIME,
        "TIME": SemanticType.TIME,
        "INTERVAL": SemanticType.DURATION,
        "INT": SemanticType.INTEGER,
        "BIGINT": SemanticType.INTEGER,
        "DECIMAL(15,2)": SemanticType.FLOAT,
        "DOUBLE": SemanticType.FLOAT,
        "BOOLEAN": SemanticType.BOOLEAN,
        "VARCHAR(25)": SemanticType.STRING,
        "CHAR(1)": SemanticType.STRING,
        "TEXT": SemanticType.STRING,
    }
    for sql, expected in cases.items():
        assert semantic_type_for_sql(sql) is expected, sql
    # Unknown / exotic / missing types are left untyped, not guessed.
    for unknown in ("UUID", "JSON", "GEOMETRY", "", None):
        assert semantic_type_for_sql(unknown) is None, unknown


def test_projection_derives_property_types(tpch_skeleton: Callable[..., Any]) -> None:
    mapping = tpch_skeleton()
    orders = next(n for n in mapping.nodes if n.source_table == "orders")
    assert orders.property_types["orderdate"] is SemanticType.DATE
    assert orders.property_types["totalprice"] is SemanticType.FLOAT
    assert orders.property_types["comment"] is SemanticType.STRING
    # A foreign-key column becomes an edge, not a property, so it carries no type.
    assert "custkey" not in orders.properties
    assert "custkey" not in orders.property_types
    # A junction edge's own (non-FK) column is typed too.
    partsupp = next(e for e in mapping.edges if e.source_table == "partsupp")
    assert partsupp.property_types["availqty"] is SemanticType.INTEGER


def test_typed_property_yaml_round_trips_both_forms() -> None:
    text = (
        "nodes:\n"
        "  - label: Event\n"
        "    source_table: event\n"
        "    properties:\n"
        "      id: id\n"  # short form, untyped
        "      startsAt:\n"  # long form, typed
        "        column: starts_at\n"
        "        type: datetime\n"
        "    primary_key: id\n"
        "edges: []\n"
    )
    mapping = SchemaMapping.from_yaml_string(text)
    node = mapping.nodes[0]
    assert node.properties == {"id": "id", "startsAt": "starts_at"}
    assert node.property_types == {"startsAt": SemanticType.DATETIME}
    # Round-trips through the serializer and stays equal.
    assert SchemaMapping.from_yaml_string(mapping_to_yaml(mapping)) == mapping
    # The untyped property stays short-form; the typed one is long-form.
    out = mapping_to_yaml(mapping)
    assert "id: id" in out
    assert "column: starts_at" in out and "type: datetime" in out


def test_untyped_mapping_serializes_short_form(mappings_dir: Path) -> None:
    # A mapping with no types emits only bare `name: column` values (byte-compat).
    mapping = SchemaMapping.from_yaml(mappings_dir / "tpch.yaml")
    out = mapping_to_yaml(mapping)
    assert "column:" not in out
    assert all(not n.property_types for n in mapping.nodes)
    assert all(not e.property_types for e in mapping.edges)


def test_property_types_reject_orphan_key() -> None:
    with pytest.raises(ValidationError):
        NodeMapping(
            label="X",
            source_table="x",
            properties={"a": "a"},
            property_types={"missing": SemanticType.DATE},
            primary_key="a",
        )


def test_long_form_property_rejects_bad_shape() -> None:
    text = (
        "nodes:\n"
        "  - label: Event\n"
        "    source_table: event\n"
        "    properties:\n"
        "      id: {column: id, note: oops}\n"  # unknown inner key
        "    primary_key: id\n"
        "edges: []\n"
    )
    with pytest.raises(ValidationError):
        SchemaMapping.from_yaml_string(text)


def test_prompt_renders_type_and_untyped_is_unchanged() -> None:
    typed = SchemaMapping.from_yaml_string(
        "nodes:\n"
        "  - label: Event\n"
        "    source_table: event\n"
        "    properties:\n"
        "      startsAt:\n"
        "        column: starts_at\n"
        "        type: datetime\n"
        "    primary_key: startsAt\n"
        "edges: []\n"
    )
    assert "`startsAt` <- SQL column `starts_at` (datetime)" in format_schema_context(typed)
    # An untyped variant of the same schema renders exactly as before (no suffix).
    untyped = SchemaMapping.from_yaml_string(
        "nodes:\n"
        "  - label: Event\n"
        "    source_table: event\n"
        "    properties:\n"
        "      startsAt: starts_at\n"
        "    primary_key: startsAt\n"
        "edges: []\n"
    )
    untyped_ctx = format_schema_context(untyped)
    assert "`startsAt` <- SQL column `starts_at`" in untyped_ctx
    assert "(datetime)" not in untyped_ctx


def test_cypher_temporal_rule_pins_declared_datetime(mappings_dir: Path) -> None:
    ldbc = SchemaMapping.from_yaml(mappings_dir / "ldbc.yaml")
    prompt = build_system_prompt(ldbc, make_target("cypher"), frozenset({SqlFeature.TEMPORAL}))
    # The schema block shows the type and the temporal rule pins the q02 fix.
    assert "(datetime)" in prompt
    assert "datetime('2010-06-01T00:00:00')" in prompt
    assert "PREFER the property" in prompt


def test_model_dump_json_serialises_types_as_strings(mappings_dir: Path) -> None:
    # The web layer ships mapping.model_dump() as JSON; a SemanticType must survive
    # as a plain string so the frontend reads `property_types` without special-casing.
    mapping = SchemaMapping.from_yaml(mappings_dir / "ldbc.yaml")
    reloaded = json.loads(json.dumps(mapping.model_dump()))
    post = next(n for n in reloaded["nodes"] if n["label"] == "Post")
    assert post["property_types"]["creationDate"] == "datetime"


def test_refine_preserves_property_type(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    # Renaming a property KEY while keeping {column, type} is allowed; dropping the
    # type is an SQL-side change and must be rejected in favour of the skeleton.
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")

    # Accept: rename the property key `orderdate` -> `placedOn`, keep column + type.
    renamed = mapping_to_yaml(skeleton).replace("orderdate:\n", "placedOn:\n")
    ok = refine_mapping(skeleton, schema, oneshot_llm(renamed))
    assert ok.accepted is True
    orders = next(n for n in ok.mapping.nodes if n.source_table == "orders")
    assert orders.property_types.get("placedOn") is SemanticType.DATE

    # Reject: drop orderdate's type (collapse it to an untyped property). Build the
    # candidate from the model so the test does not depend on YAML indentation.
    orders_skel = next(n for n in skeleton.nodes if n.source_table == "orders")
    untyped_types = {k: v for k, v in orders_skel.property_types.items() if k != "orderdate"}
    downgraded = orders_skel.model_copy(update={"property_types": untyped_types})
    candidate = SchemaMapping(
        nodes=[downgraded if n.source_table == "orders" else n for n in skeleton.nodes],
        edges=list(skeleton.edges),
    )
    bad = refine_mapping(skeleton, schema, oneshot_llm(mapping_to_yaml(candidate)))
    assert bad.accepted is False
    assert bad.mapping == skeleton
