"""Target graph query languages.

A :class:`TargetLanguage` bundles the three pieces of target-language-specific
knowledge the framework needs during translation:

1. A :attr:`name` (used in prompt scaffolding and result objects).
2. A :meth:`system_prompt_section` block enumerating language-specific
   syntactic rules and idioms.
3. An :meth:`extract_query` routine that pulls a query out of (possibly
   noisy) LLM output.

Two implementations ship: :class:`rows2graph.targets.cypher.CypherTarget`
(Neo4j) and :class:`rows2graph.targets.aql.AqlTarget` (ArangoDB). Both
satisfy the :class:`TargetLanguage` :class:`~typing.Protocol` structurally —
there is no abstract base class to inherit from.

Adding a third target language (e.g. Gremlin, SPARQL) requires implementing
the Protocol in a new module and extending :func:`make_target` to recognise
its name. Note that
:attr:`rows2graph.state.TranslationState.target_language` is currently a
``Literal["cypher", "aql"]`` — widening that literal is the one
non-Protocol-friendly step in adding a new target.
"""

from __future__ import annotations

from typing import Protocol

from rows2graph.targets.aql import AqlTarget
from rows2graph.targets.cypher import CypherTarget


class TargetLanguage(Protocol):
    """Structural type for any target graph query language."""

    @property
    def name(self) -> str: ...

    def system_prompt_section(self) -> str: ...

    def extract_query(self, llm_response: str) -> str: ...


def make_target(name: str, *, graph_name: str | None = None) -> TargetLanguage:
    """Construct a :class:`TargetLanguage` by short name.

    Args:
        name: ``"cypher"`` or ``"aql"``.
        graph_name: Only used by AQL — the named graph in the target
            ArangoDB instance, woven into the system prompt. Ignored for
            Cypher.
    """
    if name == "cypher":
        return CypherTarget()
    if name == "aql":
        return AqlTarget(graph_name=graph_name)
    raise ValueError(f"Unknown target language: {name!r}. Supported: 'cypher', 'aql'.")


__all__ = ["AqlTarget", "CypherTarget", "TargetLanguage", "make_target"]
