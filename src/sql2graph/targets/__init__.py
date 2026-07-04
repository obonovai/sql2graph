"""Target graph query languages.

A :class:`TargetLanguage` bundles the three pieces of target-language-specific
knowledge the framework needs during translation:

1. A :attr:`name` (used in prompt scaffolding and result objects).
2. A :meth:`system_prompt_section` block enumerating language-specific
   syntactic rules and idioms.
3. An :meth:`extract_query` routine that pulls a query out of (possibly
   noisy) LLM output.

Three implementations ship: :class:`sql2graph.targets.cypher.CypherTarget`
(Neo4j), :class:`sql2graph.targets.aql.AqlTarget` (ArangoDB), and
:class:`sql2graph.targets.gremlin.GremlinTarget` (Apache TinkerPop /
JanusGraph / Neptune / Cosmos DB Gremlin API). All satisfy the
:class:`TargetLanguage` :class:`~typing.Protocol` structurally; there is
no abstract base class to inherit from.

Adding a further target language (e.g. SPARQL) requires implementing the
Protocol in a new module and extending :func:`make_target` to recognise
its name. Note that
:attr:`sql2graph.state.TranslationState.target_language` is a
``Literal["cypher", "aql", "gremlin"]``; widening that literal is the
one non-Protocol-friendly step in adding a new target.
"""

from __future__ import annotations

from typing import Protocol

from sql2graph.sql_features import SqlFeature
from sql2graph.targets.aql import AqlTarget
from sql2graph.targets.cypher import CypherTarget
from sql2graph.targets.gremlin import GremlinTarget

# Canonical set of target language short names, so callers don't each hardcode it.
VALID_TARGETS: tuple[str, ...] = ("cypher", "aql", "gremlin")


class TargetLanguage(Protocol):
    """Structural type for any target graph query language."""

    @property
    def name(self) -> str: ...

    def system_prompt_section(self, features: frozenset[SqlFeature]) -> str: ...

    def extract_query(self, llm_response: str) -> str: ...

    def repair_hint(self, errors: list[str]) -> str | None:
        """Targeted fix guidance for a class of validator errors, or ``None``.

        The translator passes the latest validation errors here when building a
        fix prompt. A non-``None`` result *replaces* the generic "fix only the
        reported errors, don't restructure" instruction; use it for errors
        whose only valid fix *is* a restructure (e.g. a clause placed after a
        terminal ``RETURN``), which the validator's terse message actively
        misdirects the model away from. Return ``None`` to keep the default.
        """
        ...


def make_target(name: str) -> TargetLanguage:
    """Construct a :class:`TargetLanguage` by short name.

    Args:
        name: ``"cypher"``, ``"aql"``, or ``"gremlin"``.
    """
    if name == "cypher":
        return CypherTarget()
    if name == "aql":
        return AqlTarget()
    if name == "gremlin":
        return GremlinTarget()
    raise ValueError(f"Unknown target language: {name!r}. Supported: {', '.join(VALID_TARGETS)}.")


__all__ = ["VALID_TARGETS", "AqlTarget", "CypherTarget", "GremlinTarget", "TargetLanguage", "make_target"]
