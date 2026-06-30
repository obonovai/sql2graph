"""Optional LLM refinement of a deterministic mapping skeleton.

The deterministic projection gets the *structure* right but the *names* clunky
(``HAS_REGION`` instead of ``IN_REGION``, ``Lineitem`` instead of ``LineItem``).
This module asks an LLM to fix exactly that - the graph-facing names (node labels,
edge types, property keys) - while a hard guardrail keeps it from touching anything
else.

The guardrail has two parts. :func:`validate_against_schema` checks the mapping only
references columns that really *exist* in the extracted schema (the inverse of
:func:`rows2graph.preflight.find_unmapped_columns`, which checks a *query* against a
mapping). On top of that, :func:`_preservation_violations` checks the SQL side was
*preserved*: every ``source_table``, key, foreign key, and property column value must
be identical to the deterministic skeleton, and no node or edge may be added or
dropped. The LLM may relabel labels, edge types, and property *keys* freely, but may
not touch a single SQL identifier; anything it gets wrong falls back to the always-
valid skeleton. This mirrors the translator's own generate → validate → fix loop:
the model proposes, a deterministic check disposes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError

from rows2graph.events import ConversationCallback
from rows2graph.llm import AsyncLLMClient, LLMClient, StreamCallback
from rows2graph.mapping import SchemaMapping
from rows2graph.mapping_builder.relational import RelationalSchema
from rows2graph.mapping_builder.serialize import mapping_to_yaml

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\n(.*?)\n```\s*$", re.DOTALL)
_MAX_REPORTED_VIOLATIONS = 8


@dataclass(frozen=True)
class RefinementResult:
    """The outcome of one LLM refinement pass, including the conversation.

    Attributes:
        mapping: The refined mapping when the LLM output passed the guardrail,
            otherwise the unchanged skeleton (refinement is best-effort).
        accepted: ``True`` only when the LLM output was validated and applied.
        messages: The full chat transcript (system / user / assistant, plus any
            repair round-trip), so a caller can show exactly what was asked and
            answered. Roles are ``system`` / ``user`` / ``assistant``.
        warnings: Non-fatal explanations (a rejected suggestion, an LLM error).
    """

    mapping: SchemaMapping
    accepted: bool
    messages: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def refine_mapping(
    skeleton: SchemaMapping,
    schema: RelationalSchema,
    llm: LLMClient,
    *,
    max_repair_attempts: int = 1,
) -> RefinementResult:
    """Run the LLM naming pass and return the result plus the full conversation.

    On any failure - the LLM erroring, unparseable/invalid YAML, or output that
    references a table/column absent from *schema* or drops a skeleton table -
    the skeleton is returned unchanged (``accepted=False``) with an explanatory
    warning. One repair round-trip is attempted (configurable) before giving up,
    feeding the concrete violations back to the model. The returned ``messages``
    always include each assistant reply, so the caller can show the chat.
    """
    warnings: list[str] = []
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _build_refine_system_prompt()},
        {"role": "user", "content": _build_refine_user_prompt(skeleton, schema)},
    ]

    for attempt in range(max_repair_attempts + 1):
        try:
            reply = llm.chat(messages)
        except Exception as exc:  # noqa: BLE001 (refinement is best-effort; never crash the build)
            warnings.append(f"LLM refinement failed ({type(exc).__name__}: {exc}); kept the deterministic mapping.")
            return RefinementResult(mapping=skeleton, accepted=False, messages=messages, warnings=warnings)

        # Record the reply in the transcript before judging it, so the chat the
        # caller renders always reflects what the model actually said.
        messages.append({"role": "assistant", "content": reply.text})
        candidate, violations = _parse_and_validate(reply.text, skeleton, schema)
        if candidate is not None:
            return RefinementResult(mapping=candidate, accepted=True, messages=messages, warnings=warnings)

        if attempt >= max_repair_attempts:
            warnings.append("LLM refinement was rejected; kept the deterministic mapping.")
            warnings.extend(violations[:_MAX_REPORTED_VIOLATIONS])
            return RefinementResult(mapping=skeleton, accepted=False, messages=messages, warnings=warnings)

        messages.append({"role": "user", "content": _build_repair_prompt(violations)})

    return RefinementResult(mapping=skeleton, accepted=False, messages=messages, warnings=warnings)  # pragma: no cover


async def refine_mapping_async(
    skeleton: SchemaMapping,
    schema: RelationalSchema,
    llm: AsyncLLMClient,
    *,
    max_repair_attempts: int = 1,
    on_conversation: ConversationCallback | None = None,
) -> RefinementResult:
    """Async, optionally streaming, counterpart of :func:`refine_mapping`.

    Identical guardrail and repair logic, but awaits the async LLM and, when
    *on_conversation* is set, streams the assembling assistant turn (so an SSE
    bridge can show the model "typing") and emits a snapshot after every turn.
    """
    warnings: list[str] = []
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _build_refine_system_prompt()},
        {"role": "user", "content": _build_refine_user_prompt(skeleton, schema)},
    ]
    _emit_conversation(on_conversation, messages)

    for attempt in range(max_repair_attempts + 1):
        try:
            reply = await llm.chat(messages, stream_to=_streaming_callback(messages, on_conversation))
        except Exception as exc:  # noqa: BLE001 (refinement is best-effort; never crash the build)
            warnings.append(f"LLM refinement failed ({type(exc).__name__}: {exc}); kept the deterministic mapping.")
            return RefinementResult(mapping=skeleton, accepted=False, messages=messages, warnings=warnings)

        messages.append({"role": "assistant", "content": reply.text})
        _emit_conversation(on_conversation, messages)
        candidate, violations = _parse_and_validate(reply.text, skeleton, schema)
        if candidate is not None:
            return RefinementResult(mapping=candidate, accepted=True, messages=messages, warnings=warnings)

        if attempt >= max_repair_attempts:
            warnings.append("LLM refinement was rejected; kept the deterministic mapping.")
            warnings.extend(violations[:_MAX_REPORTED_VIOLATIONS])
            return RefinementResult(mapping=skeleton, accepted=False, messages=messages, warnings=warnings)

        messages.append({"role": "user", "content": _build_repair_prompt(violations)})
        _emit_conversation(on_conversation, messages)

    return RefinementResult(mapping=skeleton, accepted=False, messages=messages, warnings=warnings)  # pragma: no cover


def _streaming_callback(
    messages: list[dict[str, str]],
    on_conversation: ConversationCallback | None,
) -> StreamCallback | None:
    """Per-delta callback that grows the live assistant message in the snapshot."""
    if on_conversation is None:
        return None
    parts: list[str] = []

    def _cb(delta: str) -> None:
        parts.append(delta)
        _emit_conversation(on_conversation, [*messages, {"role": "assistant", "content": "".join(parts)}])

    return _cb


def _emit_conversation(on_conversation: ConversationCallback | None, messages: list[dict[str, str]]) -> None:
    """Send a *copied* snapshot to the handler, isolating its exceptions.

    The list is copied so the consumer (e.g. the SSE bridge) can hold it while
    this function keeps mutating ``messages`` for later turns.
    """
    if on_conversation is None:
        return
    try:
        on_conversation([dict(m) for m in messages])
    except Exception:  # noqa: BLE001 (a misbehaving handler must not abort refinement)
        pass


def validate_against_schema(mapping: SchemaMapping, schema: RelationalSchema) -> list[str]:
    """Return the ways *mapping* references identifiers absent from *schema*.

    Checks only the SQL side - ``source_table``, ``primary_key``,
    ``source_foreign_key``, ``target_primary_key`` and every property *value* -
    never labels/types/property *keys*, which the LLM is allowed to rewrite.
    Comparisons casefold, mirroring :mod:`rows2graph.preflight`.
    """
    columns_by_table: dict[str, set[str]] = {t.name.casefold(): {c.casefold() for c in t.column_names()} for t in schema.tables}
    table_for_label = {n.label: n.source_table for n in mapping.nodes}
    violations: list[str] = []

    for node in mapping.nodes:
        cols = columns_by_table.get(node.source_table.casefold())
        if cols is None:
            violations.append(f"node '{node.label}': source_table '{node.source_table}' is not a table in the schema")
            continue
        if node.primary_key.casefold() not in cols:
            violations.append(f"node '{node.label}': primary_key '{node.primary_key}' is not a column of '{node.source_table}'")
        for prop, column in node.properties.items():
            if column.casefold() not in cols:
                violations.append(f"node '{node.label}': property '{prop}' maps to missing column '{node.source_table}.{column}'")

    for edge in mapping.edges:
        cols = columns_by_table.get(edge.source_table.casefold())
        if cols is None:
            violations.append(f"edge '{edge.type}': source_table '{edge.source_table}' is not a table in the schema")
            continue
        if edge.source_foreign_key.casefold() not in cols:
            violations.append(f"edge '{edge.type}': source_foreign_key '{edge.source_foreign_key}' is not a column of '{edge.source_table}'")
        for prop, column in edge.properties.items():
            if column.casefold() not in cols:
                violations.append(f"edge '{edge.type}': property '{prop}' maps to missing column '{edge.source_table}.{column}'")
        target_table = table_for_label.get(edge.target_node)
        target_cols = columns_by_table.get(target_table.casefold()) if target_table else None
        if target_cols is not None and edge.target_primary_key.casefold() not in target_cols:
            violations.append(f"edge '{edge.type}': target_primary_key '{edge.target_primary_key}' is not a column of '{target_table}'")

    return violations


def _parse_and_validate(text: str, skeleton: SchemaMapping, schema: RelationalSchema) -> tuple[SchemaMapping | None, list[str]]:
    """Parse the LLM's YAML and run every guardrail; return (mapping, violations)."""
    try:
        candidate = SchemaMapping.from_yaml_string(_strip_code_fences(text))
    except (yaml.YAMLError, ValidationError) as exc:
        return None, [f"output was not a valid mapping: {exc}"]

    violations = validate_against_schema(candidate, schema)
    violations.extend(_coverage_regressions(skeleton, candidate))
    violations.extend(_preservation_violations(skeleton, candidate))
    if violations:
        return None, violations
    return candidate, []


