"""Cypher target language (Neo4j).

Provides :class:`CypherTarget`, which contributes the Cypher-specific section
of the system prompt and extracts a Cypher query from a (possibly noisy) LLM
response. The extractor accepts code-fenced (```` ```cypher ... ``` ````) and
keyword-led (``MATCH ... RETURN ...``) responses, in that order of preference.

The prompt section is built from the shared schema in
:mod:`rows2graph.targets._schema`: a :class:`~rows2graph.targets._schema.BaseRules`
block (always emitted) plus a dictionary of per-:class:`~rows2graph.sql_features.SqlFeature`
:class:`~rows2graph.targets._schema.FeatureRule` chunks; only the chunks matching
features detected in the input SQL are appended. The same five-section base
skeleton and the shared worked-example inputs are used by every target, so the
three stay structurally aligned.
"""

from __future__ import annotations

import re

from rows2graph.sql_features import SqlFeature
from rows2graph.targets._schema import (
    EX_GROUPED_COUNT_SQL,
    EX_JOIN_FILTER_SQL,
    EX_POINT_LOOKUP_SQL,
    AntiPattern,
    BaseRules,
    Example,
    FeatureRule,
    compose_section,
    extract_query,
)

# Match the first Cypher-starting keyword at the start of any line. Used as a
# fallback when the model does not wrap its response in a code fence.
_START_RE = re.compile(
    r"^(MATCH|CREATE|MERGE|RETURN|WITH|UNWIND|CALL|OPTIONAL\s+MATCH|"
    r"DETACH\s+DELETE|DELETE|SET|REMOVE|FOREACH|LOAD\s+CSV)",
    re.IGNORECASE | re.MULTILINE,
)

