"""Unit tests for offline syntax validators (Cypher / Gremlin / AQL)."""

from __future__ import annotations

import pytest

from rows2graph import (
    AqlSyntaxValidator,
    CypherSyntaxValidator,
    GremlinSyntaxValidator,
)


def test_cypher_syntax_passes_valid_query() -> None:
    assert CypherSyntaxValidator().validate("MATCH (n) RETURN n") == []


def test_cypher_syntax_passes_single_quoted_string_with_bracket() -> None:
    # A single-quoted string containing a paren is valid Cypher. The old
    # bracket-counting heuristic wrongly rejected it; the grammar accepts it.
    assert CypherSyntaxValidator().validate("MATCH (n {name: 'a)b'}) RETURN n") == []


def test_cypher_syntax_flags_bad_start() -> None:
    assert CypherSyntaxValidator().validate("WHERE x = 1")


def test_cypher_syntax_flags_malformed_pattern() -> None:
    assert CypherSyntaxValidator().validate("MATCH (n)-[r]-( RETURN n")


def test_cypher_syntax_flags_missing_projection() -> None:
    assert CypherSyntaxValidator().validate("MATCH (n) RETURN")


def test_cypher_syntax_flags_empty_query() -> None:
    assert CypherSyntaxValidator().validate("   ") == ["Query is empty"]


def test_gremlin_syntax_passes_valid_query() -> None:
    assert GremlinSyntaxValidator().validate("g.V().hasLabel('Person').valueMap()") == []


def test_gremlin_syntax_passes_with_anonymous_traversal() -> None:
    query = "g.V().hasLabel('Person').where(__.out('KNOWS')).valueMap()"
    assert GremlinSyntaxValidator().validate(query) == []


def test_gremlin_syntax_flags_bad_start() -> None:
    assert GremlinSyntaxValidator().validate("MATCH (n) RETURN n")


def test_gremlin_syntax_flags_unbalanced_parens() -> None:
    assert GremlinSyntaxValidator().validate("g.V().hasLabel('Person'.valueMap()")


def test_gremlin_syntax_flags_unbalanced_brackets() -> None:
    assert GremlinSyntaxValidator().validate("g.V().has('age', P.within([1, 2).count()")


def test_gremlin_syntax_flags_trailing_dot() -> None:
    assert GremlinSyntaxValidator().validate("g.V().hasLabel('Person').")


def test_gremlin_syntax_flags_empty_query() -> None:
    assert GremlinSyntaxValidator().validate("   ") == ["Query is empty"]


def test_gremlin_syntax_flags_unbalanced_quotes() -> None:
    assert GremlinSyntaxValidator().validate("g.V().has('label, 'x').count()")


@pytest.mark.parametrize(
    "query",
    [
        "RETURN 1",
        "RETURN DISTINCT x",
        "FOR u IN users FILTER u.age >= 20 AND u.age < 30 SORT u.name DESC LIMIT 10 RETURN u.name",
        "FOR u IN users LET n = u.name RETURN { name: n, id: u._id }",
        "FOR u IN users COLLECT city = u.city INTO g RETURN { city, count: LENGTH(g) }",
        "FOR u IN users COLLECT WITH COUNT INTO total RETURN total",
        "FOR u IN users COLLECT AGGREGATE ma = MAX(u.age) RETURN ma",
        "INSERT { name: 'x' } INTO users",
        "UPDATE 'key' WITH { a: 1 } IN users OPTIONS { waitForSync: true }",
        "UPSERT { a: 1 } INSERT { a: 1 } UPDATE { b: 2 } IN users",
        "FOR v, e, p IN 1..3 OUTBOUND 'start/1' GRAPH 'g' RETURN v",
        "FOR v, e IN OUTBOUND SHORTEST_PATH 'a/1' TO 'b/2' GRAPH 'g' RETURN v",
        "RETURN u.items[* FILTER CURRENT.age > 2]",
        "RETURN x NOT IN [1, 2, 3]",
        "RETURN a ? b : c",
        "FOR d IN @@coll FILTER d.k == @value RETURN d",
        "RETURN LENGTH(FOR x IN c RETURN x)",
        "WITH users FOR u IN users RETURN u",
    ],
)
def test_aql_syntax_passes_valid_query(query: str) -> None:
    assert AqlSyntaxValidator().validate(query) == []


@pytest.mark.parametrize(
    "query",
    [
        "FOR u IN RETURN u",
        "RETURN",
        "RETURN (1 + )",
        "RETURN [1, 2",
        "MATCH (n) RETURN n",
        "SELECT * FROM users",
        "FOR u IN users RETURN u EXTRA",
    ],
)
def test_aql_syntax_flags_invalid_query(query: str) -> None:
    assert AqlSyntaxValidator().validate(query)


def test_aql_syntax_flags_empty_query() -> None:
    assert AqlSyntaxValidator().validate("   ") == ["Query is empty"]


def test_async_cypher_syntax_validator_matches_sync() -> None:
    """Async syntax validator returns the same errors as its sync sibling."""
    import asyncio

    from rows2graph.validators.cypher.syntax import AsyncCypherSyntaxValidator

    async_v = AsyncCypherSyntaxValidator()
    sync_v = CypherSyntaxValidator()
    cases = [
        "MATCH (p:Person) RETURN p",
        "MATCH (p:Person",  # malformed: unbalanced parenthesis
        "",  # empty
        "MATCH (p:Person RETURN p",  # unbalanced paren
    ]
    for q in cases:
        sync_errors = sync_v.validate(q)
        async_errors = asyncio.run(async_v.validate(q))
        assert async_errors == sync_errors, f"divergence for query: {q!r}"
