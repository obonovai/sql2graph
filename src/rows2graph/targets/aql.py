"""AQL target language (ArangoDB Query Language).

Provides :class:`AqlTarget`, which contributes the AQL-specific section of
the system prompt and extracts an AQL query from a (possibly noisy) LLM
response.

AQL differs structurally from Cypher in three ways that matter for prompt
engineering:

1. *Nodes* are documents in named *vertex collections*; *edges* are documents
   in named *edge collections*. The framework adopts the convention that
   vertex collection names equal node labels and edge collection names equal
   edge types from the user's schema mapping.
2. *Graph traversals* go through a named graph: ``FOR v, e, p IN <min>..<max>
   OUTBOUND/INBOUND/ANY <startVertex> GRAPH "<graph>"``. The graph name is a
   property of the deployment and must be threaded into the prompt; this is
   the role of the :attr:`graph_name` constructor argument.
3. *Predicate* and *output* keywords are ``FILTER`` and ``RETURN``, not
   ``WHERE`` and ``RETURN``.
"""

from __future__ import annotations

import re

# Match a fenced code block tagged ``aql`` (case-insensitive) or untagged.
_FENCE_RE = re.compile(
    r"```(?:aql|AQL)?\s*\n(.*?)```",
    re.DOTALL,
)

# Match the first AQL top-level keyword at the start of any line.
_START_RE = re.compile(
    r"^(FOR|LET|INSERT|UPDATE|REPLACE|REMOVE|UPSERT|WITH|RETURN)\b",
    re.IGNORECASE | re.MULTILINE,
)


class AqlTarget:
    """AQL (ArangoDB) target language implementation.

    The ``graph_name`` argument is the name of the *named graph* registered
    in the target ArangoDB instance. It is woven into the system prompt so
    the LLM produces correct ``GRAPH "<name>"`` clauses in traversals. When
    ``graph_name`` is ``None`` (e.g. when running with ``--validation
    syntax`` and no deployment is being targeted), the prompt falls back to
    the wording "the configured named graph", and the LLM is expected to
    leave a placeholder that the caller fills in later.
    """

    def __init__(self, graph_name: str | None = None) -> None:
        self._graph_name = graph_name

    @property
    def name(self) -> str:
        return "aql"

    def system_prompt_section(self) -> str:
        if self._graph_name:
            graph_hint = f'The named graph is "{self._graph_name}".'
        else:
            graph_hint = "Use the configured named graph for traversals."
        return (
            "Generate valid AQL (ArangoDB Query Language) queries.\n"
            "- Treat each node label from the schema as a vertex collection name.\n"
            "- Treat each edge type from the schema as an edge collection name.\n"
            f"- {graph_hint}\n"
            "- Use `FOR v, e, p IN <min>..<max> OUTBOUND/INBOUND/ANY "
            '<startVertex> GRAPH "<graph>"` for graph traversals; '
            "do NOT use Cypher-style `MATCH (a)-[:REL]->(b)` patterns.\n"
            "- Use `FILTER` (not `WHERE`) for predicates.\n"
            "- Use `RETURN` for output (terminates each query level).\n"
            "- Aggregations: `COLLECT ... WITH COUNT INTO counter` for counts; "
            "`COLLECT key = expr AGGREGATE total = SUM(...)` for grouped sums.\n"
            "- Sorting: `SORT expr ASC|DESC`. Limiting: `LIMIT n` or `LIMIT offset, n`.\n"
            "- For SQL LIKE patterns: use the `LIKE(text, pattern, "
            "case_insensitive)` function — e.g. `FILTER LIKE(p.name, "
            "\"%foo%\")` for `name LIKE '%foo%'`. Do NOT use Cypher's "
            "`CONTAINS`/`STARTS WITH`/`ENDS WITH` keywords.\n"
            "- Start the query with one of: FOR, LET, INSERT, UPDATE, REPLACE, "
            "REMOVE, UPSERT, WITH, RETURN."
        )

    def extract_query(self, llm_response: str) -> str:
        """Pull an AQL query out of (possibly noisy) LLM output.

        Resolution order: (1) any fenced code block; (2) the first line that
        starts with an AQL keyword; (3) the whole response, stripped.
        """
        match = _FENCE_RE.search(llm_response)
        if match:
            return match.group(1).strip()

        match = _START_RE.search(llm_response)
        if match:
            return llm_response[match.start() :].strip()

        return llm_response.strip()