# Always emitted. Covers the Cypher data model, core syntax/operators, an
# explicit "these are NOT valid Cypher" anti-pattern block with bad->good
# rewrites, and worked SQL->Cypher examples — the same five-section structure
# every target's base block uses to keep small local models on the rails rather
# than emitting SQL-flavoured guesses. Every claim is verified against the
# Neo4j 5 Cypher manual. NOTE: the substrings "CONTAINS", "window function", and
# "datetime(" are deliberately kept OUT of this always-on block — they gate the
# LIKE, WINDOW, and TEMPORAL feature chunks, and leaking them here would defeat
# the focused-prompt tests.
_BASE_RULES = BaseRules(
    language="Cypher",
    output_mandate=(
        "Generate ONE valid Cypher query for Neo4j 5 for the schema above. Output "
        "ONLY the query — no prose, no explanation, no markdown fences, no "
        "alternative versions, and nothing before or after the query."
    ),
    data_model=[
        "- Each NODE label is a node (e.g. `Customer`, `Order`). Read it with `MATCH (c:Customer)`.",
        "- Each EDGE type is a DIRECTED relationship, written `(a)-[:PLACED]->(b)`. "
        "Follow the schema's direction; reverse the arrow (`<-[:PLACED]-`) to traverse against it.",
        "- A junction / link table is an EDGE, not a node. Do not `MATCH` a node for it — "
        "realize it as the relationship between the two tables it links.",
        "- Use the graph PROPERTY names from the schema (e.g. `firstName`), NOT the "
        "original SQL column names (e.g. `first_name`).",
        "- Foreign-key columns are NOT stored as properties. A join on a FK is the "
        "relationship itself — never filter on `*key`/`*_id` columns in `WHERE`.",
    ],
    core_syntax=[
        "- Read with `MATCH`; filter with `WHERE`; output with `RETURN`. A SQL alias "
        "(`col AS name`) becomes `RETURN expr AS name`.",
        "- Equality is `=` and inequality is `<>` (never `==` or `!=`). Combine predicates with `AND` / `OR` / `NOT`.",
        "- For a point lookup, prefer the inline property map: `MATCH (p:Person {id: 933})` "
        "rather than `MATCH (p:Person) WHERE p.id = 933` (both are valid; the map form is more idiomatic).",
        "- Functions are camelCase: `toUpper(s)` / `toLower(s)` (NOT `UPPER`/`LOWER`), "
        "`toInteger(x)` / `toFloat(x)` for casts, `size(s)` for the length of a string or "
        "list, `coalesce(a, b)` for the first non-null. Concatenate strings with `+`.",
        "- `count(x)` counts non-null values of `x`; `count(*)` counts rows. Sorting/paging: "
        "`RETURN ... ORDER BY ... SKIP n LIMIT n` (it is `SKIP`, not `OFFSET`).",
    ],
    anti_patterns=[
        AntiPattern(bad="`==` or `!=`", good="use `=` and `<>`"),
        AntiPattern(bad="`UPPER(s)` / `LOWER(s)` / `LEN(s)`", good="use `toUpper(s)` / `toLower(s)` / `size(s)`"),
        AntiPattern(bad="`SELECT ... FROM ...` — this is SQL", good="read with `MATCH (x:Label)`"),
        AntiPattern(
            bad="a `WHERE` join on key columns such as `WHERE o.custkey = c.custkey`",
            good="the schema relationship `(c)-[:PLACED]->(o)` already encodes that join — "
            "write the pattern, not the key predicate",
            bad_example="MATCH (c:Customer), (o:Order) WHERE o.custkey = c.custkey",
            good_example="MATCH (c:Customer)-[:PLACED]->(o:Order)",
        ),
        AntiPattern(
            bad="a standalone node for a junction table",
            bad_example="MATCH (c:Customer)-[:R]->(ph:PartSupp)-[:R2]->(p:Part)",
            good_example="MATCH (s:Supplier)-[:SUPPLIES]->(p:Part)",
        ),
    ],
    examples=[
        Example(
            sql=EX_POINT_LOOKUP_SQL,
            query="MATCH (p:Person {id: 933})\nRETURN p.id, p.firstName",
            label="point lookup",
        ),
        Example(
            sql=EX_JOIN_FILTER_SQL,
            query="MATCH (s:Supplier)-[:LOCATED_IN]->(n:Nation)\nWHERE s.acctbal > 5000\nRETURN s.name, n.name AS nation",
            label="single join + filter",
        ),
    ],
)

