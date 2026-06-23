"""AQL target language (ArangoDB Query Language).

Provides :class:`AqlTarget`, which contributes the AQL-specific section of
the system prompt and extracts an AQL query from a (possibly noisy) LLM
response.

The framework adopts the convention that vertex-collection names equal node
labels and edge-collection names equal edge types from the user's schema
mapping. Crucially, this target uses the **edge-collection (anonymous)**
traversal form — ``FOR v IN OUTBOUND <startDoc> <EdgeCollection>`` — and
never the named-graph form. Edge collections always exist physically, so a
traversal needs no registered named graph, and dropping ``GRAPH "<name>"``
removes the failure modes small models fall into: combining ``GRAPH`` with a
bare edge collection (``unexpected GRAPH keyword``), leaving a dangling
quoted graph name, or hallucinating a graph name outright.

The prompt section is built from the shared schema in
:mod:`rows2graph.targets._schema`: a :class:`~rows2graph.targets._schema.BaseRules`
block (always emitted) plus per-:class:`~rows2graph.sql_features.SqlFeature`
:class:`~rows2graph.targets._schema.FeatureRule` chunks appended only for
features detected in the input SQL. The five-section base skeleton and the
shared worked-example inputs are the same ones Cypher and Gremlin use.
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

# Match the first AQL top-level keyword at the start of any line.
_START_RE = re.compile(
    r"^(FOR|LET|INSERT|UPDATE|REPLACE|REMOVE|UPSERT|WITH|RETURN)\b",
    re.IGNORECASE | re.MULTILINE,
)

# A clause-ordering error: ArangoDB rejects a SORT/LIMIT/FILTER/COLLECT/LET that
# follows the block-terminating RETURN. The parser blames the trailing clause
# ("unexpected SORT declaration") or reports it expected the query to end — both
# misdirect the model toward the named clause when the real fix is to move
# RETURN last. Either signal (the "unexpected <clause>" phrasing or the
# "expecting end of query string" tail) identifies the class.
_ORDERING_ERROR_RE = re.compile(
    r"unexpected\s+(SORT|LIMIT|FILTER|COLLECT|LET)\b|expecting end of query string",
    re.IGNORECASE,
)

# Targeted corrective injected into the fix prompt for the ordering class. It
# explicitly licenses the restructure the default fix instruction forbids.
_ORDERING_REPAIR_HINT = (
    "This is a clause-ORDERING error, not a problem with the clause the parser "
    "names. In AQL, `RETURN` ENDS the `FOR` block, so any `SORT`, `LIMIT`, "
    "`FILTER`, or `COLLECT` placed AFTER a `RETURN` is illegal. You MUST "
    "restructure the query (this overrides the usual advice to preserve the "
    "structure): move every `SORT`/`LIMIT`/`FILTER`/`COLLECT` to BEFORE the "
    "single trailing `RETURN`, and emit exactly ONE `RETURN` as the very last "
    "clause. Canonical order: `FOR ... FILTER ... SORT ... LIMIT ... RETURN ...`."
)

# Always emitted. Covers the AQL data model, the single traversal form this
# project uses (bare edge collections, no GRAPH), an explicit anti-pattern
# block that names the exact mistakes small models make, and worked SQL->AQL
# examples against the schema. The example queries were verified against the
# documented ArangoDB AQL grammar. NOTE: "COLLECT" and "DATE_TIMESTAMP" are
# deliberately kept OUT of this always-on block — they gate the AGGREGATION and
# TEMPORAL feature chunks, and leaking them here would defeat the focused-prompt
# tests.
_BASE_RULES = BaseRules(
    language="AQL",
    output_mandate=(
        "Generate ONE valid AQL (ArangoDB Query Language) query for the schema "
        "above. Output ONLY the query — no prose, no explanation, no markdown "
        "fences, no alternative versions, and nothing before or after the query."
    ),
    data_model=[
        "- Each NODE label is a vertex collection (e.g. `Customer`, `Order`). "
        "Read its documents with `FOR x IN Customer`.",
        "- Each EDGE type is an edge collection (e.g. `PLACED`, `CONTAINS`). "
        "Edges connect documents; they are not fields on a document.",
        "- The schema above prints each edge as `[:PLACED]` for readability. "
        "That `[: ]` is Cypher notation, NOT AQL — in a query you write the edge "
        "collection name BARE (`PLACED`), never `[:PLACED]`.",
    ],
    core_syntax=[
        "- A traversal starts FROM a document and names one or more bare edge "
        "collections (depth defaults to 1):\n"
        "    FOR v IN OUTBOUND startDoc EdgeCollection\n"
        "  * `startDoc` is a document variable bound by an enclosing `FOR` "
        "(e.g. `c` from `FOR c IN Customer`), or a document id string like "
        '`"Customer/123"`. It is NEVER a collection name and NEVER a quoted '
        "collection name.\n"
        "  * `EdgeCollection` is written bare: no brackets, no colons, no "
        'quotes — `PLACED`, never `[:PLACED]` and never `"PLACED"`.\n'
        "  * This project does NOT use named graphs: never emit the `GRAPH` "
        "keyword. A traversal ends at the edge collection — nothing (no "
        "`GRAPH`, no quoted string) may follow it except `FILTER`, a nested "
        "`FOR`, or `RETURN`.",
        "- Follow the edge DIRECTION from the schema: for `[:PLACED] from "
        "Customer to Order`, go OUTBOUND from a Customer to reach its Orders, or "
        "INBOUND from an Order to reach its Customer. Use ANY only when "
        "direction does not matter.",
        "- To read an EDGE property, also bind the edge variable: "
        "`FOR p, e IN OUTBOUND s SUPPLIES RETURN { part: p.name, cost: e.supplycost }`.",
        "- Express a chain of SQL JOINs as NESTED `FOR` loops, one per edge hop "
        "(see the examples). Do NOT build nested `FILTER x IN (FOR ...)` "
        "comparisons on key columns — the edge already encodes the join, and "
        "foreign-key columns are not stored on the documents.",
        "- Use `FILTER` (never `WHERE`). Sort with `SORT expr ASC|DESC`. Page "
        "with `LIMIT n` or `LIMIT offset, n` (offset first, unlike SQL `OFFSET`).",
        "- `RETURN` produces output. To return several columns, RETURN an "
        "object: `RETURN { alias: expr, ... }`. A SQL alias (`col AS name`) "
        "becomes the object key.",
        "- `RETURN` is the LAST clause of a `FOR` block — it ends the block, so "
        "`SORT` and `LIMIT` must come BEFORE `RETURN`, never after (the reverse "
        "of SQL/Cypher). Canonical order: `FOR ... FILTER ... SORT ... LIMIT ... "
        "RETURN ...`.",
        "- Take a scalar from a subquery with `xs[0]` (null if empty); "
        "`FIRST(xs)` is equivalent. Test SQL `EXISTS` with `LENGTH(xs) > 0`.",
        "- Use the graph PROPERTY names from the schema, not the original SQL column names.",
        "- A translated SELECT almost always starts with `FOR`. Use a leading "
        "`LET` only to define a value used by a following `FOR`, and a bare "
        "leading `RETURN` only for a constant/scalar. Writes start with INSERT, "
        "UPDATE, REPLACE, REMOVE, or UPSERT.",
    ],
    anti_patterns=[
        AntiPattern(
            bad="`[:PLACED]` or `-[:REL]->` — Cypher edge syntax (AQL has no `[:`)",
            good="name the edge collection bare after the start vertex",
            bad_example='FOR v, e, p IN OUTBOUND[:LOCATED_IN]("Nation") GRAPH "named_graph"',
            good_example="FOR s IN Supplier FOR n IN OUTBOUND s LOCATED_IN RETURN { supplier: s.name, nation: n.name }",
        ),
        AntiPattern(
            bad='`OUTBOUND "Customer"` or `OUTBOUND("Customer")` — a collection name '
            "(or a function call) as the start vertex",
            good='start from a document variable bound by an enclosing `FOR`, or a document id string `"Customer/123"`',
        ),
        AntiPattern(
            bad='`OUTBOUND c PLACED GRAPH "..."` — an edge collection AND `GRAPH` together '
            "is illegal (likewise a trailing quoted string after the edge collection)",
            good="end the traversal at the bare edge collection",
            bad_example='FOR o IN OUTBOUND c PLACED GRAPH "your_graph_name"',
            good_example="FOR o IN OUTBOUND c PLACED RETURN o",
        ),
        AntiPattern(bad="`WHERE ...`", good="use `FILTER`"),
        AntiPattern(
            bad="`STARTS WITH` / `ENDS WITH` (Cypher, written with a space)",
            good="use the AQL `LIKE(text, pattern[, true])` function",
        ),
        AntiPattern(bad="`CASE WHEN ... END`", good="use the ternary `cond ? a : b`"),
        AntiPattern(
            bad="`MATCH (a)-[:R]->(b)` — Cypher",
            good="read with `FOR x IN Collection` and traverse with `FOR y IN OUTBOUND x EdgeColl`",
        ),
        AntiPattern(
            bad="`RETURN { ... }` followed by `SORT` or `LIMIT` — `RETURN` ends the "
            "`FOR` block, so nothing may come after it",
            good="put `SORT` and `LIMIT` before `RETURN`",
            bad_example="RETURN { title: f.title, n: LENGTH(members) } SORT LENGTH(members) DESC LIMIT 10",
            good_example="SORT LENGTH(members) DESC LIMIT 10 RETURN { title: f.title, n: LENGTH(members) }",
        ),
    ],
    examples=[
        Example(
            sql=EX_POINT_LOOKUP_SQL,
            query="FOR p IN Person\n  FILTER p.id == 933\n  RETURN { id: p.id, first_name: p.firstName }",
            label="point lookup",
        ),
        Example(
            sql=EX_JOIN_FILTER_SQL,
            query="FOR s IN Supplier\n  FILTER s.acctbal > 5000\n  FOR n IN OUTBOUND s LOCATED_IN\n    RETURN { name: s.name, nation: n.name }",
            label="single join + filter",
        ),
    ],
)

_LIKE_RULES = FeatureRule(
    body=(
        "SQL LIKE patterns: use the `LIKE(text, pattern, caseInsensitive)` "
        'function — e.g. `FILTER LIKE(p.name, "%foo%")` for `name LIKE '
        "'%foo%'`. For `ILIKE`, pass `true` as the third argument: "
        '`LIKE(p.name, "%foo%", true)`. AQL keeps SQL\'s `%` and `_` '
        "wildcards. Do NOT use Cypher's `STARTS WITH` / `ENDS WITH` (written "
        "with a space) — they are not AQL."
    )
)

_JOIN_RULES = FeatureRule(
    body=(
        "SQL JOIN -> a nested `FOR` traversal, one `FOR` per hop, following the "
        "schema's edge direction:\n"
        "    FOR c IN Customer\n"
        "      FOR o IN OUTBOUND c PLACED\n"
        "        RETURN { ... }\n"
        "For LEFT/OUTER joins, collect the optional side and keep the row when "
        "it is empty:\n"
        "    FOR c IN Customer\n"
        "      LET orders = (FOR o IN OUTBOUND c PLACED RETURN o)\n"
        "      FOR o IN (LENGTH(orders) > 0 ? orders : [null])\n"
        "        RETURN { name: c.name, orderkey: o.orderkey }\n"
        "(`o.orderkey` is null when the customer has no orders; reading a field "
        "off the null placeholder is safe, but do NOT start a further traversal "
        "FROM that null `o`.) Do NOT translate `JOIN ... ON` key equality into a "
        "`FILTER` on foreign-key columns — the edge encodes the join and FK "
        "columns are not stored on the documents."
    )
)

_AGGREGATION_RULES = FeatureRule(
    body=(
        "Aggregations come in two shapes.\n"
        "1) Aggregate the related items OF EACH parent (the common case, e.g. "
        "count/sum of orders per customer): use a correlated subquery, with a "
        "`FILTER` for `HAVING`:\n"
        "    FOR c IN Customer\n"
        "      LET orders = (FOR o IN OUTBOUND c PLACED RETURN o)\n"
        "      FILTER LENGTH(orders) > 1\n"
        "      RETURN { custkey: c.custkey, order_count: LENGTH(orders), "
        "total: SUM(orders[*].totalprice) }\n"
        "   `LENGTH(xs)` is `COUNT(*)` over the related rows. When the subquery "
        "returns whole documents (`RETURN o`), aggregate a field with "
        "`SUM(xs[*].field)` / `AVERAGE(xs[*].field)` / `MIN(...)` / `MAX(...)`; "
        "when it already projects the number (`RETURN o.totalprice`), use "
        "`SUM(xs)`.\n"
        "2) Global GROUP BY across a whole collection: use `COLLECT`, which "
        "always needs a following `RETURN`:\n"
        "   - grouped count: `FOR o IN Order COLLECT status = o.orderstatus "
        "WITH COUNT INTO n RETURN { status, n }`\n"
        "   - grouped sum/avg: `FOR o IN Order COLLECT status = o.orderstatus "
        "AGGREGATE total = SUM(o.totalprice), avg = AVERAGE(o.totalprice) "
        "RETURN { status, total, avg }`\n"
        "   - plain total count: `FOR x IN Coll COLLECT WITH COUNT INTO n "
        "RETURN n`\n"
        "   AGGREGATE functions: SUM, AVERAGE, MIN, MAX, LENGTH, COUNT_UNIQUE. "
        "For SQL `HAVING` on a grouped query, add a `FILTER` after the "
        "`COLLECT`."
    ),
    example=Example(
        sql=EX_GROUPED_COUNT_SQL,
        query="FOR p IN Part\n  COLLECT brand = p.brand WITH COUNT INTO c\n  RETURN { brand, c }",
        label="grouped count",
    ),
)

_ORDER_LIMIT_RULES = FeatureRule(
    body=(
        "Sorting: `SORT expr ASC|DESC`. Paging: `LIMIT n` or "
        "`LIMIT offset, n` (offset comes first, unlike SQL's `OFFSET n`). "
        "Place `SORT` and `LIMIT` BEFORE `RETURN` — `RETURN` terminates the "
        "`FOR` block, so a trailing `SORT`/`LIMIT` is a syntax error. Sort by the "
        "underlying expression, NOT a `RETURN` projection key: use `SORT "
        "LENGTH(members) DESC`, not `SORT member_count DESC` referring to the "
        "RETURN alias."
    ),
    example=Example(
        sql="SELECT name FROM supplier ORDER BY acctbal DESC LIMIT 10",
        query="FOR s IN Supplier\n  SORT s.acctbal DESC\n  LIMIT 10\n  RETURN { name: s.name }",
        label="top-N",
    ),
)

_CTE_RULES = FeatureRule(
    body=(
        "SQL CTEs (`WITH x AS (...)`) -> AQL `LET x = (FOR ... RETURN ...)` "
        "subquery assignments. Note: AQL's top-level `WITH` keyword declares "
        "collection bindings for transactions, NOT a CTE — use `LET` for the "
        "CTE pattern."
    )
)

_UNION_RULES = FeatureRule(
    body=(
        "Set operations: AQL has `UNION(arr1, arr2)` and `UNION_DISTINCT(arr1, "
        "arr2)` as array functions. `UNION_DISTINCT` is a FUNCTION, NOT an infix "
        "keyword — never place it BETWEEN two `FOR ... RETURN` blocks the way "
        "SQL's `UNION` joins two SELECTs. Bind each side with `LET` and combine:\n"
        "    LET a = (FOR ... RETURN ...)\n"
        "    LET b = (FOR ... RETURN ...)\n"
        "    RETURN UNION_DISTINCT(a, b)\n"
        "(the inline `FOR x IN UNION_DISTINCT((FOR a IN ... RETURN a), (FOR b IN "
        "... RETURN b)) RETURN x` form is equivalent.) `UNION_DISTINCT` already "
        "removes duplicates — do NOT add `DISTINCT` (AQL also forbids `RETURN "
        "DISTINCT` on a top-level function result); use plain `UNION(a, b)` for "
        "`UNION ALL`. For `INTERSECT`/`EXCEPT`, use `INTERSECTION(...)` and "
        "`MINUS(...)`."
    )
)

_WINDOW_RULES = FeatureRule(
    body=(
        "AQL has no native window functions. Emulate by collecting rows per "
        "partition, then iterating with an index: "
        "`COLLECT key = expr INTO group ... LET ranked = (FOR g IN group SORT "
        "g.x RETURN g) FOR i IN 0..LENGTH(ranked)-1 ...`. Use `ROW_NUMBER`-"
        "style logic via the loop index."
    )
)

_CASE_RULES = FeatureRule(
    body=(
        "Conditional expressions: AQL has no `CASE` keyword. Use the ternary "
        "operator: `cond ? a : b`. For multi-branch conditionals, chain "
        "ternaries: `cond1 ? a : (cond2 ? b : c)`."
    )
)

_SUBQUERY_RULES = FeatureRule(
    body=(
        "Subqueries in AQL are inline expressions wrapped in parentheses, "
        "always returning an array: `LET sub = (FOR x IN coll FILTER ... "
        "RETURN x)`. For scalar results, take the first element: `sub[0]`. "
        "For `EXISTS`, test `LENGTH(sub) > 0`. For `IN (subquery)`, the "
        "subquery array is the right-hand side of `FILTER x.col IN sub`."
    )
)

_DISTINCT_RULES = FeatureRule(
    body=(
        "`SELECT DISTINCT` -> `RETURN DISTINCT ...`. For `COUNT(DISTINCT x)`, "
        "use `COUNT_UNIQUE(x)` inside an `AGGREGATE` clause, or "
        "`COUNT_DISTINCT(x)` / `LENGTH(UNIQUE(x))` as a plain expression."
    )
)

_TEMPORAL_RULES = FeatureRule(
    body=(
        "SQL date/timestamp literals → AQL: ArangoDB has no native date type. "
        "A date/timestamp is stored either as an ISO-8601 string or as a numeric "
        "epoch-millis value — check the schema for which.\n"
        "- ISO-8601 strings sort lexically, so compare them DIRECTLY: a SQL "
        "`shipdate >= '1995-03-01'` range becomes `FILTER doc.shipdate >= "
        "'1995-03-01' AND doc.shipdate < '1995-04-01'`. Keep the operators "
        "unchanged; only zero-padded `YYYY-MM-DD` (and `YYYY-MM-DDThh:mm:ss`) "
        "literals sort correctly.\n"
        "- Do NOT wrap literals in Cypher-style `date(...)` / `datetime(...)` — "
        "those constructors are not AQL.\n"
        "- For date arithmetic, or when the stored values are numeric "
        "timestamps, use the AQL date functions: `DATE_TIMESTAMP(x)` (to epoch "
        "millis), `DATE_DIFF`, `DATE_ADD`, `DATE_FORMAT` — e.g. "
        "`FILTER DATE_DIFF(doc.from, doc.to, 'd') > 30`."
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


class AqlTarget:
    """AQL (ArangoDB) target language implementation.

    Implements :class:`rows2graph.targets.TargetLanguage` structurally —
    there is no abstract base class to inherit from. The target is stateless:
    AQL traversals use bare edge collections (the anonymous-graph form), so no
    named-graph name needs to be threaded into the prompt.
    """

    @property
    def name(self) -> str:
        return "aql"

    def system_prompt_section(self, features: frozenset[SqlFeature]) -> str:
        """AQL-specific section appended to the system prompt.

        The base block is always emitted; the per-feature rule chunks are
        appended in :class:`~rows2graph.sql_features.SqlFeature` declaration
        order (see :func:`~rows2graph.targets._schema.compose_section`).
        """
        return compose_section(_BASE_RULES, _FEATURE_RULES, features)

    def extract_query(self, llm_response: str) -> str:
        """Pull an AQL query out of (possibly noisy) LLM output.

        Resolution order: (1) any fenced code block; (2) the first line that
        starts with an AQL keyword; (3) the whole response, stripped.
        """
        return extract_query(_START_RE, llm_response)

    def repair_hint(self, errors: list[str]) -> str | None:
        """Return the clause-ordering corrective when the errors warrant it.

        This is the single most common AQL failure for SQL-trained models: they
        map SQL's trailing `ORDER BY`/`LIMIT` to a trailing `SORT`/`LIMIT`,
        which AQL forbids after the terminal `RETURN`. The validator's message
        names the trailing clause, so a literal "fix only that" retry never
        moves the `RETURN`. See :data:`_ORDERING_REPAIR_HINT`.
        """
        if any(_ORDERING_ERROR_RE.search(err) for err in errors):
            return _ORDERING_REPAIR_HINT
        return None
