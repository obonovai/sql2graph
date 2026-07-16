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
provider settings live in ``sql2graph.llm``; graph database connection
settings live in ``sql2graph.validators``. Keeping these orthogonal
concerns in separate modules (and separate YAML files) is the central
architectural commitment of this refactor.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class _StrictModel(BaseModel):
    """Base model with ``extra='forbid'``.

    Strictness here is a debugging affordance: an unknown YAML field
    typically signals a typo that would otherwise silently mis-map a column
    or fall back to a default value. We prefer a Pydantic ``ValidationError``
    that names the offending field.
    """

    model_config = ConfigDict(extra="forbid")


# Required mapping identifiers (labels, table/column names, property names) must
# be non-empty. Without this, Pydantic accepts "" and "   " as valid ``str``: an
# empty ``primary_key`` or ``label`` would load fine yet silently break the
# system prompt and the column-coverage pre-flight. ``strip_whitespace`` also
# trims accidental surrounding spaces from names.
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# A key that is one or more columns. Accepts a scalar string or a list in YAML
# (normalized to a list by the models' ``mode="before"`` validators); an empty
# list is rejected, and each element must be a non-blank column name. Composite
# keys keep every column so a node's identity and an edge's join are not lossily
# reduced to a single column.
KeyColumns = Annotated[list[NonBlankStr], Field(min_length=1)]


def _as_key_list(value: Any) -> Any:
    """Normalize a scalar-or-list key field to a list, for a ``mode="before"`` validator.

    Accepts the legacy scalar form (``primary_key: id``) and the composite list
    form (``primary_key: [orderkey, linenumber]``), returning a list either way so
    the stored value is always a list. A non-str/non-list value is passed through
    untouched for Pydantic to reject with its normal type error.
    """
    return [value] if isinstance(value, str) else value


class SemanticType(StrEnum):
    """A normalized, loader-agnostic semantic type for a graph property.

    This is deliberately a small, closed vocabulary rather than a raw SQL type
    (``VARCHAR(25)``) or a physical graph-storage type (native ``DateTime`` vs
    epoch-millis ``Long``). It records what the value *means* - a fact about the
    data that holds regardless of which loader wrote the graph - and leaves the
    physical rendering to each target language. The translator surfaces it in
    the system prompt so the LLM no longer has to *guess* a property's type from
    the shape of a SQL literal (the guess that made a ``datetime`` column compare
    against ``date('...')`` and silently evaluate to null).

    The value is best-effort: the builder derives it from the source SQL type
    where it can (see
    :func:`sql2graph.mapping_builder.sql_types.semantic_type_for_sql`) and
    leaves a property untyped when it cannot. It stays overridable by hand.
    """

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    DURATION = "duration"


def _split_property_types(data: Any) -> Any:
    """Normalize the two accepted ``properties`` YAML shapes into two flat dicts.

    A property value may be written short (``name: "column"``) or long
    (``name: {column: "column", type: "datetime"}``). The long form is split so
    ``properties`` always ends up ``{name: column}`` and any declared type moves
    into a parallel ``property_types`` ``{name: type}``. This runs as a
    ``mode="before"`` validator; when every value is already a bare string (the
    common case, and every mapping authored before this feature) it is a no-op,
    so untyped YAML and direct Python constructors pass through untouched.
    """
    if not isinstance(data, dict) or "properties" not in data:
        return data
    props = data["properties"]
    if not isinstance(props, dict) or not any(isinstance(v, dict) for v in props.values()):
        return data  # not the long form: leave the no-op (or field validation) to Pydantic
    flat: dict[Any, Any] = {}
    types: dict[Any, Any] = dict(data.get("property_types") or {})
    for name, value in props.items():
        if isinstance(value, dict):
            unexpected = set(value) - {"column", "type"}
            if unexpected or "column" not in value:
                raise ValueError(
                    f"property '{name}': expected a column string or a {{column, type}} "
                    f"object, got keys {sorted(value)}"
                )
            flat[name] = value["column"]
            if value.get("type") is not None:
                types[name] = value["type"]
        else:
            flat[name] = value
    return {**data, "properties": flat, "property_types": types}


def _check_property_types_subset(properties: dict[str, str], property_types: dict[str, SemanticType]) -> None:
    """Reject a type annotation whose key is not a declared property."""
    orphan = set(property_types) - set(properties)
    if orphan:
        raise ValueError(f"property_types keys {sorted(orphan)} are not declared properties")


