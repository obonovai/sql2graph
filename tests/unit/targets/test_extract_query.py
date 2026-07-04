"""Unit tests for target ``extract_query`` fence/keyword parsing."""

from __future__ import annotations

import pytest

from sql2graph import AqlTarget, CypherTarget, GremlinTarget

# Target classes for parametrized cross-target parity tests.
_ALL_TARGET_CLASSES = [CypherTarget, AqlTarget, GremlinTarget]


def test_extract_cypher_from_fence() -> None:
    text = "Here you go:\n```cypher\nMATCH (n) RETURN n\n```\nDone."
    assert CypherTarget().extract_query(text) == "MATCH (n) RETURN n"


def test_extract_cypher_from_keyword() -> None:
    text = "Sure!\nMATCH (n:Person) RETURN n.name"
    assert CypherTarget().extract_query(text) == "MATCH (n:Person) RETURN n.name"


def test_extract_cypher_fallback_returns_stripped() -> None:
    assert CypherTarget().extract_query("  not a query  ") == "not a query"


def test_extract_aql_from_fence() -> None:
    text = "```aql\nFOR p IN persons RETURN p\n```"
    assert AqlTarget().extract_query(text) == "FOR p IN persons RETURN p"


def test_extract_aql_from_keyword() -> None:
    text = "OK:\nFOR p IN persons FILTER p.age > 18 RETURN p"
    assert AqlTarget().extract_query(text) == "FOR p IN persons FILTER p.age > 18 RETURN p"


def test_extract_aql_fallback_returns_stripped() -> None:
    assert AqlTarget().extract_query("  nothing here  ") == "nothing here"


def test_extract_gremlin_from_gremlin_fence() -> None:
    text = "Here you go:\n```gremlin\ng.V().hasLabel('Person').valueMap()\n```\nDone."
    assert GremlinTarget().extract_query(text) == "g.V().hasLabel('Person').valueMap()"


def test_extract_gremlin_from_groovy_fence() -> None:
    text = "```groovy\ng.V().has('Person', 'id', 933).valueMap()\n```"
    assert GremlinTarget().extract_query(text) == "g.V().has('Person', 'id', 933).valueMap()"


def test_extract_gremlin_from_keyword() -> None:
    text = "Sure!\ng.V().hasLabel('Person').count()"
    assert GremlinTarget().extract_query(text) == "g.V().hasLabel('Person').count()"


def test_extract_gremlin_fallback_returns_stripped() -> None:
    assert GremlinTarget().extract_query("  not a query  ") == "not a query"


def test_extract_aql_from_mislabeled_arangodb_fence() -> None:
    # Regression: the model fences AQL as ```arangodb (not ```aql). The body
    # must still be extracted cleanly, without the closing ``` leaking in.
    text = "```arangodb\nFOR f IN Forum\n  RETURN f\n```"
    result = AqlTarget().extract_query(text)
    assert result == "FOR f IN Forum\n  RETURN f"
    assert "```" not in result


@pytest.mark.parametrize("target_cls", _ALL_TARGET_CLASSES)
@pytest.mark.parametrize("tag", ["sql", "text", "json"])
def test_extract_handles_arbitrary_fence_tag(target_cls: type, tag: str) -> None:
    # Any info-string after ``` is treated as a fence for every target, so a
    # mislabeled fence never falls through and leaks its closing delimiter.
    text = f"```{tag}\nQUERY BODY HERE\n```"
    result = target_cls().extract_query(text)
    assert result == "QUERY BODY HERE"
    assert "```" not in result


def test_extract_strips_trailing_fence_on_keyword_fallback() -> None:
    # No opening fence, but a stray closing ``` trails the keyword query (the
    # shape that previously reached the validator). It must be stripped.
    text = "FOR p IN persons RETURN p\n```"
    result = AqlTarget().extract_query(text)
    assert result == "FOR p IN persons RETURN p"
    assert "```" not in result
