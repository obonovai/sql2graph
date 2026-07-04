"""The build_mapping / build_mapping_async facades (sync + async streaming)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from sql2graph import SchemaMapping, build_mapping, build_mapping_async
from sql2graph.mapping_builder import mapping_to_yaml
from sql2graph.mapping_builder.ddl import extract_schema_from_ddl
from sql2graph.mapping_builder.project import CoverageReport, project_to_mapping


def test_build_mapping_noop_refinement(tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    # The naming pass always runs. A non-YAML reply is rejected by the guardrail, so
    # the build falls back to the deterministic skeleton - still valid, but now with
    # the transcript recorded and an (empty) diff rather than None.
    result = build_mapping(ddl=tpch_ddl, dialect="postgres", llm=oneshot_llm("(noop)"))
    assert result.refined is False
    assert SchemaMapping.from_yaml_string(result.yaml) == result.mapping
    assert result.report.as_dict()["node_count"] == 7
    assert result.skeleton_yaml == result.yaml
    # The naming pass ran: the chat is recorded, the diff is empty (not None), and the
    # fallback is explained in the warnings.
    assert result.conversation and result.conversation[0]["role"] == "system"
    assert result.diff is not None and result.diff.is_empty()
    assert any("rejected" in w.lower() for w in result.warnings)


def test_build_mapping_with_llm_refines(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    improved = mapping_to_yaml(skeleton).replace("HAS_REGION", "IN_REGION")
    result = build_mapping(ddl=tpch_ddl, dialect="postgres", llm=oneshot_llm(improved))
    assert result.refined is True
    assert "IN_REGION" in {e.type for e in result.mapping.edges}
    # The refinement is now transparent: original kept, chat captured, diff computed.
    assert result.skeleton_yaml == mapping_to_yaml(skeleton)
    assert result.skeleton_yaml != result.yaml
    assert result.conversation and result.conversation[0]["role"] == "system"
    assert result.diff is not None
    assert ("HAS_REGION", "IN_REGION") in {(r.before, r.after) for r in result.diff.edge_type_renames}


def test_build_mapping_deterministic_without_llm(tpch_skeleton: Callable[..., Any], tpch_ddl: str) -> None:
    # No llm -> deterministic only: the naming pass is skipped entirely, so there is no
    # chat and diff is None (not an empty diff). The mapping is exactly the projection
    # skeleton, and skeleton_yaml equals the output yaml.
    result = build_mapping(ddl=tpch_ddl, dialect="postgres")
    assert result.refined is False
    assert result.conversation == []
    assert result.diff is None
    assert result.mapping == tpch_skeleton()
    assert result.skeleton_yaml == result.yaml
    # No naming pass ran, so there is no LLM cost to report.
    assert result.token_usage.total_tokens == 0
    assert result.duration_seconds == 0.0


def test_report_as_dict_lists_junctions_and_warnings(tpch_ddl: str) -> None:
    report: CoverageReport = project_to_mapping(extract_schema_from_ddl(tpch_ddl, dialect="postgres")).report
    data = report.as_dict()
    assert data["edge_tables"] == ["partsupp"]
    assert isinstance(data["warnings"], list)


def test_build_mapping_async_streams_and_matches_sync(tpch_skeleton: Callable[..., Any], tpch_ddl: str, oneshot_async_llm: Callable[..., Any], oneshot_llm: Callable[..., Any]) -> None:
    skeleton = tpch_skeleton()
    improved = mapping_to_yaml(skeleton).replace("HAS_REGION", "IN_REGION")
    snapshots: list[list[dict[str, str]]] = []
    result = asyncio.run(
        build_mapping_async(
            ddl=tpch_ddl,
            dialect="postgres",
            llm=oneshot_async_llm(improved),
            on_conversation=snapshots.append,
        )
    )
    assert result.refined is True
    assert "IN_REGION" in {e.type for e in result.mapping.edges}
    assert result.diff is not None
    assert result.conversation and result.conversation[0]["role"] == "system"

    # The assistant turn streamed in: snapshots whose last message is the assistant
    # grow monotonically (partial chunks then the full reply).
    assistant_lens = [len(s[-1]["content"]) for s in snapshots if s and s[-1]["role"] == "assistant"]
    assert len(assistant_lens) >= 2
    assert assistant_lens == sorted(assistant_lens)

    # The async path produces the same result as the sync path.
    sync_result = build_mapping(ddl=tpch_ddl, dialect="postgres", llm=oneshot_llm(improved))
    assert result.yaml == sync_result.yaml
    assert result.skeleton_yaml == sync_result.skeleton_yaml
    assert result.diff.as_dict() == sync_result.diff.as_dict()  # type: ignore[union-attr]


def test_build_mapping_async_noop_refinement(tpch_ddl: str, oneshot_async_llm: Callable[..., Any]) -> None:
    # Async counterpart: a rejected naming pass falls back to the skeleton, but the
    # conversation is recorded and the diff is empty (not None).
    result = asyncio.run(build_mapping_async(ddl=tpch_ddl, dialect="postgres", llm=oneshot_async_llm("(noop)")))
    assert result.refined is False
    assert result.skeleton_yaml == result.yaml
    assert result.conversation and result.conversation[0]["role"] == "system"
    assert result.diff is not None and result.diff.is_empty()


def test_build_mapping_async_deterministic_without_llm(tpch_skeleton: Callable[..., Any], tpch_ddl: str) -> None:
    # Async counterpart of the deterministic path: no llm -> no naming pass, no chat,
    # diff is None, and the mapping is the projection skeleton.
    result = asyncio.run(build_mapping_async(ddl=tpch_ddl, dialect="postgres"))
    assert result.refined is False
    assert result.conversation == []
    assert result.diff is None
    assert result.mapping == tpch_skeleton()
