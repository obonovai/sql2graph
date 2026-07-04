"""Unit tests for per-target prompt rules: base blocks, repair hints, feature
gating, cross-target parity, and AQL clause/set-operation rules.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from sql2graph import (
    AqlSyntaxValidator,
    AqlTarget,
    CypherSyntaxValidator,
    CypherTarget,
    GremlinTarget,
    SQLTranslator,
)
from sql2graph.prompts import build_system_prompt
from sql2graph.sql_features import ALL_FEATURES, SqlFeature
from sql2graph.targets import aql as aql_target
from sql2graph.targets import cypher as cypher_target
from sql2graph.targets import gremlin as gremlin_target
from sql2graph.targets._schema import EX_JOIN_FILTER_SQL, EX_POINT_LOOKUP_SQL

# Target classes and modules for parametrized cross-target parity tests.
_ALL_TARGET_CLASSES = [CypherTarget, AqlTarget, GremlinTarget]
_ALL_TARGET_MODULES = [cypher_target, aql_target, gremlin_target]


def test_gremlin_base_rules_teach_projection_and_forbidden_patterns(person_forum_schema: Callable[..., Any]) -> None:
    # A plain SELECT detects no SqlFeature, so the always-on base block must
    # itself carry the read/projection guidance and the anti-hallucination
    # list, the two failure modes seen in the captured error logs.
    prompt = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset())
    # Reading + projecting columns into one traversal.
    assert ".project(" in prompt
    assert ".values(" in prompt
    # id COLUMN vs the graph's internal element id.
    assert "internal element id" in prompt
    # Anonymous steps are method calls (`__.id()`, not `__.id`).
    assert "__.id()" in prompt
    # Explicit forbidden list that names the hallucinated read steps.
    assert "NOT valid Gremlin" in prompt
    assert "WRITES a property" in prompt


def test_gremlin_base_rules_demand_single_traversal_no_prose(person_forum_schema: Callable[..., Any]) -> None:
    # The model tends to emit the right query, then keep talking with prose and
    # alternative versions; the base block must forbid that explicitly.
    prompt = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset())
    assert "Output EXACTLY ONE traversal" in prompt
    assert "no alternative versions" in prompt


def test_aql_repair_hint_fires_on_clause_ordering_error() -> None:
    errors = [
        "[HTTP 400][ERR 1501] syntax error, unexpected SORT declaration, "
        "expecting end of query string near 'SORT LENGTH(members) DESC' at position 4:1",
    ]
    hint = AqlTarget().repair_hint(errors)
    assert hint is not None
    assert "RETURN" in hint and "before" in hint.lower()


def test_aql_repair_hint_fires_on_offline_grammar_ordering_error() -> None:
    # The offline ANTLR validator phrases the clause-after-RETURN error as
    # "mismatched input 'SORT' expecting <EOF>", not the server's "unexpected
    # SORT". repair_hint must recognise both so the corrective still fires.
    errors = AqlSyntaxValidator().validate("FOR u IN users RETURN u.name SORT u.name DESC")
    assert errors and "mismatched input 'SORT'" in errors[0]
    hint = AqlTarget().repair_hint(errors)
    assert hint is not None
    assert "RETURN" in hint and "before" in hint.lower()


def test_aql_repair_hint_none_for_unrelated_error() -> None:
    assert AqlTarget().repair_hint(["Unbalanced parentheses"]) is None
    # A real offline parse error that is NOT a clause-ordering problem.
    assert AqlTarget().repair_hint(AqlSyntaxValidator().validate("RETURN (1 + )")) is None


def test_cypher_and_gremlin_repair_hint_always_none() -> None:
    err = ["unexpected SORT declaration, expecting end of query string"]
    assert CypherTarget().repair_hint(err) is None
    assert GremlinTarget().repair_hint(err) is None


def test_cypher_prompt_includes_like_chunk_only_when_feature_detected(person_forum_schema: Callable[..., Any]) -> None:
    with_like = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset({SqlFeature.LIKE}))
    without_like = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset())
    assert "CONTAINS" in with_like
    assert "CONTAINS" not in without_like


def test_cypher_prompt_omits_window_when_not_detected(person_forum_schema: Callable[..., Any]) -> None:
    prompt = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset({SqlFeature.LIKE}))
    assert "window function" not in prompt.lower()


def test_cypher_prompt_includes_window_chunk_when_detected(person_forum_schema: Callable[..., Any]) -> None:
    prompt = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset({SqlFeature.WINDOW}))
    assert "window function" in prompt.lower()


def test_cypher_prompt_includes_temporal_chunk_only_when_detected(person_forum_schema: Callable[..., Any]) -> None:
    with_temporal = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset({SqlFeature.TEMPORAL}))
    without_temporal = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset())
    # `datetime(` is unique to the temporal chunk and absent from the base block.
    assert "datetime(" in with_temporal
    assert "datetime(" not in without_temporal


def test_cypher_base_rules_carry_anti_pattern_block(person_forum_schema: Callable[..., Any]) -> None:
    # The always-on base block must now mirror AQL/Gremlin: a concrete data
    # model plus an explicit "NOT valid Cypher" anti-pattern list and an
    # output-format mandate, present even with no features detected.
    prompt = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset())
    assert "NOT valid Cypher" in prompt
    assert "Output ONLY the query" in prompt
    assert "MATCH" in prompt


def test_aql_prompt_includes_collect_only_when_aggregation_detected(person_forum_schema: Callable[..., Any]) -> None:
    with_agg = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset({SqlFeature.AGGREGATION}))
    without_agg = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset())
    assert "COLLECT" in with_agg
    assert "COLLECT" not in without_agg


def test_gremlin_prompt_includes_textp_only_when_like_detected(person_forum_schema: Callable[..., Any]) -> None:
    with_like = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset({SqlFeature.LIKE}))
    without_like = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset())
    assert "TextP.containing" in with_like
    assert "TextP.containing" not in without_like


def test_gremlin_prompt_includes_dedup_only_when_distinct_detected(person_forum_schema: Callable[..., Any]) -> None:
    with_distinct = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset({SqlFeature.DISTINCT}))
    without_distinct = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset())
    assert ".dedup()" in with_distinct
    assert ".dedup()" not in without_distinct


def test_aql_prompt_includes_temporal_chunk_only_when_detected(person_forum_schema: Callable[..., Any]) -> None:
    # Every target now defines a TEMPORAL chunk (no silent gaps). For AQL the
    # chunk is gated on `DATE_TIMESTAMP`, a substring absent from the base block.
    with_temporal = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset({SqlFeature.TEMPORAL}))
    without_temporal = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset())
    assert "DATE_TIMESTAMP" in with_temporal
    assert "DATE_TIMESTAMP" not in without_temporal


def test_gremlin_prompt_includes_temporal_chunk_only_when_detected(person_forum_schema: Callable[..., Any]) -> None:
    # Gremlin's TEMPORAL chunk is gated on `epoch`, absent from the base block.
    with_temporal = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset({SqlFeature.TEMPORAL}))
    without_temporal = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset())
    assert "epoch" in with_temporal
    assert "epoch" not in without_temporal


def test_all_targets_build_with_all_features(person_forum_schema: Callable[..., Any]) -> None:
    # TEMPORAL is in ALL_FEATURES (emitted on parse-failure); every target must
    # build the full rule set without raising now that coverage is total.
    for target in (CypherTarget(), AqlTarget(), GremlinTarget()):
        prompt = build_system_prompt(person_forum_schema(), target, ALL_FEATURES)
        assert prompt  # built without raising


@pytest.mark.parametrize(
    ("target_cls", "marker"),
    [
        (CypherTarget, "toInteger"),
        (AqlTarget, "TO_NUMBER"),
        (GremlinTarget, ".toUpper()"),
    ],
)
def test_scalar_chunk_gated_per_target(target_cls: type, marker: str, person_forum_schema: Callable[..., Any]) -> None:
    # The scalar-function mapping table must appear only when SCALAR is detected,
    # and its marker substring must stay OUT of the always-on base block (so the
    # token-saving gate actually holds).
    with_scalar = build_system_prompt(person_forum_schema(), target_cls(), frozenset({SqlFeature.SCALAR}))
    without_scalar = build_system_prompt(person_forum_schema(), target_cls(), frozenset())
    assert marker in with_scalar
    assert marker not in without_scalar


@pytest.mark.parametrize(
    ("target_cls", "marker"),
    [
        (CypherTarget, "IS NOT NULL"),
        (AqlTarget, "!= null"),
        (GremlinTarget, ".hasNot("),
    ],
)
def test_null_chunk_gated_per_target(target_cls: type, marker: str, person_forum_schema: Callable[..., Any]) -> None:
    # The null-handling chunk must appear only when NULL is detected and its
    # marker must be absent from the base block.
    with_null = build_system_prompt(person_forum_schema(), target_cls(), frozenset({SqlFeature.NULL}))
    without_null = build_system_prompt(person_forum_schema(), target_cls(), frozenset())
    assert marker in with_null
    assert marker not in without_null


def test_generic_join_rule_is_feature_gated(person_forum_schema: Callable[..., Any]) -> None:
    with_join = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset({SqlFeature.JOIN}))
    without_join = build_system_prompt(person_forum_schema(), CypherTarget(), frozenset())
    assert "Map SQL JOINs" in with_join
    assert "Map SQL JOINs" not in without_join


def test_gremlin_join_projection_guidance_only_when_join_detected(person_forum_schema: Callable[..., Any]) -> None:
    # The "label-as-you-go then select(...).by(...), walk the path once"
    # pattern is the fix for multi-table SELECT joins; it should appear only
    # when a JOIN is present, not on a single-table query.
    with_join = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset({SqlFeature.JOIN}))
    without_join = build_system_prompt(person_forum_schema(), GremlinTarget(), frozenset())
    assert "Walk the path ONCE" in with_join
    assert "Walk the path ONCE" not in without_join


def test_translator_omits_unused_rules_from_system_message(scripted_llm: Callable[..., Any], person_forum_schema: Callable[..., Any]) -> None:
    # SQL has only LIKE; the system prompt should carry the LIKE chunk
    # and omit the WINDOW chunk.
    fake = scripted_llm(["MATCH (p:Person) WHERE p.name CONTAINS 'a' RETURN p"])
    with SQLTranslator(
        schema_mapping=person_forum_schema(),
        llm=fake,
        target=CypherTarget(),
        validator=CypherSyntaxValidator(),
    ) as translator:
        translator.translate("SELECT * FROM persons WHERE full_name LIKE '%a%'")

    system_msg = fake.calls[0][0]
    assert system_msg["role"] == "system"
    assert "CONTAINS" in system_msg["content"]
    assert "window function" not in system_msg["content"].lower()


@pytest.mark.parametrize("target_mod", _ALL_TARGET_MODULES)
def test_target_feature_rules_cover_every_sql_feature(target_mod: Any) -> None:
    # The keystone parity guard: every target must define a rule chunk for every
    # SqlFeature, so a half-landed feature (a chunk in one target but silently
    # missing from another, as TEMPORAL once was) fails the suite instead of
    # disappearing into a tolerant `.get()` lookup.
    assert set(target_mod._FEATURE_RULES) == set(SqlFeature)


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
def test_target_base_block_has_uniform_sections(target_cls: type, person_forum_schema: Callable[..., Any]) -> None:
    # Every target's always-on base block renders the same five-section
    # skeleton with the same headers, regardless of detected features.
    base = build_system_prompt(person_forum_schema(), target_cls(), frozenset())
    for header in ("Data model:", "Core syntax:", "These are NOT valid", "Examples:"):
        assert header in base, f"{target_cls.__name__} base missing section {header!r}"


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
def test_target_base_block_renders_shared_examples(target_cls: type, person_forum_schema: Callable[..., Any]) -> None:
    # Every target shows its translation of the same shared SQL inputs, so the
    # worked examples line up across languages.
    base = build_system_prompt(person_forum_schema(), target_cls(), frozenset())
    assert EX_POINT_LOOKUP_SQL in base
    assert EX_JOIN_FILTER_SQL in base


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
def test_order_limit_chunk_has_worked_example(target_cls: type, person_forum_schema: Callable[..., Any]) -> None:
    # Every target's ORDER_LIMIT chunk carries a worked sort+limit example (parity);
    # it is gated, so it appears only when ORDER_LIMIT is detected.
    sql = "SELECT name FROM table_a ORDER BY value DESC LIMIT 10"
    with_ol = build_system_prompt(person_forum_schema(), target_cls(), frozenset({SqlFeature.ORDER_LIMIT}))
    without_ol = build_system_prompt(person_forum_schema(), target_cls(), frozenset())
    assert sql in with_ol
    assert sql not in without_ol


def test_aql_base_teaches_sort_limit_before_return(person_forum_schema: Callable[..., Any]) -> None:
    # The fatal AQL failure mode (aql-11/aql-12): SORT/LIMIT placed after RETURN.
    # The always-on base block must teach that RETURN terminates the FOR block,
    # with a BAD->GOOD anti-pattern showing the correct ordering.
    base = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset())
    assert "must come BEFORE `RETURN`" in base
    assert "SORT LENGTH(items) DESC LIMIT 10 RETURN" in base  # GOOD ordering shown


def test_aql_base_teaches_junction_table_is_an_edge(person_forum_schema: Callable[..., Any]) -> None:
    # Parity with Cypher/Gremlin: AQL's always-on data model must warn that a
    # junction/link table is an edge collection, not a vertex collection, so the
    # model does not invent `FOR x IN PartSupp`.
    base = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset())
    assert "junction / link table is an EDGE collection" in base
    assert "never `FILTER` on `*key`/`*_id` columns" in base


def test_aql_union_rule_warns_function_not_infix(person_forum_schema: Callable[..., Any]) -> None:
    # aql-13 wasted iterations writing `FOR...RETURN UNION_DISTINCT FOR...RETURN`
    # (SQL-style infix). The UNION chunk must warn it is a function, not infix.
    with_union = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset({SqlFeature.UNION}))
    without_union = build_system_prompt(person_forum_schema(), AqlTarget(), frozenset())
    assert "NOT an infix" in with_union
    assert "NOT an infix" not in without_union
