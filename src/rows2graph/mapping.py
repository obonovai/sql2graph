"""Schema mapping between a relational source and a property-graph target.

This module defines the Pydantic models that the framework uses to describe a
relational-to-graph schema mapping. The mapping is the only piece of user input
that the framework needs to interpret semantically: the LLM consumes it as
part of the system prompt to translate SQL queries into the target graph query
language.

The mapping is intentionally minimal: each relational table either becomes a
graph **node label** (with column-to-property assignments and a primary key)
or it materialises a graph **edge type** (joining a foreign-key column in one
table to a primary-key column in another). Junction tables become edges with
properties drawn from their non-foreign-key columns; self-referential edges
are permitted by allowing ``source_node == target_node``.

Cross-field invariants (for example, that every edge's ``source_node`` and
``target_node`` refers to a label defined in ``nodes``) are enforced as
Pydantic ``model_validator`` checks at load time, so malformed YAML fails fast
with a precise error message rather than silently misleading the LLM.

The schema mapping is *deployment-invariant*: the same mapping can drive a
translation against any LLM provider and any deployed graph database. LLM
provider settings live in ``rows2graph.llm``; graph database connection
settings live in ``rows2graph.validators``. Keeping these orthogonal
concerns in separate modules (and separate YAML files) is the central
architectural commitment of this refactor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    """Base model with ``extra='forbid'``.

    Strictness here is a debugging affordance: an unknown YAML field
    typically signals a typo that would otherwise silently mis-map a column
    or fall back to a default value. We prefer a Pydantic ``ValidationError``
    that names the offending field.
    """

    model_config = ConfigDict(extra="forbid")


class NodeMapping(_StrictModel):
    """A single graph node label, sourced from one relational table.

    Attributes:
        label: The vertex label used verbatim in generated graph queries
            (e.g. ``(:Person)`` in Cypher, or the vertex collection name in
            AQL).
        source_table: The relational table that supplies rows for this label.
            The LLM sees this name in the system prompt but does not query the
            table directly.
        properties: Mapping from graph property name (key) to SQL column name
            (value). The LLM is instructed to use the *key* in generated
            queries.
        primary_key: SQL column that uniquely identifies rows in
            ``source_table``. Used by the LLM to reason about joins.
    """

    label: str
    source_table: str
    properties: dict[str, str]
    primary_key: str


class EdgeMapping(_StrictModel):
    """A single graph edge type, materialised from a relational foreign key.

    The edge connects rows in ``source_node``'s table to rows in
    ``target_node``'s table via a join on
    ``source_table.source_foreign_key = target_primary_key``. If
    ``source_table`` is a dedicated junction table (e.g. ``forum_person``,
    ``partsupp``), additional non-FK columns can become edge properties.

    Self-references (``source_node == target_node``) are supported (e.g.
    ``Person -[:KNOWS]-> Person``, ``Person -[:MANAGES]-> Person``).

    Attributes:
        type: Relationship type used verbatim in generated queries
            (e.g. ``[:KNOWS]``).
        source_node: ``label`` of an existing node, checked at load time.
        target_node: ``label`` of an existing node, checked at load time.
        source_table: Table containing the foreign key that materialises the
            edge.
        source_foreign_key: Foreign key column in ``source_table``.
        target_primary_key: Primary key column in the target node's table.
        properties: Optional edge properties, same format as node properties.
    """

    type: str
    source_node: str
    target_node: str
    source_table: str
    source_foreign_key: str
    target_primary_key: str
    properties: dict[str, str] = Field(default_factory=dict)


class SchemaMapping(_StrictModel):
    """The full relational-to-graph schema mapping.

    The YAML file at ``config/mappings/<name>.yaml`` deserialises directly
    into this class. There is no top-level ``schema_mapping:`` wrapper. This
    keeps the YAML file purely about the mapping; orthogonal concerns
    (LLM, server, translation loop) live elsewhere.

    Cross-field validation in :meth:`validate_edge_references` rejects edges
    that point at undeclared node labels. This shifts a class of common typos
    from "LLM hallucinates a label that fails server-side validation N
    iterations later" to "config load raises immediately".
    """

    nodes: list[NodeMapping]
    edges: list[EdgeMapping]

    def source_tables(self) -> set[str]:
        """Every relational table this mapping covers (node + edge sources).

        Returned with each table's casing exactly as written in the mapping.
        Coverage comparisons against a SQL query are case-insensitive and live
        in :func:`rows2graph.preflight.find_unmapped_tables`, not here.
        """
        return {n.source_table for n in self.nodes} | {e.source_table for e in self.edges}

    @model_validator(mode="after")
    def validate_edge_references(self) -> Self:
        """Reject edges whose ``source_node``/``target_node`` is undeclared."""
        node_labels = {n.label for n in self.nodes}
        for edge in self.edges:
            if edge.source_node not in node_labels:
                raise ValueError(f"Edge '{edge.type}' references undefined source_node '{edge.source_node}'")
            if edge.target_node not in node_labels:
                raise ValueError(f"Edge '{edge.type}' references undefined target_node '{edge.target_node}'")
        return self

    @classmethod
    def from_yaml(cls, path: Path | str) -> Self:
        """Load a schema mapping from a YAML file.

        The file is parsed with ``yaml.safe_load`` and then validated by
        Pydantic. Any unknown keys, missing required fields, or unresolved
        edge references raise ``pydantic.ValidationError`` with a precise
        location.
        """
        with open(path) as f:
            return cls.from_yaml_string(f.read())

    @classmethod
    def from_yaml_string(cls, text: str) -> Self:
        """Load a schema mapping from a YAML string (e.g. a textarea or HTTP body).

        Same parse-and-validate path as :meth:`from_yaml`, but from an in-memory
        string rather than a file: ``yaml.safe_load`` then Pydantic validation, so
        malformed YAML raises ``yaml.YAMLError`` and a structurally invalid mapping
        raises ``pydantic.ValidationError``.
        """
        data = yaml.safe_load(text)
        return cls.model_validate(data)
