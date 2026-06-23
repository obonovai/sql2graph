"""Gremlin target language (Apache TinkerPop).

Provides :class:`GremlinTarget`, which contributes the Gremlin-specific
section of the system prompt and extracts a Gremlin-Groovy traversal from
a (possibly noisy) LLM response. The extractor accepts code-fenced
(```` ```gremlin ... ``` ```` or ```` ```groovy ... ``` ````) and
keyword-led (``g.V()...``) responses, in that order of preference.

Gremlin is a *traversal* language rather than a *declarative* language —
queries are method chains rooted at a ``TraversalSource`` (conventionally
``g``). The framework targets the Gremlin-Groovy script form because that
is what the Gremlin Server REPL and the Python driver's
``Client.submit(script)`` consume, and what most public documentation and
training data show. The same script runs against any TinkerPop-compatible
backend (TinkerGraph, JanusGraph, Amazon Neptune, Azure Cosmos DB
Gremlin API), so the prompt is intentionally backend-agnostic.

The prompt section is built from the shared schema in
:mod:`rows2graph.targets._schema`: a :class:`~rows2graph.targets._schema.BaseRules`
block (always emitted) plus per-:class:`~rows2graph.sql_features.SqlFeature`
:class:`~rows2graph.targets._schema.FeatureRule` chunks appended only for
features detected in the input SQL. The five-section base skeleton and the
shared worked-example inputs are the same ones Cypher and AQL use.
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

# Match the first Gremlin-starting token at the start of any line. ``g.V``,
# ``g.E``, ``g.addV``, ``g.addE``, ``g.with`` cover all legal entry points
# against a TraversalSource; ``__.`` covers anonymous traversals that a
# model might occasionally emit as a top-level (technically invalid but
# worth keeping the slice working so the syntax validator can flag it).
_START_RE = re.compile(
    r"^(g\.(V|E|addV|addE|with)\b|__\.)",
    re.IGNORECASE | re.MULTILINE,
)

# Always emitted. Covers the Gremlin data model, core read/projection/traversal
# syntax, an explicit "these are NOT valid Gremlin" anti-pattern block, and
# worked SQL->Gremlin examples — the same five-section structure every target's
# base block uses. NOTE: the substrings "TextP.containing", ".dedup()",
# "Walk the path ONCE", and "epoch" are deliberately kept OUT of this always-on
# block — they gate the LIKE, DISTINCT, JOIN, and TEMPORAL feature chunks, and
# leaking them here would defeat the focused-prompt tests.
_BASE_RULES = BaseRules(
    language="Gremlin",
    output_mandate=(
        "Generate ONE valid Gremlin-Groovy traversal against the TraversalSource "
        "`g`. The whole SQL statement becomes a SINGLE traversal chain that "
        "returns rows — never emit multiple `g.…` statements and never write one "
        "statement per selected column. Output EXACTLY ONE traversal and NOTHING "
        "else — no prose or explanation, no alternative versions, no `// or` "
        "comments, and no text before or after the traversal. If more than one "
        "encoding works, choose the single best one and emit only that."
    ),
    data_model=[
        "- Each NODE label is a vertex (e.g. `Customer`, `Order`). Read it with `g.V().hasLabel('Customer')`.",
        "- Each EDGE type is a DIRECTED relationship. Traverse it with "
        "`.out('PLACED')` in the schema's direction, or `.in('PLACED')` against it.",
        "- A junction / link table is an EDGE, not a vertex — realize it as a "
        "traversal step (`.out('SUPPLIES')`), do not look for a vertex with its name.",
        "- Use the graph PROPERTY names from the schema (e.g. `firstName`), NOT the "
        "original SQL column names (e.g. `first_name`).",
        "- Foreign-key columns are NOT stored as properties. A join on a FK is the "
        "edge itself — never `.has(...)` on a `*key`/`*_id` column.",
    ],
    core_syntax=[
        "- Read vertices with `g.V()`, edges with `g.E()`; writes use `g.addV('Label')` / `g.addE('TYPE')`.",
        "- Filter by label with `.hasLabel('Label')`; filter by a property with "
        "`.has('prop', value)` or `.has('Label', 'prop', value)`.",
        "- To filter on a key/identifier COLUMN (e.g. `WHERE id = 933`) use "
        "`.has('Label', 'id', 933)`, NOT `g.V(933)`. `g.V(x)` matches the graph's "
        "internal element id, which is unrelated to your data's id/key columns.",
        "- READ a property value with `.values('prop')`. To return several columns "
        "as one row use `.project('colA', 'colB').by(<A>).by(<B>)`: each `.by(...)` "
        "lines up positionally with one `.project(...)` key. `.by('prop')` reads "
        "that property, `.by(__.id())` returns the element id, and "
        "`.by(__.out('TYPE').values('prop'))` pulls a value across an edge. "
        "`.valueMap('a', 'b')` / `.elementMap('a', 'b')` return a map when named "
        "columns are not needed.",
        "- A SQL column alias (`col AS name`) becomes the `.project('name')` key.",
        "- Traverse edges with `.out('TYPE')` / `.in('TYPE')` / `.both('TYPE')`, "
        "using the direction declared in the schema.",
        "- Inside `.by(...)`, `.where(...)`, `.project(...).by(...)` and similar, a "
        "nested traversal starts with `__.` and EVERY step is a method call with "
        "parentheses: `__.values('x')`, `__.id()`, `__.out('TYPE')`, `__.count()`. "
        "Never write a bare field like `__.id` or Groovy-style access like "
        "`__.properties['x'].value`.",
        "- Start the query with one of: `g.V(...)`, `g.E(...)`, `g.addV(...)`, `g.addE(...)`, `g.with(...)`.",
    ],
    anti_patterns=[
        AntiPattern(
            bad="`.property('x')`, `.propertyMap().get('x')`, `.valueMap().get('x')`, or "
            "`.get('x')` to READ a value — `.property(...)` WRITES a property and "
            "`.get(...)` is not a step",
            good="read with `.values('x')` / `.valueMap('x')` / `.project(...).by(...)`",
        ),
        AntiPattern(
            bad="`.with('name', expr)` to name an output column — `.with(...)` only configures step options",
            good="use `.project(...).by(...)`",
        ),
        AntiPattern(
            bad="terminal steps `.next()` / `.toList()` / `.value()` in the MIDDLE of a "
            "chain, and `+` string concatenation inside the traversal",
        ),
    ],
    examples=[
        Example(
            sql=EX_POINT_LOOKUP_SQL,
            query="g.V().has('Person', 'id', 933).project('id', 'first_name').by('id').by('firstName')",
            label="point lookup",
        ),
        Example(
            sql=EX_JOIN_FILTER_SQL,
            query="g.V().hasLabel('Supplier').has('acctbal', P.gt(5000)).as('s')"
            ".out('LOCATED_IN').as('n').select('s', 'n').by('name').by('name')",
            label="single join + filter",
        ),
    ],
)

_LIKE_RULES = FeatureRule(
    body=(
        "SQL string-pattern predicates → Gremlin: use the `TextP` predicates "
        "rather than building regexes. SQL LIKE/ILIKE patterns use `%` (any "
        "sequence) and `_` (any single char) as wildcards; translate as:\n"
        "- `col LIKE '%x%'`           → `.has('col', TextP.containing('x'))`\n"
        "- `col LIKE 'x%'`            → `.has('col', TextP.startingWith('x'))`\n"
        "- `col LIKE '%x'`            → `.has('col', TextP.endingWith('x'))`\n"
        "- `col LIKE 'x'` (no wildcards) → `.has('col', 'x')`\n"
        "- `col NOT LIKE '%x%'`       → `.has('col', TextP.notContaining('x'))`\n"
        "- ILIKE: TinkerPop has no case-insensitive `TextP`; lowercase both "
        "sides in a `.filter { it.get().value('col').toLowerCase().contains("
        "'x') }` closure when case-insensitivity matters.\n"
        "Only fall back to a Groovy closure (`.filter { ... }`) when the "
        "pattern needs regex features beyond contains/startingWith/endingWith."
    )
)

_JOIN_RULES = FeatureRule(
    body=(
        "SQL JOINs → traversal steps: realise each `JOIN` as `.out('TYPE')` / "
        "`.in('TYPE')` / `.both('TYPE')` along the schema-declared edge label and "
        "direction. Use `.optional(__.out('TYPE'))` for outer joins "
        "(LEFT/RIGHT/FULL) — the traversal is skipped instead of dropping the row "
        "when no edge matches. Do NOT translate JOIN ON predicates into `.has(...)` "
        "calls on foreign-key columns — the schema's edge label already encodes the "
        "join.\n"
        "When the SELECT returns columns from several joined tables, label each "
        "node as you traverse with `.as('t')`, then read them together at the end: "
        "`.select('t1', 't2').by('propA').by('propB')` (or "
        "`.project('a', 'b').by(__.select('t1').values('propA'))...`). Walk the "
        "path ONCE — do not restart from `g.V()` for each column."
    )
)

_AGGREGATION_RULES = FeatureRule(
    body=(
        "SQL aggregations → Gremlin reducers: use `.count()`, `.sum()`, "
        "`.mean()`, `.min()`, `.max()`, `.fold()` for collect-style "
        "aggregation. For grouped aggregates, use `.group().by(<key>)."
        "by(<reducer>)` — e.g. `.group().by('label').by(__.count())`. "
        "Gremlin has no `GROUP BY` clause — grouping is expressed by the "
        "first `by(...)` of `.group()`. To express SQL `HAVING`, attach a "
        "`.unfold().filter { ... }` after the `.group()`, or use "
        "`.where(__.values(<key>).is(P.gt(threshold)))` patterns."
    ),
    example=Example(
        sql=EX_GROUPED_COUNT_SQL,
        query="g.V().hasLabel('Part').group().by('brand').by(__.count())",
        label="grouped count",
    ),
)

_ORDER_LIMIT_RULES = FeatureRule(
    body=(
        "Sorting and paging: use `.order().by('col', asc)` or `.order()."
        "by('col', desc)` (import `org.apache.tinkerpop.gremlin.process."
        "traversal.Order` is implicit in the server REPL — just write "
        "`asc`/`desc`). Paging: `.limit(n)` for a row cap; `.range(start, "
        "end)` for OFFSET+LIMIT (`start` is zero-based and exclusive of "
        "`end`). The typical order of steps is `.order().by(...).range(...)"
        ".valueMap(...)`."
    ),
    example=Example(
        sql="SELECT name FROM supplier ORDER BY acctbal DESC LIMIT 10",
        query="g.V().hasLabel('Supplier').order().by('acctbal', desc).limit(10).values('name')",
        label="top-N",
    ),
)

_CTE_RULES = FeatureRule(
    body=(
        "SQL CTEs (`WITH name AS (...)`) → Gremlin: name intermediate "
        "results with `.as('x')` and re-select them downstream with "
        "`.select('x')`. Anonymous sub-traversals use `__.X` (e.g. "
        "`.where(__.out('KNOWS').has('city', 'Paris'))`). Gremlin has no "
        "named CTE block — inline the correlated logic, or factor it into "
        "a named traversal step and reuse with `.select`."
    )
)

_UNION_RULES = FeatureRule(
    body=(
        "Set operations: `.union(__.A, __.B)` runs both anonymous "
        "traversals from the same incoming traverser and emits the "
        "concatenated output. For SQL `UNION` (de-duplicating), follow "
        "with `.dedup()`; for `UNION ALL` omit it. Gremlin has no native "
        "`INTERSECT`/`EXCEPT` step; emulate `INTERSECT` with `.where("
        "__.B)` and `EXCEPT` with `.not(__.B)`."
    )
)

_WINDOW_RULES = FeatureRule(
    body=(
        "SQL window functions (`OVER (PARTITION BY ... ORDER BY ...)`) "
        "have no direct Gremlin equivalent. Emulate by grouping into "
        "ordered folds — e.g. `.group().by(<partition>).by(__.order()."
        "by(<sort>).fold())` — then `.unfold()` the grouped lists. For "
        "row-number / rank, use the `.sack()` step seeded to 0 and "
        "incremented per traverser, or process the folded list with a "
        "Groovy closure `.map { ... }` when running against Gremlin "
        "Server."
    )
)

_CASE_RULES = FeatureRule(
    body=(
        "Conditional expressions: Gremlin has no `CASE WHEN`; use the "
        "`.choose(predicate, trueBranch, falseBranch)` step. The "
        "predicate is an anonymous traversal (e.g. `__.has('col', P.gt(0))"
        "`). Multi-branch CASE: nest `choose` calls. For simple value "
        "mapping, prefer `.choose(__.values('col')).option('a', __."
        "constant('X')).option('b', __.constant('Y'))`."
    )
)

_SUBQUERY_RULES = FeatureRule(
    body=(
        "SQL subqueries → anonymous traversals (`__.X`). Common patterns:\n"
        "- `EXISTS (SELECT ... FROM ... WHERE ...)` → `.where(__.X)`\n"
        "- `NOT EXISTS (...)`                       → `.not(__.X)`\n"
        "- `col IN (SELECT ...)`                    → `.where(__.values("
        "'col').is(P.within(<values>)))` for a static list, or `.where("
        "__.X.where(P.eq('col')))` for a correlated subquery.\n"
        "- Scalar subqueries used in projections: `.project('a', 'b')."
        "by('a').by(__.X.fold())` then `.fold()`."
    )
)

_DISTINCT_RULES = FeatureRule(
    body=(
        "`SELECT DISTINCT` → append `.dedup()` after the projection. "
        "`COUNT(DISTINCT x)` → `.values('x').dedup().count()` (counts distinct "
        "values of `x`). Use `.dedup().by('x')` only when you need the "
        "deduplicated elements themselves rather than a count."
    )
)

_TEMPORAL_RULES = FeatureRule(
    body=(
        "SQL date/timestamp literals → Gremlin: TinkerPop has no date "
        "constructor (the Cypher `date(...)` / `datetime(...)` are not Gremlin). "
        "Compare against the value AS STORED on the property:\n"
        "- ISO-8601 string properties: compare with the string bound directly — "
        "`.has('shipdate', P.gte('1995-03-01')).has('shipdate', "
        "P.lt('1995-04-01'))`. Zero-padded `YYYY-MM-DD` strings order lexically, "
        "so a range works.\n"
        "- epoch-millis (Long) properties: convert the SQL literal to "
        "milliseconds since the Unix epoch and compare numerically — "
        "`.has('createdAt', P.gte(801964800000))`.\n"
        "Pick the form that matches the property's type in the schema; never "
        "compare an ISO string against an epoch-millis property or vice versa."
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


class GremlinTarget:
    """Gremlin (Apache TinkerPop) target language implementation.

    Implements :class:`rows2graph.targets.TargetLanguage` structurally.
    The same instance is used regardless of the concrete backend
    (TinkerGraph, JanusGraph, Neptune, Cosmos DB) — Gremlin-Groovy
    scripts written against `g` are portable across them.
    """

    @property
    def name(self) -> str:
        return "gremlin"

    def system_prompt_section(self, features: frozenset[SqlFeature]) -> str:
        """Gremlin-specific section appended to the system prompt.

        The base block is always emitted; the per-feature rule chunks are
        appended in :class:`~rows2graph.sql_features.SqlFeature` declaration
        order (see :func:`~rows2graph.targets._schema.compose_section`).
        """
        return compose_section(_BASE_RULES, _FEATURE_RULES, features)

    def extract_query(self, llm_response: str) -> str:
        """Pull a Gremlin traversal out of (possibly noisy) LLM output.

        Resolution order: (1) any fenced code block; (2) the first line
        that starts with a Gremlin entry-point token (``g.V``, ``g.E``,
        ``g.addV``, ``g.addE``, ``g.with``, ``__.``); (3) the whole
        response, stripped.
        """
        return extract_query(_START_RE, llm_response)

    def repair_hint(self, errors: list[str]) -> str | None:  # noqa: ARG002
        """No Gremlin-specific repair overrides yet — keep the default fix flow.

        Gremlin is a single method-chain, so the AQL clause-ordering trap does
        not arise; the generic fix instruction is appropriate.
        """
        return None
