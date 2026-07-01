"""Render a :class:`~rows2graph.mapping.SchemaMapping` to YAML, and the audit to text.

The library has always *read* mapping YAML (``yaml.safe_load`` in
:meth:`SchemaMapping.from_yaml`); this is the first place it *writes* it. The
emitted YAML is deliberately the same shape a human authors by hand and round-
trips exactly through :meth:`SchemaMapping.from_yaml_string`, so a generated
mapping is indistinguishable from a written one and can be edited freely.
"""

from __future__ import annotations

from typing import Any

import yaml

from rows2graph.mapping import EdgeMapping, NodeMapping, SchemaMapping, SemanticType
from rows2graph.mapping_builder.project import CoverageReport


def mapping_to_yaml(mapping: SchemaMapping, *, header: str | None = None) -> str:
    """Serialise *mapping* to YAML in the canonical ``nodes:`` / ``edges:`` shape.

    Field order is preserved (``sort_keys=False``); an edge's ``properties`` block
    is emitted only when non-empty, matching the hand-authored examples. An
    optional *header* is rendered as leading ``#`` comment lines.
    """
    data: dict[str, Any] = {
        "nodes": [_node_dict(n) for n in mapping.nodes],
        "edges": [_edge_dict(e) for e in mapping.edges],
    }
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
    if header:
        comment = "\n".join(f"# {line}" if line else "#" for line in header.splitlines())
        return f"{comment}\n{body}"
    return body


def _props_out(properties: dict[str, str], property_types: dict[str, SemanticType]) -> dict[str, Any]:
    """Render the ``properties`` block, short form when untyped, long when typed.

    An untyped property stays a bare ``name: column`` string, so a mapping that
    never used types serialises byte-for-byte as before; a typed one round-trips
    as ``name: {column: ..., type: ...}``, which
    :func:`rows2graph.mapping._split_property_types` reads straight back.
    """
    out: dict[str, Any] = {}
    for name, column in properties.items():
        semantic = property_types.get(name)
        out[name] = {"column": column, "type": semantic.value} if semantic is not None else column
    return out


def _node_dict(node: NodeMapping) -> dict[str, Any]:
    return {
        "label": node.label,
        "source_table": node.source_table,
        "properties": _props_out(node.properties, node.property_types),
        "primary_key": node.primary_key,
    }


def _edge_dict(edge: EdgeMapping) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": edge.type,
        "source_node": edge.source_node,
        "target_node": edge.target_node,
        "source_table": edge.source_table,
        "source_foreign_key": edge.source_foreign_key,
        "target_primary_key": edge.target_primary_key,
    }
    if edge.properties:
        out["properties"] = _props_out(edge.properties, edge.property_types)
    return out


def format_audit_report(report: CoverageReport) -> str:
    """Render the coverage report as a human-readable block (for the CLI / stderr)."""
    lines: list[str] = []
    lines.append(f"Nodes: {len(report.node_tables)}  Edges: {len(report.fk_edges)}")
    if report.node_tables:
        lines.append("")
        lines.append("Node tables:")
        lines.extend(f"  - {t}" for t in report.node_tables)
    if report.edge_tables:
        lines.append("")
        lines.append("Junction tables collapsed to edges:")
        lines.extend(f"  - {t}" for t in report.edge_tables)
    if report.fk_edges:
        lines.append("")
        lines.append("Relationships:")
        lines.extend(f"  - {e}" for e in report.fk_edges)
    if report.synthesized_keys:
        lines.append("")
        lines.append("Synthesized primary keys (no key declared):")
        lines.extend(f"  - {t}" for t in report.synthesized_keys)
    if report.dropped_objects:
        lines.append("")
        lines.append("Dropped (not mapped):")
        lines.extend(f"  - {name}: {reason}" for name, reason in report.dropped_objects)
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in report.warnings)
    return "\n".join(lines)
