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

Like the Cypher and Gremlin targets, the prompt section is a fixed
``_BASE_RULES`` block (always emitted) plus per-:class:`~rows2graph.sql_features.SqlFeature`
chunks appended only for features detected in the input SQL. The base block
deliberately mirrors :mod:`rows2graph.targets.gremlin`: concrete syntax, a
"these are NOT valid AQL" anti-pattern block with verbatim bad→good
rewrites, worked examples, and an output-format mandate — the structure that
keeps small local models (llama3.2, qwen2.5-coder:7b) on the rails rather
than emitting Cypher-flavoured guesses.
"""

from __future__ import annotations

import re

from rows2graph.sql_features import SqlFeature

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

# Always emitted. Covers the AQL data model, the single traversal form this
# project uses (bare edge collections, no GRAPH), an explicit anti-pattern
# block that names the exact mistakes small models make, and three worked
# SQL→AQL examples against the schema. The example queries were verified
# against the documented ArangoDB AQL grammar.
_BASE_RULES = (
    "Generate ONE valid AQL (ArangoDB Query Language) query for the schema "
    "above. Output ONLY the query — no prose, no explanation, no markdown "
    "fences, no alternative versions, and nothing before or after the "
    "query.\n"
    "\n"
    "Data model:\n"
    "- Each NODE label is a vertex collection (e.g. `Customer`, `Order`). "
    "Read its documents with `FOR x IN Customer`.\n"
    "- Each EDGE type is an edge collection (e.g. `PLACED`, `CONTAINS`). "
    "Edges connect documents; they are not fields on a document.\n"
    "- The schema above prints each edge as `[:PLACED]` for readability. "
    "That `[: ]` is Cypher notation, NOT AQL — in a query you write the "
    "edge collection name BARE (`PLACED`), never `[:PLACED]`.\n"
    "\n"
    "Traversal — this is how you JOIN:\n"
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
    "`FOR`, or `RETURN`.\n"
    "- Follow the edge DIRECTION from the schema: for `[:PLACED] from "
    "Customer to Order`, go OUTBOUND from a Customer to reach its Orders, or "
    "INBOUND from an Order to reach its Customer. Use ANY only when "
    "direction does not matter.\n"
    "- To read an EDGE property, also bind the edge variable: "
    "`FOR p, e IN OUTBOUND s SUPPLIES RETURN { part: p.name, cost: "
    "e.supplycost }`.\n"
    "- Express a chain of SQL JOINs as NESTED `FOR` loops, one per edge hop "
    "(see the examples). Do NOT build nested `FILTER x IN (FOR ...)` "
    "comparisons on key columns — the edge already encodes the join, and "
    "foreign-key columns are not stored on the documents.\n"
    "\n"
    "Predicates and output:\n"
    "- Use `FILTER` (never `WHERE`). Sort with `SORT expr ASC|DESC`. Page "
    "with `LIMIT n` or `LIMIT offset, n` (offset first, unlike SQL "
    "`OFFSET`).\n"
    "- `RETURN` produces output. To return several columns, RETURN an "
    "object: `RETURN { alias: expr, ... }`. A SQL alias (`col AS name`) "
    "becomes the object key.\n"
    "- Take a scalar from a subquery with `xs[0]` (null if empty); "
    "`FIRST(xs)` is equivalent. Test SQL `EXISTS` with `LENGTH(xs) > 0`.\n"
    "- Use the graph PROPERTY names from the schema, not the original SQL "
    "column names.\n"
    "- A translated SELECT almost always starts with `FOR`. Use a leading "
    "`LET` only to define a value used by a following `FOR`, and a bare "
    "leading `RETURN` only for a constant/scalar. Writes start with INSERT, "
    "UPDATE, REPLACE, REMOVE, or UPSERT.\n"
    "\n"
    "These are NOT valid AQL — never generate them:\n"
    "- `[:PLACED]` or `-[:REL]->` : Cypher edge syntax (AQL has no `[:`). "
    "Name the edge collection bare after the start vertex.\n"
    '    BAD:  FOR v, e, p IN OUTBOUND[:LOCATED_IN]("Nation") GRAPH '
    '"named_graph"\n'
    "    GOOD: FOR s IN Supplier FOR n IN OUTBOUND s LOCATED_IN RETURN "
    "{ supplier: s.name, nation: n.name }\n"
    '- `OUTBOUND "Customer"` or `OUTBOUND("Customer")` : a collection '
    "name (or a function call) as the start vertex. Start from a document "
    "variable bound by an enclosing `FOR`, or a document id string "
    '`"Customer/123"`.\n'
    '- `OUTBOUND c PLACED GRAPH "..."` : an edge collection AND `GRAPH` '
    'together is illegal. Likewise `OUTBOUND c PLACED "name"` : a '
    "trailing quoted string (a leftover graph name) after the edge "
    "collection is invalid.\n"
    '    BAD:  FOR o IN OUTBOUND c PLACED GRAPH "your_graph_name"\n'
    "    GOOD: FOR o IN OUTBOUND c PLACED RETURN o\n"
    "- `WHERE ...` : use `FILTER`.\n"
    "- `STARTS WITH` / `ENDS WITH` (Cypher, written with a space) : use the "
    "AQL `LIKE(text, pattern[, true])` function.\n"
    "- `CASE WHEN ... END` : use the ternary `cond ? a : b`.\n"
    "- `MATCH (a)-[:R]->(b)` : Cypher. Read with `FOR x IN Collection` and "
    "traverse with `FOR y IN OUTBOUND x EdgeColl`.\n"
    "\n"
    "Examples:\n"
    "- single join + filter (`SELECT s.name, n.name AS nation FROM supplier "
    "s JOIN nation n ON n.nationkey = s.nationkey WHERE s.acctbal > "
    "5000`):\n"
    "    FOR s IN Supplier\n"
    "      FILTER s.acctbal > 5000\n"
    "      FOR n IN OUTBOUND s LOCATED_IN\n"
    "        RETURN { name: s.name, nation: n.name }\n"
    "- multi-hop join — a chain of JOINs becomes nested `FOR` loops "
    "(`FROM customer c JOIN orders o ... JOIN lineitem li ... JOIN part "
    "p ...`):\n"
    "    FOR c IN Customer\n"
    "      FOR o IN OUTBOUND c PLACED\n"
    "        FOR li IN OUTBOUND o CONTAINS\n"
    "          FOR p IN OUTBOUND li OF_PART\n"
    "            RETURN { customer_name: c.name, orderkey: o.orderkey, "
    "quantity: li.quantity, part_name: p.name }\n"
    "- GROUP BY + HAVING via a correlated subquery and `FILTER` "
    "(`... COUNT(o.orderkey) AS order_count ... GROUP BY ... HAVING "
    "COUNT(o.orderkey) > 1`):\n"
    "    FOR c IN Customer\n"
    "      LET orders = (FOR o IN OUTBOUND c PLACED RETURN o)\n"
    "      FILTER LENGTH(orders) > 1\n"
    "      FOR n IN OUTBOUND c LOCATED_IN\n"
    "        RETURN { custkey: c.custkey, customer_name: c.name, "
    "mktsegment: c.mktsegment, nation_name: n.name, order_count: "
    "LENGTH(orders) }"
)

_LIKE_RULES = (
    "SQL LIKE patterns: use the `LIKE(text, pattern, caseInsensitive)` "
    'function — e.g. `FILTER LIKE(p.name, "%foo%")` for `name LIKE '
    "'%foo%'`. For `ILIKE`, pass `true` as the third argument: "
    '`LIKE(p.name, "%foo%", true)`. AQL keeps SQL\'s `%` and `_` '
    "wildcards. Do NOT use Cypher's `STARTS WITH` / `ENDS WITH` (written "
    "with a space) — they are not AQL."
)

_JOIN_RULES = (
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

_AGGREGATION_RULES = (
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
)

_ORDER_LIMIT_RULES = (
    "Sorting: `SORT expr ASC|DESC`. Paging: `LIMIT n` or "
    "`LIMIT offset, n` (offset comes first, unlike SQL's `OFFSET n`)."
)

_CTE_RULES = (
    "SQL CTEs (`WITH x AS (...)`) -> AQL `LET x = (FOR ... RETURN ...)` "
    "subquery assignments. Note: AQL's top-level `WITH` keyword declares "
    "collection bindings for transactions, NOT a CTE — use `LET` for the "
    "CTE pattern."
)

_UNION_RULES = (
    "Set operations: AQL has `UNION(arr1, arr2)` and `UNION_DISTINCT(arr1, "
    "arr2)` as array functions. Pattern: "
    "`FOR x IN UNION_DISTINCT((FOR a IN ... RETURN a), "
    "(FOR b IN ... RETURN b)) RETURN x`. For `INTERSECT`/`EXCEPT`, use "
    "`INTERSECTION(...)` and `MINUS(...)`."
)

_WINDOW_RULES = (
    "AQL has no native window functions. Emulate by collecting rows per "
    "partition, then iterating with an index: "
    "`COLLECT key = expr INTO group ... LET ranked = (FOR g IN group SORT "
    "g.x RETURN g) FOR i IN 0..LENGTH(ranked)-1 ...`. Use `ROW_NUMBER`-"
    "style logic via the loop index."
)

_CASE_RULES = (
    "Conditional expressions: AQL has no `CASE` keyword. Use the ternary "
    "operator: `cond ? a : b`. For multi-branch conditionals, chain "
    "ternaries: `cond1 ? a : (cond2 ? b : c)`."
)

_SUBQUERY_RULES = (
    "Subqueries in AQL are inline expressions wrapped in parentheses, "
    "always returning an array: `LET sub = (FOR x IN coll FILTER ... "
    "RETURN x)`. For scalar results, take the first element: `sub[0]`. "
    "For `EXISTS`, test `LENGTH(sub) > 0`. For `IN (subquery)`, the "
    "subquery array is the right-hand side of `FILTER x.col IN sub`."
)

_DISTINCT_RULES = (
    "`SELECT DISTINCT` -> `RETURN DISTINCT ...`. For `COUNT(DISTINCT x)`, "
    "use `COUNT_UNIQUE(x)` inside an `AGGREGATE` clause, or "
    "`COUNT_DISTINCT(x)` / `LENGTH(UNIQUE(x))` as a plain expression."
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
        order, giving a stable, readable layout across translations (the same
        composition strategy as the Cypher and Gremlin targets).
        """
        chunks = [_BASE_RULES, *(_FEATURE_RULES[feat] for feat in SqlFeature if feat in features)]
        return "\n\n".join(chunks)

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
