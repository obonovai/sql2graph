"""Cypher target language (Neo4j).

Provides :class:`CypherTarget`, which contributes the Cypher-specific section
of the system prompt and extracts a Cypher query from a (possibly noisy) LLM
response. The extractor accepts code-fenced (```` ```cypher ... ``` ````) and
keyword-led (``MATCH ... RETURN ...``) responses, in that order of preference.

The prompt section is assembled from a fixed base block and a dictionary of
per-:class:`~rows2graph.sql_features.SqlFeature` rule chunks; only the chunks
matching features detected in the input SQL are appended. This keeps the
prompt focused (small models get distracted by irrelevant rules) and makes
adding a new operation-specific rule a localized change rather than a
lengthening of the always-on block.
"""

from __future__ import annotations

import re

from rows2graph.sql_features import SqlFeature

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

# Always emitted — covers what reading a Cypher query requires regardless of
# the source SQL shape.
_BASE_RULES = (
    "Generate valid Cypher queries for Neo4j.\n"
    "- Use `MATCH` for reading, `CREATE`/`MERGE` for writing.\n"
    "- Use relationship patterns like `(a)-[:REL_TYPE]->(b)`.\n"
    "- Use `WHERE` for filtering, `RETURN` for output.\n"
    "- Start the query with one of: MATCH, CREATE, MERGE, RETURN, WITH, "
    "UNWIND, CALL, OPTIONAL MATCH, DETACH DELETE, DELETE, SET, REMOVE, "
    "FOREACH, LOAD CSV."
)

# The LIKE-mapping table is unchanged from the pre-refactor prompt — empirical
# testing on TPC-H showed it was the single most effective addition for
# translation accuracy on small models.
_LIKE_RULES = (
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

_JOIN_RULES = (
    "SQL JOINs → Cypher relationship traversals: realize each `JOIN` as a "
    "pattern segment between node variables, using the relationship type "
    "and direction declared in the schema. Use `OPTIONAL MATCH` for outer "
    "joins (LEFT/RIGHT/FULL). Do NOT translate JOIN ON predicates into "
    "`WHERE` conditions on foreign-key columns — the schema's relationship "
    "already encodes the join."
)

_AGGREGATION_RULES = (
    "SQL aggregations → Cypher: use `count(...)`, `sum(...)`, `avg(...)`, "
    "`min(...)`, `max(...)`, `collect(...)`. Cypher has NO explicit "
    "`GROUP BY` clause — grouping is implicit in the non-aggregate "
    "expressions of `RETURN` (or of the upstream `WITH`). To express SQL "
    "`HAVING`, project aggregates through a `WITH` clause and add a `WHERE` "
    "after it: `WITH k, count(*) AS c WHERE c > 5 RETURN k, c`."
)

_ORDER_LIMIT_RULES = (
    "Sorting and paging: use `ORDER BY <expr> ASC|DESC`, `SKIP n` for "
    "offsets (note: SKIP, not OFFSET), and `LIMIT n`. The order is "
    "`RETURN ... ORDER BY ... SKIP ... LIMIT ...`."
)

_CTE_RULES = (
    "SQL CTEs (`WITH name AS (...)`) → Cypher: chain `WITH` clauses that "
    "project the intermediate results forward. Cypher's `WITH` is the "
    "pipeline operator (different meaning from SQL's `WITH`) — each `WITH` "
    "is a step in a pipeline. Inline correlated logic instead of trying to "
    "re-create a named CTE block."
)

_UNION_RULES = (
    "Set operations: use `UNION` (de-duplicates) or `UNION ALL` (keeps "
    "duplicates) between two complete `MATCH ... RETURN` statements. The "
    "return columns of both sides MUST have identical names and order. "
    "Cypher has no native `INTERSECT`/`EXCEPT`; emulate with `WITH` + "
    "`WHERE` predicates if needed."
)

_WINDOW_RULES = (
    "SQL window functions (`OVER (PARTITION BY ... ORDER BY ...)`) have no "
    "direct Cypher equivalent. Emulate by projecting through `WITH`, "
    "`collect`-ing into an ordered list, and `UNWIND`-ing with an index — "
    "e.g. `WITH partition_key, collect(row) AS rows ... UNWIND range(0, "
    "size(rows)-1) AS i ...`. If APOC is available, `apoc.coll.*` helpers "
    "simplify ranking."
)

_CASE_RULES = (
    "Conditional expressions: Cypher accepts `CASE WHEN ... THEN ... ELSE "
    "... END` with the same syntax as SQL. Both the searched form "
    "(`CASE WHEN cond THEN ...`) and the simple form (`CASE expr WHEN val "
    "THEN ...`) are supported."
)

_SUBQUERY_RULES = (
    "SQL subqueries → Cypher: use a `CALL { ... }` subquery for correlated "
    "or scalar results, and `EXISTS { MATCH ... WHERE ... }` for "
    "`EXISTS`/`IN` predicates. A `CALL` subquery can `RETURN` values into "
    "the outer scope. FROM-subqueries flatten into the main `MATCH` "
    "pattern in most cases."
)

_DISTINCT_RULES = (
    "`SELECT DISTINCT` → `RETURN DISTINCT ...`. Inside aggregates: "
    "`COUNT(DISTINCT x)` → `count(DISTINCT x)`."
)

_FEATURE_RULES: dict[SqlFeature, str] = {
    SqlFeature.LIKE: _LIKE_RULES,
    SqlFeature.JOIN: _JOIN_RULES,
    SqlFeature.AGGREGATION: _AGGREGATION_RULES,
    SqlFeature.ORDER_LIMIT: _ORDER_LIMIT_RULES,
    SqlFeature.CTE: _CTE_RULES,
    SqlFeature.UNION: _UNION_RULES,
    SqlFeature.WINDOW: _WINDOW_RULES,
    SqlFeature.CASE: _CASE_RULES,
    SqlFeature.SUBQUERY: _SUBQUERY_RULES,
    SqlFeature.DISTINCT: _DISTINCT_RULES,
}


class CypherTarget:
    """Cypher (Neo4j) target language implementation.

    Implements :class:`rows2graph.targets.TargetLanguage` structurally.
    """

    @property
    def name(self) -> str:
        return "cypher"

    def system_prompt_section(self, features: frozenset[SqlFeature]) -> str:
        """Cypher-specific section appended to the system prompt.

        The base block is always emitted; the per-feature rule chunks are
        appended in :class:`~rows2graph.sql_features.SqlFeature` declaration
        order — this gives a stable, readable layout where related rules
        (e.g. LIKE then JOIN then AGGREGATION) appear in the same sequence
        across translations.
        """
        chunks = [_BASE_RULES, *(_FEATURE_RULES[feat] for feat in SqlFeature if feat in features)]
        return "\n\n".join(chunks)

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
