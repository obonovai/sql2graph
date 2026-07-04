"""Unit tests for the ``make_target`` factory."""

from __future__ import annotations

import pytest

from sql2graph import AqlTarget, CypherTarget, GremlinTarget, make_target


def test_make_target_cypher() -> None:
    t = make_target("cypher")
    assert isinstance(t, CypherTarget)
    assert t.name == "cypher"


def test_make_target_aql() -> None:
    t = make_target("aql")
    assert isinstance(t, AqlTarget)
    assert t.name == "aql"
    section = t.system_prompt_section(frozenset())
    # AQL uses bare edge-collection traversals plus an anti-pattern block,
    # not the named-graph form.
    assert "OUTBOUND" in section
    assert "These are NOT valid AQL" in section


def test_make_target_gremlin() -> None:
    t = make_target("gremlin")
    assert isinstance(t, GremlinTarget)
    assert t.name == "gremlin"


def test_make_target_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown target language"):
        make_target("sparql")
