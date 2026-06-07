"""Cypher target language (Neo4j).

Provides :class:`CypherTarget`, which contributes the Cypher-specific section
of the system prompt and extracts a Cypher query from a (possibly noisy) LLM
response. The extractor accepts code-fenced (```` ```cypher ... ``` ````) and
keyword-led (``MATCH ... RETURN ...``) responses, in that order of preference.
"""

from __future__ import annotations

import re

# Match a fenced code block tagged ``cypher`` (case-insensitive) or untagged.
# The first captured group is the body of the fence.
_FENCE_RE = re.compile(
    r"```(?:cypher|CYPHER)?\s*\n(.*?)```",
    re.DOTALL,
)

# Match the first Cypher-starting keyword at the start of any line. Used as a
# fallback when the model does not wrap its response in a code fence.
_START_RE = re.compile(
    r"^(MATCH|CREATE|MERGE|RETURN|WITH|UNWIND|CALL|OPTIONAL\s+MATCH|"
    r"DETACH\s+DELETE|DELETE|SET|REMOVE|FOREACH|LOAD\s+CSV)",
    re.IGNORECASE | re.MULTILINE,
)


class CypherTarget:
    """Cypher (Neo4j) target language implementation.

    Implements :class:`rows2graph.targets.TargetLanguage` structurally.
    """

    @property
    def name(self) -> str:
        return "cypher"

    def system_prompt_section(self) -> str:
        """Cypher-specific section appended to the system prompt.

        The SQL-LIKE → Cypher operator mapping is spelled out explicitly
        because empirical testing showed that small models often produce
        ``=~ '%foo%'`` (mixing SQL wildcards with Cypher regex syntax) when
        translating ``LIKE`` clauses; the explicit table is the single most
        effective prompt addition for translation accuracy on TPC-H queries.
        """
        return (
            "Generate valid Cypher queries for Neo4j.\n"
            "- Use `MATCH` for reading, `CREATE`/`MERGE` for writing.\n"
            "- Use relationship patterns like `(a)-[:REL_TYPE]->(b)`.\n"
            "- Use `WHERE` for filtering, `RETURN` for output.\n"
            "- Start the query with one of: MATCH, CREATE, MERGE, RETURN, WITH, "
            "UNWIND, CALL, OPTIONAL MATCH, DETACH DELETE, DELETE, SET, REMOVE, "
            "FOREACH, LOAD CSV.\n"
            "\n"
            "SQL string-pattern predicates → Cypher: SQL LIKE/ILIKE patterns use "
            "`%` (any sequence) and `_` (any single char) as wildcards. Cypher's "
            "`=~` operator uses Java regex — `%` is a literal percent sign there, "
            "not a wildcard. Translate using Cypher's dedicated string operators:\n"
            "- `col LIKE '%x%'`           → `col CONTAINS 'x'`\n"
            "- `col LIKE 'x%'`            → `col STARTS WITH 'x'`\n"
            "- `col LIKE '%x'`            → `col ENDS WITH 'x'`\n"
            "- `col LIKE 'x'` (no wildcards) → `col = 'x'`\n"
            "- `col ILIKE '%x%'`          → `toLower(col) CONTAINS toLower('x')`\n"
            "- `col NOT LIKE '%x%'`       → `NOT col CONTAINS 'x'`\n"
            "Only fall back to `=~` when the pattern needs regex features beyond "
            "CONTAINS/STARTS WITH/ENDS WITH. In that case, convert `%` → `.*` and "
            "`_` → `.` explicitly. Never leave SQL-style `%`/`_` wildcards inside "
            "a Cypher `=~` string."
        )

    def extract_query(self, llm_response: str) -> str:
        """Pull a Cypher query out of (possibly noisy) LLM output.

        Resolution order: (1) any fenced code block; (2) the first line that
        starts with a Cypher keyword; (3) the whole response, stripped.
        """
        match = _FENCE_RE.search(llm_response)
        if match:
            return match.group(1).strip()

        match = _START_RE.search(llm_response)
        if match:
            return llm_response[match.start() :].strip()

        return llm_response.strip()