class ListProperty(_StrictModel):
    """A multi-valued node property materialised from a child table.

    LDBC-style multi-valued attributes - the ``email`` addresses a Person owns,
    the ``language``s they ``speak`` - are stored relationally in a dedicated
    child table keyed by the parent's id (``person_email(person_id, email)``,
    ``person_speaks(person_id, language)``). In the property graph they are a
    **list property on the parent node**, not separate nodes: the scalar
    :class:`NodeMapping.properties` shape (one graph property <- one column of the
    node's own ``source_table``) cannot express them, so a :class:`NodeMapping`
    declares one :class:`ListProperty` per multi-valued attribute instead.

    Attributes:
        source_table: The child table holding the values (e.g. ``person_email``).
        foreign_key: The child-table column referencing the parent node's key
            (e.g. ``person_id``); the join is ``source_table.foreign_key =
            <parent>.primary_key``.
        column: The child-table column holding each element value (e.g. ``email``).
        type: Optional :class:`SemanticType` of each element, surfaced in the
            prompt so the LLM knows the element type. ``None`` when untyped.
    """

    source_table: NonBlankStr
    foreign_key: NonBlankStr
    column: NonBlankStr
    type: SemanticType | None = None


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
        property_types: Optional mapping from graph property name to a
            :class:`SemanticType`, surfaced in the prompt so the LLM knows a
            value's type instead of guessing it. Empty when untyped.
        list_properties: Optional mapping from graph property name to a
            :class:`ListProperty` describing a multi-valued attribute sourced
            from a child table (e.g. ``email``, ``language``). Empty for the
            common single-table node.
        primary_key: One or more SQL columns that identify rows in
            ``source_table`` (a composite key keeps all its columns, e.g.
            ``[orderkey, linenumber]``). Accepts a scalar string or a list in
            YAML; stored as a list. Used by the LLM to reason about joins.
    """

    label: NonBlankStr
    source_table: NonBlankStr
    properties: dict[NonBlankStr, NonBlankStr]
    property_types: dict[NonBlankStr, SemanticType] = Field(default_factory=dict)
    list_properties: dict[NonBlankStr, ListProperty] = Field(default_factory=dict)
    primary_key: KeyColumns

    @model_validator(mode="before")
    @classmethod
    def _normalize_before(cls, data: Any) -> Any:
        """Split typed properties, then normalize ``primary_key`` to a list."""
        data = _split_property_types(data)
        if isinstance(data, dict) and "primary_key" in data:
            data = {**data, "primary_key": _as_key_list(data["primary_key"])}
        return data

    @model_validator(mode="after")
    def _validate_property_types(self) -> Self:
        _check_property_types_subset(self.properties, self.property_types)
        return self

    @model_validator(mode="after")
    def _reject_property_name_clash(self) -> Self:
        """A graph property name must be either scalar or a list, not both."""
        clash = set(self.properties) & set(self.list_properties)
        if clash:
            raise ValueError(f"property name(s) declared as both scalar and list: {sorted(clash)}")
        return self


class EdgeMapping(_StrictModel):
    """A single graph edge type, materialised from a relational foreign key.

    The edge connects rows in ``source_node``'s table to rows in
    ``target_node``'s table via a join on
    ``source_table.source_foreign_key = target_primary_key``. Both key fields may
    hold more than one column for a composite join, positionally matched
    (``source.fk[i] = target.pk[i]``) and required to be the same length. If
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
        source_foreign_key: Foreign-key column(s) in ``source_table`` (a scalar
            or a list in YAML; stored as a list).
        target_primary_key: Primary-key column(s) in the target node's table,
            positionally matched to ``source_foreign_key`` (same length).
        properties: Optional edge properties, same format as node properties.
        property_types: Optional per-property :class:`SemanticType`, same
            meaning as :attr:`NodeMapping.property_types`.
    """

    type: NonBlankStr
    source_node: NonBlankStr
    target_node: NonBlankStr
    source_table: NonBlankStr
    source_foreign_key: KeyColumns
    target_primary_key: KeyColumns
    properties: dict[NonBlankStr, NonBlankStr] = Field(default_factory=dict)
    property_types: dict[NonBlankStr, SemanticType] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_before(cls, data: Any) -> Any:
        """Split typed properties, then normalize both join-key fields to lists."""
        data = _split_property_types(data)
        if isinstance(data, dict):
            merged = dict(data)
            for field_name in ("source_foreign_key", "target_primary_key"):
                if field_name in merged:
                    merged[field_name] = _as_key_list(merged[field_name])
            return merged
        return data

    @model_validator(mode="after")
    def _validate_property_types(self) -> Self:
        _check_property_types_subset(self.properties, self.property_types)
        return self

    @model_validator(mode="after")
    def _validate_join_arity(self) -> Self:
        """A composite join must pair the same number of source and target columns."""
        if len(self.source_foreign_key) != len(self.target_primary_key):
            raise ValueError(
                f"edge '{self.type}': source_foreign_key has {len(self.source_foreign_key)} "
                f"column(s) but target_primary_key has {len(self.target_primary_key)}; a "
                f"composite join must be positionally length-matched (source.fk[i] = target.pk[i])"
            )
        return self


class SchemaMapping(_StrictModel):
    """The full relational-to-graph schema mapping.

    The YAML file at ``examples/mappings/<name>.yaml`` deserialises directly
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
        Includes the child tables backing multi-valued list properties, so a SQL
        query reading ``person_email`` is not flagged as unmapped. Coverage
        comparisons against a SQL query are case-insensitive and live in
        :func:`sql2graph.engine.preflight.find_unmapped_tables`, not here.
        """
        return (
            {n.source_table for n in self.nodes}
            | {e.source_table for e in self.edges}
            | {lp.source_table for n in self.nodes for lp in n.list_properties.values()}
        )

    def node_labels(self) -> set[str]:
        """The set of declared graph node labels."""
        return {n.label for n in self.nodes}

    def edge_types(self) -> set[str]:
        """The set of declared graph relationship types."""
        return {e.type for e in self.edges}

    def properties_for_label(self, label: str) -> set[str]:
        """Graph property names declared for ``label`` (empty if the label is unknown)."""
        return {prop for n in self.nodes if n.label == label for prop in n.properties}

    def properties_for_edge(self, edge_type: str) -> set[str]:
        """Graph property names declared for ``edge_type`` (empty if it is unknown)."""
        return {prop for e in self.edges if e.type == edge_type for prop in e.properties}

    @model_validator(mode="after")
    def _reject_duplicate_node_labels(self) -> Self:
        """Reject two nodes sharing a ``label``: the label would be ambiguous.

        The set built in :meth:`validate_edge_references` silently de-duplicates,
        so without this a duplicate label loads fine and the LLM sees two
        conflicting definitions for the same node.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for node in self.nodes:
            if node.label in seen and node.label not in duplicates:
                duplicates.append(node.label)
            seen.add(node.label)
        if duplicates:
            raise ValueError(f"Duplicate node label(s): {', '.join(sorted(duplicates))}")
        return self

    @model_validator(mode="after")
    def validate_edge_references(self) -> Self:
        """Reject edges whose ``source_node``/``target_node`` is undeclared."""
        labels = self.node_labels()
        for edge in self.edges:
            if edge.source_node not in labels:
                raise ValueError(f"Edge '{edge.type}' references undefined source_node '{edge.source_node}'")
            if edge.target_node not in labels:
                raise ValueError(f"Edge '{edge.type}' references undefined target_node '{edge.target_node}'")
        return self

    @model_validator(mode="after")
    def _reject_duplicate_edges(self) -> Self:
        """Reject fully-identical edges (a copy-paste slip).

        Two edges that share ``type``/``source_node``/``target_node`` but differ
        in ``source_table`` (or the keys/properties) are allowed: that is a
        legitimate multi-junction pattern (e.g. Person-LIKES-Post alongside
        Person-LIKES-Comment).
        """
        seen: set[tuple[str, str, str, str, tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]] = set()
        for edge in self.edges:
            key = (
                edge.type,
                edge.source_node,
                edge.target_node,
                edge.source_table,
                tuple(edge.source_foreign_key),
                tuple(edge.target_primary_key),
                tuple(sorted(edge.properties.items())),
            )
            if key in seen:
                raise ValueError(
                    f"Duplicate edge: [:{edge.type}] {edge.source_node}->{edge.target_node} "
                    f"from table '{edge.source_table}'"
                )
            seen.add(key)
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
