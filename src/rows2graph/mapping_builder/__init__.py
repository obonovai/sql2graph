"""Build a :class:`~rows2graph.mapping.SchemaMapping` from a relational schema.

The library requires a hand-authored schema mapping for every translation. This
package generates a first draft of that mapping from ``CREATE TABLE`` DDL, so the
user reviews and edits instead of writing it from scratch.

The pipeline is three stages, each its own module:

1. **extract** (:mod:`~rows2graph.mapping_builder.ddl`) - parse DDL into the
   dependency-free :class:`~rows2graph.mapping_builder.relational.RelationalSchema`
   IR (the seam where a future live-database source could plug in).
2. **project** (:mod:`~rows2graph.mapping_builder.project`) - apply the canonical
   relational-to-graph heuristics to produce a mapping that is *valid by
   construction*, plus a :class:`~rows2graph.mapping_builder.project.CoverageReport`.
3. **refine** (:mod:`~rows2graph.mapping_builder.refine`, optional) - let an LLM
   improve names, fenced so it can never invent an identifier.

:func:`build_mapping` is the single entry point. With no ``llm`` it is fully
deterministic, offline, and free; pass an :class:`~rows2graph.llm.LLMClient` to
opt into the naming pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rows2graph.events import ConversationCallback
from rows2graph.llm import AsyncLLMClient, LLMClient
from rows2graph.mapping import SchemaMapping
from rows2graph.mapping_builder.ddl import DdlParseError, extract_schema_from_ddl
from rows2graph.mapping_builder.diff import MappingDiff, RenameDiff, diff_mappings
from rows2graph.mapping_builder.project import CoverageReport, ProjectionResult, is_junction_table, project_to_mapping
from rows2graph.mapping_builder.refine import (
    RefinementResult,
    refine_mapping,
    refine_mapping_async,
    validate_against_schema,
)
from rows2graph.mapping_builder.relational import Column, ForeignKey, RelationalSchema, Table
from rows2graph.mapping_builder.serialize import format_audit_report, mapping_to_yaml


@dataclass(frozen=True)
class BuildResult:
    """Everything a mapping build produces.

    Attributes:
        mapping: The generated :class:`SchemaMapping` (deterministic, or refined
            when an LLM was supplied and its output passed the guardrail).
        yaml: ``mapping`` serialised to canonical YAML, ready to save or load.
        skeleton_yaml: The deterministic mapping's YAML before any refinement. It
            equals ``yaml`` when no LLM ran or the refinement was rejected; when
            the LLM changed names it is the "original" a reviewer can compare to.
        report: The :class:`CoverageReport` explaining how the schema was projected.
        refined: ``True`` iff an LLM ran *and* changed the deterministic skeleton.
        warnings: Non-fatal issues (synthesized keys, dropped edges, rejected
            refinement). Always safe to surface to the user.
        conversation: The refinement chat transcript (system / user / assistant);
            empty when no LLM ran. Lets a caller show exactly what the AI was asked
            and answered.
        diff: The renames the LLM applied (labels, edge types, property keys), or
            ``None`` when no LLM ran. Empty when the LLM kept every name.
    """

    mapping: SchemaMapping
    yaml: str
    report: CoverageReport
    refined: bool = False
    warnings: list[str] = field(default_factory=list)
    skeleton_yaml: str = ""
    conversation: list[dict[str, str]] = field(default_factory=list)
    diff: MappingDiff | None = None


def build_mapping(*, ddl: str, dialect: str | None = None, llm: LLMClient | None = None) -> BuildResult:
    """Generate a :class:`SchemaMapping` from ``CREATE TABLE`` *ddl*.

    Deterministic by default: extract the schema, project it onto a valid mapping,
    and serialise. When *llm* is provided, an additional refinement pass improves
    naming; if that pass fails any guardrail the deterministic mapping is kept and
    the reason is added to ``warnings``.

    Args:
        ddl: One or more ``CREATE TABLE`` statements.
        dialect: Optional sqlglot dialect (e.g. ``"postgres"``).
        llm: Optional client enabling the naming-refinement pass.

    Raises:
        DdlParseError: if the DDL cannot be parsed.
    """
    schema = extract_schema_from_ddl(ddl, dialect=dialect)
    projection = project_to_mapping(schema)
    outcome = refine_mapping(projection.mapping, schema, llm) if llm is not None else None
    return _finalize(projection, outcome)


async def build_mapping_async(
    *,
    ddl: str,
    dialect: str | None = None,
    llm: AsyncLLMClient | None = None,
    on_conversation: ConversationCallback | None = None,
) -> BuildResult:
    """Async, optionally streaming, counterpart of :func:`build_mapping`.

    The deterministic extract/project stage is identical and synchronous; only
    the optional refinement pass runs on the async LLM. When *on_conversation* is
    set, the refinement streams the assistant turn as a growing snapshot so a
    caller (the web SSE bridge) can show the chat live.

    Raises:
        DdlParseError: if the DDL cannot be parsed.
    """
    schema = extract_schema_from_ddl(ddl, dialect=dialect)
    projection = project_to_mapping(schema)
    outcome = (
        await refine_mapping_async(projection.mapping, schema, llm, on_conversation=on_conversation)
        if llm is not None
        else None
    )
    return _finalize(projection, outcome)


def _finalize(projection: ProjectionResult, outcome: RefinementResult | None) -> BuildResult:
    """Assemble a :class:`BuildResult` from the deterministic projection and the
    optional refinement outcome (shared by the sync and async entry points)."""
    skeleton = projection.mapping
    mapping = outcome.mapping if outcome is not None else skeleton
    warnings = list(projection.report.warnings)
    conversation: list[dict[str, str]] = []
    diff: MappingDiff | None = None
    if outcome is not None:
        warnings.extend(outcome.warnings)
        conversation = outcome.messages
        diff = diff_mappings(skeleton, mapping)
    return BuildResult(
        mapping=mapping,
        yaml=mapping_to_yaml(mapping),
        report=projection.report,
        refined=mapping != skeleton,
        warnings=warnings,
        skeleton_yaml=mapping_to_yaml(skeleton),
        conversation=conversation,
        diff=diff,
    )


__all__ = [
    "BuildResult",
    "Column",
    "CoverageReport",
    "DdlParseError",
    "ForeignKey",
    "MappingDiff",
    "ProjectionResult",
    "RefinementResult",
    "RelationalSchema",
    "RenameDiff",
    "Table",
    "build_mapping",
    "build_mapping_async",
    "diff_mappings",
    "extract_schema_from_ddl",
    "format_audit_report",
    "is_junction_table",
    "mapping_to_yaml",
    "project_to_mapping",
    "refine_mapping",
    "validate_against_schema",
]
