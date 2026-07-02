"""LLM refinement guardrail, mapping diffs, and schema validation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from rows2graph import EdgeMapping, NodeMapping, SchemaMapping
from rows2graph.mapping_builder import mapping_to_yaml
from rows2graph.mapping_builder.ddl import extract_schema_from_ddl
from rows2graph.mapping_builder.diff import diff_mappings
from rows2graph.mapping_builder.refine import refine_mapping, refine_mapping_async, validate_against_schema


def test_refine_applies_valid_rename(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    improved_yaml = mapping_to_yaml(skeleton).replace("HAS_REGION", "IN_REGION").replace("label: Lineitem", "label: LineItem")
    # keep edges that reference the renamed label consistent
    improved_yaml = improved_yaml.replace("source_node: Lineitem", "source_node: LineItem")
    outcome = refine_mapping(skeleton, schema, oneshot_llm(improved_yaml))
    assert outcome.accepted is True
    assert outcome.warnings == []
    assert "IN_REGION" in {e.type for e in outcome.mapping.edges}
    assert "LineItem" in {n.label for n in outcome.mapping.nodes}
    # the transcript carries the chat the modal renders
    roles = [m["role"] for m in outcome.messages]
    assert roles[:2] == ["system", "user"]
    assert "assistant" in roles


def test_refine_strips_code_fences(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    fenced = "```yaml\n" + mapping_to_yaml(skeleton) + "\n```"
    outcome = refine_mapping(skeleton, schema, oneshot_llm(fenced))
    assert outcome.accepted is True
    assert outcome.warnings == []
    assert outcome.mapping == skeleton


def test_refine_rejects_hallucinated_column_and_falls_back(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    bad = mapping_to_yaml(skeleton).replace("primary_key: regionkey", "primary_key: not_a_column")
    outcome = refine_mapping(skeleton, schema, oneshot_llm(bad))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("not_a_column" in w for w in outcome.warnings)
    # even a rejected attempt is shown in the transcript
    assert any(m["role"] == "assistant" for m in outcome.messages)


def test_refine_rejects_dropped_table_coverage_regression(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    # Drop the Region node entirely but keep edges valid by also dropping its edge.
    smaller = SchemaMapping(
        nodes=[n for n in skeleton.nodes if n.source_table != "region"],
        edges=[e for e in skeleton.edges if e.target_node != "Region"],
    )
    outcome = refine_mapping(skeleton, schema, oneshot_llm(mapping_to_yaml(smaller)))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("region" in w.lower() for w in outcome.warnings)


def test_refine_falls_back_on_malformed_yaml(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    outcome = refine_mapping(skeleton, schema, oneshot_llm("this: is: not: valid: mapping"))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert outcome.warnings  # explains the fallback


def test_refine_falls_back_when_llm_errors(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    outcome = refine_mapping(skeleton, schema, oneshot_llm(error=RuntimeError("boom")))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("boom" in w for w in outcome.warnings)
    # transcript still records what we tried to send
    assert [m["role"] for m in outcome.messages] == ["system", "user"]


def test_refine_rejects_swapped_foreign_key_column(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    # The LLM repoints an FK to another column that *exists* on the table. The
    # existence-only guardrail accepted this; the preservation check must reject it.
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    swapped = mapping_to_yaml(skeleton).replace("source_foreign_key: regionkey", "source_foreign_key: name")
    outcome = refine_mapping(skeleton, schema, oneshot_llm(swapped))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("SQL side changed" in w for w in outcome.warnings)


def test_refine_rejects_swapped_property_column(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    # A node property value is repointed from one real column to another real one.
    # (Typed properties serialise long-form, so the column lives on a `column:` line.)
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    swapped = mapping_to_yaml(skeleton).replace("column: name", "column: comment")
    outcome = refine_mapping(skeleton, schema, oneshot_llm(swapped))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton


def test_refine_rejects_added_edge(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    # A spurious but identifier-valid relationship the LLM invents must be rejected
    # (every column exists, so only the preservation check catches it).
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    with_extra = SchemaMapping(
        nodes=list(skeleton.nodes),
        edges=[
            *skeleton.edges,
            EdgeMapping(
                type="BOGUS",
                source_node="Supplier",
                target_node="Supplier",
                source_table="supplier",
                source_foreign_key="suppkey",
                target_primary_key="suppkey",
            ),
        ],
    )
    outcome = refine_mapping(skeleton, schema, oneshot_llm(mapping_to_yaml(with_extra)))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton


def test_diff_mappings_detects_label_and_edge_renames(tpch_skeleton: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    renamed = (
        mapping_to_yaml(skeleton)
        .replace("label: Lineitem", "label: LineItem")
        .replace("source_node: Lineitem", "source_node: LineItem")
        .replace("HAS_REGION", "IN_REGION")
    )
    diff = diff_mappings(skeleton, SchemaMapping.from_yaml_string(renamed))
    assert not diff.is_empty()
    assert ("Lineitem", "LineItem") in {(r.before, r.after) for r in diff.label_renames}
    assert ("HAS_REGION", "IN_REGION") in {(r.before, r.after) for r in diff.edge_type_renames}


def test_diff_mappings_detects_property_rename(tpch_skeleton: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    # Rename Region's property key 'name' -> 'title'; the SQL column value stays 'name'.
    after = SchemaMapping(
        nodes=[
            NodeMapping(
                label=n.label,
                source_table=n.source_table,
                primary_key=n.primary_key,
                properties=(
                    {("title" if k == "name" else k): v for k, v in n.properties.items()}
                    if n.source_table == "region"
                    else n.properties
                ),
            )
            for n in skeleton.nodes
        ],
        edges=list(skeleton.edges),
    )
    diff = diff_mappings(skeleton, after)
    assert any(r.before == "name" and r.after == "title" for r in diff.property_renames)


def test_diff_mappings_empty_for_identical(tpch_skeleton: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    assert diff_mappings(skeleton, skeleton).is_empty()


def test_validate_against_schema_flags_unknown_table(tpch_ddl: str) -> None:
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    bogus = SchemaMapping(
        nodes=[{"label": "X", "source_table": "ghost", "properties": {"k": "k"}, "primary_key": "k"}],  # type: ignore[list-item]
        edges=[],
    )
    violations = validate_against_schema(bogus, schema)
    assert any("ghost" in v for v in violations)


def test_refine_mapping_async_falls_back_when_llm_errors(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_async_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    schema = extract_schema_from_ddl(tpch_ddl, dialect="postgres")
    outcome = asyncio.run(refine_mapping_async(skeleton, schema, oneshot_async_llm(error=RuntimeError("boom"))))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("boom" in w for w in outcome.warnings)