def _sql_side_signature(mapping: SchemaMapping) -> tuple[frozenset[Any], frozenset[Any]]:
    """The SQL-facing identity of a mapping, independent of graph-facing names.

    Two mappings share a signature iff they map the same tables and columns the
    same way, regardless of labels, edge types, or property *keys*. Property
    column values are compared as a set per node/edge (a renamed key keeps its
    value; a dropped column changes the set). All values casefold, mirroring the
    rest of the guardrail.
    """
    nodes = frozenset(
        (
            n.source_table.casefold(),
            n.primary_key.casefold(),
            frozenset(v.casefold() for v in n.properties.values()),
        )
        for n in mapping.nodes
    )
    edges = frozenset(
        (
            e.source_table.casefold(),
            e.source_foreign_key.casefold(),
            e.target_primary_key.casefold(),
            frozenset(v.casefold() for v in e.properties.values()),
        )
        for e in mapping.edges
    )
    return nodes, edges


def _preservation_violations(skeleton: SchemaMapping, candidate: SchemaMapping) -> list[str]:
    """Flag any SQL-side change: a swapped identifier, or an added/dropped node/edge.

    :func:`validate_against_schema` only proves the candidate's identifiers
    *exist*; this proves they were *preserved*. Without it the LLM could silently
    repoint a foreign key or primary key to another real column, swap a property to
    a different real column, or add/drop an edge, and the result would still pass
    (every identifier exists and the model is structurally valid).
    """
    skel_nodes, skel_edges = _sql_side_signature(skeleton)
    cand_nodes, cand_edges = _sql_side_signature(candidate)
    out: list[str] = []
    if cand_nodes != skel_nodes:
        out.append(
            "a node's SQL side changed (a source_table, primary_key, or property column "
            "value differs from the draft); only labels and property names may be renamed"
        )
    if cand_edges != skel_edges:
        out.append(
            "an edge's SQL side changed (a relationship was added or removed, or a "
            "source_table / foreign key / target key / property column value differs from "
            "the draft); only edge types and property names may be renamed"
        )
    return out


