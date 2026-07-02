"""Prompt-assembly unit tests: system/generate/fix/escalation prompts and signatures."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rows2graph import AqlTarget, CypherTarget, GremlinTarget
from rows2graph.prompts import (
    build_escalation_prompt,
    build_fix_prompt,
    build_generate_prompt,
    build_system_prompt,
    error_signature,
    normalize_query,
)


def test_build_system_prompt_cypher(person_forum_schema: Callable[..., Any]) -> None:
    prompt = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset())
    assert "cypher" in prompt
    assert "MATCH" in prompt
    assert "Person" in prompt  # schema is embedded


def test_build_system_prompt_aql_uses_edge_collection_form(person_forum_schema: Callable[..., Any]) -> None:
    prompt = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset())
    assert "aql" in prompt
    assert "FOR" in prompt
    assert "FILTER" in prompt
    assert "Person" in prompt  # schema is embedded
    # AQL uses bare edge-collection traversals, and the prompt warns against
    # the Cypher edge syntax small models tend to emit.
    assert "OUTBOUND" in prompt
    assert "These are NOT valid AQL" in prompt


def test_build_system_prompt_gremlin(person_forum_schema: Callable[..., Any]) -> None:
    prompt = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset())
    assert "gremlin" in prompt
    assert "g.V()" in prompt
    assert "Person" in prompt  # schema is embedded


def test_build_generate_prompt_includes_sql() -> None:
    assert "SELECT 1" in build_generate_prompt("SELECT 1")


def test_build_fix_prompt_includes_errors() -> None:
    prompt = build_fix_prompt(
        sql_query="SELECT 1",
        generated_query="MATCH (n",
        errors=["Unbalanced parentheses"],
    )
    assert "SELECT 1" in prompt
    assert "MATCH (n" in prompt
    assert "Unbalanced parentheses" in prompt


def test_build_fix_prompt_default_keeps_do_not_restructure() -> None:
    prompt = build_fix_prompt(sql_query="SELECT 1", generated_query="MATCH (n", errors=["e"])
    assert "Do not change the query structure unnecessarily." in prompt


def test_build_fix_prompt_repair_hint_replaces_default() -> None:
    prompt = build_fix_prompt(
        sql_query="SELECT 1",
        generated_query="MATCH (n",
        errors=["e"],
        repair_hint="MOVE THE RETURN LAST.",
    )
    assert "MOVE THE RETURN LAST." in prompt
    # The hint *replaces* the default "don't restructure" instruction.
    assert "Do not change the query structure unnecessarily." not in prompt


def test_build_escalation_prompt_names_the_repetition_and_hint() -> None:
    prompt = build_escalation_prompt(
        sql_query="SELECT 1",
        generated_query="FOR f IN Forum RETURN f SORT f.x",
        errors=["unexpected SORT declaration"],
        repair_hint="MOVE THE RETURN LAST.",
    )
    assert "DIFFERENT" in prompt  # tells the model not to repeat itself
    assert "FOR f IN Forum RETURN f SORT f.x" in prompt
    assert "MOVE THE RETURN LAST." in prompt


def test_error_signature_is_position_independent() -> None:
    a = ["[ERR 1501] syntax error, unexpected SORT declaration near 'x' at position 4:3"]
    b = ["[ERR 1501] syntax error, unexpected SORT declaration near 'y' at position 4:1"]
    # Same ArangoDB error code + shape, different position/near-text → same signature.
    assert error_signature(a) == error_signature(b)
    assert error_signature(a) != error_signature(["[ERR 1577] something else"])


def test_normalize_query_collapses_whitespace() -> None:
    assert normalize_query("FOR f\n  RETURN f") == normalize_query("FOR f RETURN f")
