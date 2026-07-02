"""Shared fixtures for the static (unit) suite.

Schema builders are exposed as *factory* fixtures: each returns a zero-arg
callable so a test can request a fresh :class:`SchemaMapping` at the call site
(``person_forum_schema()``), matching the old module-level helper's call syntax
and giving each construction an independent instance.

The scripted fake-LLM clients live here (rather than in a per-subdir conftest)
because they are used across the unit tree - both the translator loop tests and
the prompt-assembly test that drives a translator to inspect its system message.
They are factory fixtures returning the double *class*, so a test constructs one
with a call-site-specific response queue: ``scripted_llm(["MATCH ... RETURN p"])``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from rows2graph import EdgeMapping, NodeMapping, SchemaMapping
from tests.unit._doubles import ScriptedAsyncLLM, ScriptedLLM


@pytest.fixture
def scripted_llm() -> type[ScriptedLLM]:
    """Factory for the sync scripted LLM double: ``scripted_llm([resp, ...])``."""
    return ScriptedLLM


@pytest.fixture
def scripted_async_llm() -> type[ScriptedAsyncLLM]:
    """Factory for the async scripted LLM double: ``scripted_async_llm([resp, ...])``."""
    return ScriptedAsyncLLM


@pytest.fixture
def person_forum_schema() -> Callable[[], SchemaMapping]:
    """Factory for a two-node (Person, Forum) one-edge (Person-KNOWS-Person) mapping."""

    def _build() -> SchemaMapping:
        return SchemaMapping(
            nodes=[
                NodeMapping(
                    label="Person",
                    source_table="persons",
                    primary_key="id",
                    properties={"name": "full_name"},
                ),
                NodeMapping(
                    label="Forum",
                    source_table="forums",
                    primary_key="id",
                    properties={"title": "title"},
                ),
            ],
            edges=[
                EdgeMapping(
                    type="KNOWS",
                    source_node="Person",
                    target_node="Person",
                    source_table="knows",
                    source_foreign_key="from_id",
                    target_primary_key="id",
                )
            ],
        )

    return _build


@pytest.fixture
def forum_no_title_schema() -> Callable[[], SchemaMapping]:
    """Factory for a single-node Forum mapping whose only property is its own id."""

    def _build() -> SchemaMapping:
        return SchemaMapping(
            nodes=[NodeMapping(label="Forum", source_table="forum", primary_key="id", properties={"id": "id"})],
            edges=[],
        )

    return _build
