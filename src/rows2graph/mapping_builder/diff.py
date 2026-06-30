"""Diff two mappings to show exactly what the LLM refinement renamed.

The refinement pass may only change node labels, edge types, and property keys
(never SQL identifiers - see :mod:`rows2graph.mapping_builder.refine`). That
constraint is what makes a clean, small "what the AI changed" view possible: we
match each entity by the identifiers refinement cannot touch and report only the
graph-facing names that differ.

Matching keys:
* nodes by ``source_table``,
* edges by ``(source_table, source_foreign_key, target_primary_key)``,
* properties by their SQL-column *value*.

Matching is conservative: an entity whose key is not unique on either side is
skipped rather than guessed, so the diff never reports a spurious rename.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rows2graph.mapping import EdgeMapping, NodeMapping, SchemaMapping


@dataclass(frozen=True)
class RenameDiff:
    """One graph-facing name the refinement changed."""

    kind: str  # "node label" | "edge type" | "property"
    where: str  # context, e.g. the source table, the join column, or "Label.column"
    before: str
    after: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "where": self.where, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class MappingDiff:
    """All the renames between a deterministic skeleton and its refined version."""

    label_renames: list[RenameDiff]
    edge_type_renames: list[RenameDiff]
    property_renames: list[RenameDiff]

    def is_empty(self) -> bool:
        return not (self.label_renames or self.edge_type_renames or self.property_renames)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label_renames": [r.as_dict() for r in self.label_renames],
            "edge_type_renames": [r.as_dict() for r in self.edge_type_renames],
            "property_renames": [r.as_dict() for r in self.property_renames],
        }


def diff_mappings(before: SchemaMapping, after: SchemaMapping) -> MappingDiff:
    """Return the renames the refinement applied going from *before* to *after*."""
    return MappingDiff(
        label_renames=_label_renames(before, after),
        edge_type_renames=_edge_type_renames(before, after),
        property_renames=_property_renames(before, after),
    )


def _label_renames(before: SchemaMapping, after: SchemaMapping) -> list[RenameDiff]:
    after_by_table = _node_index(after.nodes)
    out: list[RenameDiff] = []
    for node in before.nodes:
        match = after_by_table.get(node.source_table.casefold())
        if match is not None and match.label != node.label:
            out.append(RenameDiff(kind="node label", where=node.source_table, before=node.label, after=match.label))
    return out


def _edge_type_renames(before: SchemaMapping, after: SchemaMapping) -> list[RenameDiff]:
    after_index = _edge_index(after.edges)
    out: list[RenameDiff] = []
    for key, edge in _edge_index(before.edges).items():
        match = after_index.get(key)
        if match is not None and match.type != edge.type:
            where = f"{edge.source_table}.{edge.source_foreign_key}"
            out.append(RenameDiff(kind="edge type", where=where, before=edge.type, after=match.type))
    return out


def _property_renames(before: SchemaMapping, after: SchemaMapping) -> list[RenameDiff]:
    out: list[RenameDiff] = []
    after_nodes = _node_index(after.nodes)
    for node in before.nodes:
        match = after_nodes.get(node.source_table.casefold())
        if match is not None:
            out.extend(_prop_renames(node.properties, match.properties, where=match.label))
    after_edges = _edge_index(after.edges)
    for key, edge in _edge_index(before.edges).items():
        match_edge = after_edges.get(key)
        if match_edge is not None:
            out.extend(_prop_renames(edge.properties, match_edge.properties, where=match_edge.type))
    return out


def _node_index(nodes: list[NodeMapping]) -> dict[str, NodeMapping]:
    """Index nodes by ``source_table``, dropping non-unique keys (mirrors :func:`_edge_index`)."""
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node.source_table.casefold()] = counts.get(node.source_table.casefold(), 0) + 1
    return {n.source_table.casefold(): n for n in nodes if counts[n.source_table.casefold()] == 1}


def _edge_index(edges: list[EdgeMapping]) -> dict[tuple[str, str, str], EdgeMapping]:
    """Index edges by the join identity refinement preserves, dropping non-unique keys."""
    counts: dict[tuple[str, str, str], int] = {}
    for edge in edges:
        counts[_edge_key(edge)] = counts.get(_edge_key(edge), 0) + 1
    return {_edge_key(e): e for e in edges if counts[_edge_key(e)] == 1}


def _edge_key(edge: EdgeMapping) -> tuple[str, str, str]:
    return (edge.source_table.casefold(), edge.source_foreign_key.casefold(), edge.target_primary_key.casefold())


def _prop_renames(before_props: dict[str, str], after_props: dict[str, str], *, where: str) -> list[RenameDiff]:
    """Compare property maps by their SQL-column value (which refinement keeps)."""
    after_by_value = _invert_unique(after_props)
    before_value_counts: dict[str, int] = {}
    for value in before_props.values():
        before_value_counts[value.casefold()] = before_value_counts.get(value.casefold(), 0) + 1
    out: list[RenameDiff] = []
    for before_key, value in before_props.items():
        if before_value_counts[value.casefold()] != 1:
            continue  # ambiguous column on the before side; skip rather than guess
        after_key = after_by_value.get(value.casefold())
        if after_key is not None and after_key != before_key:
            out.append(RenameDiff(kind="property", where=f"{where}.{value}", before=before_key, after=after_key))
    return out


def _invert_unique(props: dict[str, str]) -> dict[str, str]:
    """Map casefolded column value -> property key, dropping non-unique values."""
    counts: dict[str, int] = {}
    for value in props.values():
        counts[value.casefold()] = counts.get(value.casefold(), 0) + 1
    return {value.casefold(): key for key, value in props.items() if counts[value.casefold()] == 1}
