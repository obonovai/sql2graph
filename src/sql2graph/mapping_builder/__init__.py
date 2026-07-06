"""Build a :class:`~sql2graph.mapping.SchemaMapping` from a relational schema.

The library requires a hand-authored schema mapping for every translation. This
package generates a first draft of that mapping from ``CREATE TABLE`` DDL, so the
user reviews and edits instead of writing it from scratch.

The pipeline is three stages, each its own module:

1. **extract** (:mod:`~sql2graph.mapping_builder.ddl`) - parse DDL into the
   dependency-free :class:`~sql2graph.mapping_builder.relational.RelationalSchema`
   IR (the seam where a future live-database source could plug in).
2. **project** (:mod:`~sql2graph.mapping_builder.project`) - apply the canonical
   relational-to-graph heuristics to produce a mapping that is *valid by
   construction*, plus a :class:`~sql2graph.mapping_builder.project.CoverageReport`.
3. **refine** (:mod:`~sql2graph.mapping_builder.refine`, *optional*) - let an LLM
   improve names, fenced so it can never invent an identifier.

:func:`build_mapping` is the single entry point. It is deterministic by default: with
no ``llm`` it runs only extract + project, so it is offline and free. Pass an
:class:`~sql2graph.llm.LLMClient` to opt into the naming pass; if that pass fails any
guardrail the deterministic mapping is kept (with a warning), so the result is always
valid even when the model errors or is unreachable. The deterministic projection on
its own is also available via :func:`project_to_mapping` - the seam tests and a future
live-database source plug into.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sql2graph.engine.events import ConversationCallback
from sql2graph.llm import AsyncLLMClient, LLMClient, TokenUsage
from sql2graph.mapping import SchemaMapping
from sql2graph.mapping_builder.ddl import DdlParseError, extract_schema_from_ddl
from sql2graph.mapping_builder.diff import MappingDiff, RenameDiff, diff_mappings
from sql2graph.mapping_builder.project import (
    CoverageReport,
    ProjectionResult,
    is_composition_fk,
    is_junction_table,
    is_multivalue_property_table,
    project_to_mapping,
)
from sql2graph.mapping_builder.refine import (
    RefinementResult,
    refine_mapping,
    refine_mapping_async,
    validate_against_schema,
)
from sql2graph.mapping_builder.relational import Column, ForeignKey, RelationalSchema, Table
from sql2graph.mapping_builder.serialize import mapping_to_yaml


@dataclass(frozen=True)
class BuildResult:
    """Everything a mapping build produces.

    Attributes:
        mapping: The generated :class:`SchemaMapping` (deterministic, or refined
            when an LLM was supplied and its output passed the guardrail).
        yaml: ``mapping`` serialised to canonical YAML, ready to save or load.
        skeleton_yaml: The deterministic mapping's YAML before refinement. It equals
            ``yaml`` when the naming pass kept every name or was rejected; when the LLM
            changed names it is the "original" a reviewer can compare to.
        report: The :class:`CoverageReport` explaining how the schema was projected.
        refined: ``True`` iff the naming pass ran and changed the deterministic skeleton.
        warnings: Non-fatal issues (synthesized keys, dropped edges, rejected
            refinement). Always safe to surface to the user.
        conversation: The refinement chat transcript (system / user / assistant).
            Populated when the naming pass ran, so a caller can show exactly what the
            AI was asked and answered; empty when no ``llm`` was supplied.
        diff: The renames the LLM applied (labels, edge types, property keys). ``None``
            when no ``llm`` was supplied; otherwise present but empty when the LLM kept
            every name or its output was rejected.
        token_usage: Tokens the naming pass consumed. Zero when no ``llm`` was supplied.
        duration_seconds: Wall-clock time of the naming pass. Zero for a deterministic build.
    """

    mapping: SchemaMapping
    yaml: str
    report: CoverageReport
    refined: bool = False
    warnings: list[str] = field(default_factory=list)
    skeleton_yaml: str = ""
    conversation: list[dict[str, str]] = field(default_factory=list)
    diff: MappingDiff | None = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    duration_seconds: float = 0.0


def build_mapping(*, ddl: str, dialect: str | None = None, llm: LLMClient | None = None) -> BuildResult:
    """Generate a :class:`SchemaMapping` from ``CREATE TABLE`` *ddl*.

    Extract the schema and project it onto a valid mapping. When *llm* is provided,
    additionally run the LLM naming pass to make the labels and edge types read well;
    the pass is guarded, so if it fails any check the deterministic mapping is kept and
    the reason is added to ``warnings``. With no *llm* the build is deterministic,
    offline, and free (equivalent to :func:`project_to_mapping`, wrapped in a
    :class:`BuildResult`). Either way the result is always valid.

    Args:
        ddl: One or more ``CREATE TABLE`` statements.
        dialect: Optional sqlglot dialect (e.g. ``"postgres"``).
        llm: Optional client for the naming-refinement pass. ``None`` skips it.

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

    The deterministic extract/project stage is identical and synchronous; only the
    optional naming pass runs on the async LLM. When *llm* is ``None`` the build is
    deterministic and *on_conversation* is never called. When *on_conversation* is set
    and an *llm* is supplied, the refinement streams the assistant turn as a growing
    snapshot so a caller (the web SSE bridge) can show the chat live.

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
    (optional) refinement outcome, shared by the sync and async entry points.

    When *outcome* is ``None`` the naming pass was skipped: the mapping *is* the
    skeleton, ``conversation`` is empty, and ``diff`` is ``None``. When the pass ran but
    the guardrail rejected it, the outcome's mapping is likewise the skeleton, so
    ``refined`` is ``False`` and ``diff`` is empty, yet the conversation still records
    what was attempted.
    """
    skeleton = projection.mapping
    mapping = outcome.mapping if outcome is not None else skeleton
    warnings = list(projection.report.warnings)
    conversation: list[dict[str, str]] = []
    diff: MappingDiff | None = None
    token_usage = TokenUsage()
    duration_seconds = 0.0
    if outcome is not None:
        warnings.extend(outcome.warnings)
        conversation = outcome.messages
        diff = diff_mappings(skeleton, mapping)
        token_usage = outcome.token_usage
        duration_seconds = outcome.duration_seconds
    return BuildResult(
        mapping=mapping,
        yaml=mapping_to_yaml(mapping),
        report=projection.report,
        refined=mapping != skeleton,
        warnings=warnings,
        skeleton_yaml=mapping_to_yaml(skeleton),
        conversation=conversation,
        diff=diff,
        token_usage=token_usage,
        duration_seconds=duration_seconds,
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
    "is_composition_fk",
    "is_junction_table",
    "is_multivalue_property_table",
    "mapping_to_yaml",
    "project_to_mapping",
    "refine_mapping",
    "validate_against_schema",
]