# The LIKE-mapping table is unchanged from the pre-refactor prompt — empirical
# testing on TPC-H showed it was the single most effective addition for
# translation accuracy on small models.
_LIKE_RULES = FeatureRule(
    body=(
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
)

_JOIN_RULES = FeatureRule(
    body=(
        "SQL JOINs → Cypher relationship traversals: realize each `JOIN` as a "
        "pattern segment between node variables, using the relationship type and "
        "direction from the schema. Use `OPTIONAL MATCH` for outer joins "
        "(LEFT/RIGHT/FULL). Do NOT translate JOIN ON predicates into `WHERE` "
        "conditions on foreign-key columns — the schema's relationship already "
        "encodes the join.\n"
        "- Through-node join: when two tables join via foreign keys that both "
        "reference a SHARED parent table (e.g. customer and supplier both carry "
        "`nationkey`), traverse THROUGH the shared node with one leg reversed:\n"
        "    MATCH (s:Supplier)-[:LOCATED_IN]->(n:Nation)<-[:LOCATED_IN]-(c:Customer)\n"
        "- Multi-path join: when several joins fan out from the same table, write "
        "comma-separated patterns that REUSE the bound variable instead of "
        "repeating its label:\n"
        "    MATCH (c:Customer)-[:LOCATED_IN]->(n:Nation),\n"
        "          (c)-[:PLACED]->(o:Order)-[:CONTAINS]->(li:LineItem)\n"
        "- Relationship properties: to read a column that lives on the JUNCTION/"
        "link table (the edge), bind the relationship variable and read it like a "
        "property: `MATCH (p1:Person)-[k:KNOWS]->(p2:Person) RETURN "
        "k.creationDate`."
    )
)

_AGGREGATION_RULES = FeatureRule(
    body=(
        "SQL aggregations → Cypher: use `count(...)`, `sum(...)`, `avg(...)`, "
        "`min(...)`, `max(...)`, `collect(...)`. Cypher has NO `GROUP BY` clause — "
        "grouping is implicit in the non-aggregate expressions of `RETURN` (or of "
        "the upstream `WITH`): list the group keys alongside the aggregate and they "
        "become the grouping.\n"
        "- `count(x)` ignores nulls, so it counts only the rows where `x` is "
        "present; `count(*)` counts every row. For a SQL `LEFT JOIN ... COUNT(...)` "
        "use `OPTIONAL MATCH` plus `count(var)` on the optional variable — "
        "unmatched parents then count 0, which is the correct LEFT-JOIN count:\n"
        "    MATCH (s:Supplier)\n"
        "    OPTIONAL MATCH (s)-[:SUPPLIES]->(p:Part)\n"
        "    RETURN s.suppkey, count(p) AS supplied_part_count\n"
        "- To group by a node (not just a scalar), carry the node variable through "
        "`WITH`: `MATCH (c:Customer)-[:PLACED]->(o:Order), (c)-[:LOCATED_IN]->"
        "(n:Nation) WITH c, n, count(o) AS cnt RETURN c.name, n.name, cnt`.\n"
        "- SQL `HAVING` → project the aggregate through `WITH`, then add a `WHERE` "
        "after it: `WITH p, count(friend) AS c WHERE c > 5 RETURN p, c`."
    ),
    example=Example(
        sql=EX_GROUPED_COUNT_SQL,
        query="MATCH (p:Part)\nRETURN p.brand AS brand, count(p) AS c",
        label="grouped count",
    ),
)

_ORDER_LIMIT_RULES = FeatureRule(
    body=(
        "Sorting and paging: use `ORDER BY <expr> ASC|DESC`, `SKIP n` for "
        "offsets (note: SKIP, not OFFSET), and `LIMIT n`. The order is "
        "`RETURN ... ORDER BY ... SKIP ... LIMIT ...`."
    ),
    example=Example(
        sql="SELECT name FROM supplier ORDER BY acctbal DESC LIMIT 10",
        query="MATCH (s:Supplier)\nRETURN s.name\nORDER BY s.acctbal DESC\nLIMIT 10",
        label="top-N",
    ),
)

_CTE_RULES = FeatureRule(
    body=(
        "SQL CTEs (`WITH name AS (...)`) → Cypher: chain `WITH` clauses that "
        "project the intermediate results forward. Cypher's `WITH` is the "
        "pipeline operator (different meaning from SQL's `WITH`) — each `WITH` "
        "is a step in a pipeline. Inline correlated logic instead of trying to "
        "re-create a named CTE block."
    )
)

_UNION_RULES = FeatureRule(
    body=(
        "Set operations: use `UNION` (de-duplicates) or `UNION ALL` (keeps "
        "duplicates) between two complete `MATCH ... RETURN` statements. The "
        "return columns of both sides MUST have identical names and order. "
        "Cypher has no native `INTERSECT`/`EXCEPT`; emulate with `WITH` + "
        "`WHERE` predicates if needed."
    )
)

_WINDOW_RULES = FeatureRule(
    body=(
        "SQL window functions (`OVER (PARTITION BY ... ORDER BY ...)`) have no "
        "direct Cypher equivalent. Emulate by projecting through `WITH`, "
        "`collect`-ing into an ordered list, and `UNWIND`-ing with an index — "
        "e.g. `WITH partition_key, collect(row) AS rows ... UNWIND range(0, "
        "size(rows)-1) AS i ...`. If APOC is available, `apoc.coll.*` helpers "
        "simplify ranking."
    )
)

_CASE_RULES = FeatureRule(
    body=(
        "Conditional expressions: Cypher accepts `CASE WHEN ... THEN ... ELSE "
        "... END` with the same syntax as SQL. Both the searched form "
        "(`CASE WHEN cond THEN ...`) and the simple form (`CASE expr WHEN val "
        "THEN ...`) are supported."
    )
)

_SUBQUERY_RULES = FeatureRule(
    body=(
        "SQL subqueries → Cypher: use a `CALL { ... }` subquery for correlated or "
        "scalar results (it can `RETURN` values into the outer scope), and an "
        "existential subquery `EXISTS { MATCH ... WHERE ... }` for `EXISTS` / `IN` "
        "predicates. FROM-subqueries usually flatten into the main `MATCH` "
        "pattern.\n"
        "- For `(NOT) EXISTS` on a single relationship, prefer the pattern-"
        "predicate shorthand: put the path pattern directly in `WHERE`. "
        "`WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.custkey = c.custkey)` → "
        "`MATCH (c:Customer) WHERE NOT (c)-[:PLACED]->(:Order) RETURN ...`. The "
        "positive form drops the `NOT`: `WHERE (c)-[:PLACED]->(:Order)`. Use the "
        "fuller `EXISTS { MATCH ... }` form only when the inner side needs its own "
        "`WHERE` or multiple hops."
    )
)

_DISTINCT_RULES = FeatureRule(
    body=("`SELECT DISTINCT` → `RETURN DISTINCT ...`. Inside aggregates: `COUNT(DISTINCT x)` → `count(DISTINCT x)`.")
)

_TEMPORAL_RULES = FeatureRule(
    body=(
        "SQL date/timestamp literals → Cypher temporal values: Neo4j stores dates "
        "and timestamps as typed temporal values, NOT strings. Comparing a "
        "temporal property against a bare quoted string (`po.creationDate >= "
        "'2010-06-01'`) compares a temporal against a STRING and does not behave "
        "as a date range — wrap the literal in the matching constructor:\n"
        "- a DATE literal (`'YYYY-MM-DD'`) → `date('YYYY-MM-DD')`, e.g. "
        "`WHERE li.shipdate >= date('1995-03-01') AND li.shipdate <= "
        "date('1995-03-31')`.\n"
        "- a TIMESTAMP literal → `datetime('YYYY-MM-DDThh:mm:ss')` using the "
        "ISO-8601 `T` separator (a space is not accepted): a SQL `'2010-06-01'` "
        "compared against a timestamp property becomes "
        "`datetime('2010-06-01T00:00:00')`, e.g. `WHERE po.creationDate >= "
        "datetime('2010-06-01T00:00:00') AND po.creationDate < "
        "datetime('2010-07-01T00:00:00')`.\n"
        "Pick the constructor that matches the property's type in the schema: "
        "`date(...)` for a date-only column, `datetime(...)` for a timestamp/"
        "creation-time column. Other constructors: `localdatetime(...)` "
        "(timezone-less timestamp), `localtime(...)` / `time(...)` (time of day), "
        "`duration(...)` (an interval). Keep the comparison operators (`>=`, `<`, "
        "`<=`) unchanged; only the literal is wrapped."
    )
)

_FEATURE_RULES: dict[SqlFeature, FeatureRule] = {
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
    SqlFeature.TEMPORAL: _TEMPORAL_RULES,
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
        order (see :func:`~rows2graph.targets._schema.compose_section`).
        """
        return compose_section(_BASE_RULES, _FEATURE_RULES, features)

    def extract_query(self, llm_response: str) -> str:
        """Pull a Cypher query out of (possibly noisy) LLM output.

        Resolution order: (1) any fenced code block; (2) the first line that
        starts with a Cypher keyword; (3) the whole response, stripped.
        """
        return extract_query(_START_RE, llm_response)