def _coverage_regressions(skeleton: SchemaMapping, candidate: SchemaMapping) -> list[str]:
    """Flag node tables the candidate dropped or introduced relative to the skeleton."""
    skel = {n.source_table.casefold(): n.source_table for n in skeleton.nodes}
    cand = {n.source_table.casefold() for n in candidate.nodes}
    out = [f"dropped node for table '{name}' present in the deterministic mapping" for key, name in skel.items() if key not in cand]
    out += [f"introduced a node for table '{t}' that is not in the deterministic mapping" for t in sorted(cand - set(skel))]
    return out


def _strip_code_fences(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    return match.group(1) if match else text.strip()


def _build_refine_system_prompt() -> str:
    return (
        "You are a graph data-modeling expert. You are given a draft "
        "relational-to-property-graph mapping (YAML) that was generated mechanically "
        "from a SQL schema, plus that schema. Your job is to make the draft read like "
        "an expert hand-authored it, WITHOUT changing what it maps to.\n\n"
        "You MAY change only:\n"
        "  - node `label` values (e.g. `Lineitem` -> `LineItem`),\n"
        "  - edge `type` values (e.g. `HAS_REGION` -> `IN_REGION`, a junction's name to a verb like `KNOWS`),\n"
        "  - the KEYS of any `properties` map (the graph-facing property names).\n\n"
        "You MUST NOT change any SQL identifier. Every `source_table`, `primary_key`, "
        "`source_foreign_key`, `target_primary_key`, and every `properties` VALUE must be "
        "copied verbatim from the draft. Do not add or remove nodes or edges, and do not "
        "drop any table. If an edge `source_node`/`target_node` references a label you "
        "rename, update those references consistently so every edge still points at a "
        "declared node.\n\n"
        "Respond with ONLY the improved mapping as YAML - the same `nodes:`/`edges:` "
        "structure, no prose, no code fences."
    )


def _build_refine_user_prompt(skeleton: SchemaMapping, schema: RelationalSchema) -> str:
    return (
        f"Relational schema:\n{_render_relational_schema(schema)}\n\n"
        f"Draft mapping to improve:\n{mapping_to_yaml(skeleton)}\n"
        "Return the improved mapping as YAML only."
    )


def _build_repair_prompt(violations: list[str]) -> str:
    listed = "\n".join(f"  - {v}" for v in violations[:_MAX_REPORTED_VIOLATIONS])
    return (
        "Your previous answer changed or invented SQL identifiers, which is not allowed:\n"
        f"{listed}\n\n"
        "Return the mapping again. Rename only labels, edge types, and property keys; "
        "copy every table name, column name, and key verbatim from the draft. YAML only."
    )


def _render_relational_schema(schema: RelationalSchema) -> str:
    """A compact textual rendering of the schema for the prompt."""
    lines: list[str] = []
    for table in schema.tables:
        pk = {c.casefold() for c in table.primary_key}
        fk_by_col = {fk.columns[0].casefold(): fk.ref_table for fk in table.single_column_foreign_keys()}
        parts: list[str] = []
        for column in table.columns:
            flags = []
            if column.name.casefold() in pk:
                flags.append("PK")
            if column.name.casefold() in fk_by_col:
                flags.append(f"FK->{fk_by_col[column.name.casefold()]}")
            parts.append(column.name + (f" [{', '.join(flags)}]" if flags else ""))
        lines.append(f"- {table.name}({', '.join(parts)})")
    return "\n".join(lines)
