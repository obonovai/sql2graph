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

The prompt section is assembled from a fixed base block and a dictionary
of per-:class:`~rows2graph.sql_features.SqlFeature` rule chunks; only the
chunks matching features detected in the input SQL are appended. This
keeps the prompt focused (small models get distracted by irrelevant
rules) and matches the composition strategy used by
:class:`~rows2graph.targets.cypher.CypherTarget` and
:class:`~rows2graph.targets.aql.AqlTarget`.
"""

from __future__ import annotations

import re

from rows2graph.sql_features import SqlFeature

# Match a fenced code block tagged ``gremlin`` or ``groovy`` (case-insensitive)
# or untagged. The first captured group is the body of the fence.
_FENCE_RE = re.compile(
    r"```(?:gremlin|groovy|GREMLIN|GROOVY)?\s*\n(.*?)```",
    re.DOTALL,
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

# Always emitted — covers what reading a Gremlin traversal requires
# regardless of the source SQL shape.
_BASE_RULES = (
    "Generate ONE valid Gremlin-Groovy traversal against the TraversalSource "
    "`g`. The whole SQL statement becomes a SINGLE traversal chain that "
    "returns rows — never emit multiple `g.…` statements and never write one "
    "statement per selected column.\n"
    "- Read vertices with `g.V()`, edges with `g.E()`; writes use "
    "`g.addV('Label')` / `g.addE('TYPE')`.\n"
    "- Filter by label with `.hasLabel('Label')`; filter by a property with "
    "`.has('prop', value)` or `.has('Label', 'prop', value)`.\n"
    "- To filter on a key/identifier COLUMN (e.g. `WHERE id = 933`) use "
    "`.has('Label', 'id', 933)`, NOT `g.V(933)`. `g.V(x)` matches the graph's "
    "internal element id, which is unrelated to your data's id/key columns.\n"
    "- READ a property value with `.values('prop')`. To return several columns "
    "as one row use `.project('colA', 'colB').by(<A>).by(<B>)`: each `.by(...)` "
    "lines up positionally with one `.project(...)` key. `.by('prop')` reads "
    "that property, `.by(__.id())` returns the element id, and "
    "`.by(__.out('TYPE').values('prop'))` pulls a value across an edge. "
    "`.valueMap('a', 'b')` / `.elementMap('a', 'b')` return a map when named "
    "columns are not needed.\n"
    "- A SQL column alias (`col AS name`) becomes the `.project('name')` key.\n"
    "- Traverse edges with `.out('TYPE')` / `.in('TYPE')` / `.both('TYPE')`, "
    "using the direction declared in the schema.\n"
    "- Inside `.by(...)`, `.where(...)`, `.project(...).by(...)` and similar, a "
    "nested traversal starts with `__.` and EVERY step is a method call with "
    "parentheses: `__.values('x')`, `__.id()`, `__.out('TYPE')`, `__.count()`. "
    "Never write a bare field like `__.id` or Groovy-style access like "
    "`__.properties['x'].value`.\n"
    "- Start the query with one of: `g.V(...)`, `g.E(...)`, `g.addV(...)`, "
    "`g.addE(...)`, `g.with(...)`.\n"
    "\n"
    "These are NOT valid Gremlin — never use them:\n"
    "- `.property('x')`, `.propertyMap().get('x')`, `.valueMap().get('x')`, or "
    "`.get('x')` to READ a value: `.property(...)` WRITES a property and "
    "`.get(...)` is not a step. Read with `.values('x')` / `.valueMap('x')` / "
    "`.project(...).by(...)`.\n"
    "- `.with('name', expr)` to name an output column — `.with(...)` only "
    "configures step options. Use `.project(...).by(...)`.\n"
    "- terminal steps `.next()` / `.toList()` / `.value()` in the MIDDLE of a "
    "chain, and `+` string concatenation inside the traversal.\n"
    "\n"
    "Examples:\n"
    "- `SELECT id, first_name FROM person WHERE id = 933` →\n"
    "  `g.V().has('Person', 'id', 933).project('id', 'first_name')"
    ".by('id').by('firstName')`\n"
    "- columns from two joined tables in one row "
    "(`SELECT p.first_name, c.name FROM person p JOIN city c ON …`) →\n"
    "  `g.V().hasLabel('Person').as('p').out('IS_LOCATED_IN').as('c')"
    ".select('p', 'c').by('firstName').by('name')`\n"
    "\n"
    "Output EXACTLY ONE traversal and NOTHING else — no prose or explanation, "
    "no alternative versions, no `// or` comments, and no text before or after "
    "the traversal. If more than one encoding works, choose the single best one "
    "and emit only that."
)

_LIKE_RULES = (
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

_JOIN_RULES = (
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

_AGGREGATION_RULES = (
    "SQL aggregations → Gremlin reducers: use `.count()`, `.sum()`, "
    "`.mean()`, `.min()`, `.max()`, `.fold()` for collect-style "
    "aggregation. For grouped aggregates, use `.group().by(<key>)."
    "by(<reducer>)` — e.g. `.group().by('label').by(__.count())`. "
    "Gremlin has no `GROUP BY` clause — grouping is expressed by the "
    "first `by(...)` of `.group()`. To express SQL `HAVING`, attach a "
    "`.unfold().filter { ... }` after the `.group()`, or use "
    "`.where(__.values(<key>).is(P.gt(threshold)))` patterns."
)

_ORDER_LIMIT_RULES = (
    "Sorting and paging: use `.order().by('col', asc)` or `.order()."
    "by('col', desc)` (import `org.apache.tinkerpop.gremlin.process."
    "traversal.Order` is implicit in the server REPL — just write "
    "`asc`/`desc`). Paging: `.limit(n)` for a row cap; `.range(start, "
    "end)` for OFFSET+LIMIT (`start` is zero-based and exclusive of "
    "`end`). The typical order of steps is `.order().by(...).range(...)"
    ".valueMap(...)`."
)

_CTE_RULES = (
    "SQL CTEs (`WITH name AS (...)`) → Gremlin: name intermediate "
    "results with `.as('x')` and re-select them downstream with "
    "`.select('x')`. Anonymous sub-traversals use `__.X` (e.g. "
    "`.where(__.out('KNOWS').has('city', 'Paris'))`). Gremlin has no "
    "named CTE block — inline the correlated logic, or factor it into "
    "a named traversal step and reuse with `.select`."
)

_UNION_RULES = (
    "Set operations: `.union(__.A, __.B)` runs both anonymous "
    "traversals from the same incoming traverser and emits the "
    "concatenated output. For SQL `UNION` (de-duplicating), follow "
    "with `.dedup()`; for `UNION ALL` omit it. Gremlin has no native "
    "`INTERSECT`/`EXCEPT` step; emulate `INTERSECT` with `.where("
    "__.B)` and `EXCEPT` with `.not(__.B)`."
)

_WINDOW_RULES = (
    "SQL window functions (`OVER (PARTITION BY ... ORDER BY ...)`) "
    "have no direct Gremlin equivalent. Emulate by grouping into "
    "ordered folds — e.g. `.group().by(<partition>).by(__.order()."
    "by(<sort>).fold())` — then `.unfold()` the grouped lists. For "
    "row-number / rank, use the `.sack()` step seeded to 0 and "
    "incremented per traverser, or process the folded list with a "
    "Groovy closure `.map { ... }` when running against Gremlin "
    "Server."
)

_CASE_RULES = (
    "Conditional expressions: Gremlin has no `CASE WHEN`; use the "
    "`.choose(predicate, trueBranch, falseBranch)` step. The "
    "predicate is an anonymous traversal (e.g. `__.has('col', P.gt(0))"
    "`). Multi-branch CASE: nest `choose` calls. For simple value "
    "mapping, prefer `.choose(__.values('col')).option('a', __."
    "constant('X')).option('b', __.constant('Y'))`."
)

_SUBQUERY_RULES = (
    "SQL subqueries → anonymous traversals (`__.X`). Common patterns:\n"
    "- `EXISTS (SELECT ... FROM ... WHERE ...)` → `.where(__.X)`\n"
    "- `NOT EXISTS (...)`                       → `.not(__.X)`\n"
    "- `col IN (SELECT ...)`                    → `.where(__.values("
    "'col').is(P.within(<values>)))` for a static list, or `.where("
    "__.X.where(P.eq('col')))` for a correlated subquery.\n"
    "- Scalar subqueries used in projections: `.project('a', 'b')."
    "by('a').by(__.X.fold())` then `.fold()`."
)

_DISTINCT_RULES = (
    "`SELECT DISTINCT` → append `.dedup()` after the projection. "
    "`COUNT(DISTINCT x)` → `.values('x').dedup().count()` (counts distinct "
    "values of `x`). Use `.dedup().by('x')` only when you need the "
    "deduplicated elements themselves rather than a count."
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
        order — this gives a stable, readable layout where related rules
        (e.g. LIKE then JOIN then AGGREGATION) appear in the same sequence
        across translations.
        """
        chunks = [
            _BASE_RULES,
            *(chunk for feat in SqlFeature if feat in features and (chunk := _FEATURE_RULES.get(feat)) is not None),
        ]
        return "\n\n".join(chunks)

    def extract_query(self, llm_response: str) -> str:
        """Pull a Gremlin traversal out of (possibly noisy) LLM output.

        Resolution order: (1) any fenced code block; (2) the first line
        that starts with a Gremlin entry-point token (``g.V``, ``g.E``,
        ``g.addV``, ``g.addE``, ``g.with``, ``__.``); (3) the whole
        response, stripped.
        """
        match = _FENCE_RE.search(llm_response)
        if match:
            return match.group(1).strip()

        match = _START_RE.search(llm_response)
        if match:
            return llm_response[match.start() :].strip()

        return llm_response.strip()
