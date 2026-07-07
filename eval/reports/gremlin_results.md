# SQL -> Gremlin evaluation: results and analysis

Run of 2026-07-07. 15 LDBC gold queries x 5 models (llama3.2:latest, qwen3-coder:30b,
gemma4:26b via Ollama; claude-opus-4-8 and claude-opus-4-8-thinking, the same Opus model
with extended thinking on, via the Anthropic API), serial model-by-model, temperature 0,
max 3 generate-validate-fix iterations with offline ANTLR (TinkerPop grammar) validation.
The base four-model run is from 2026-07-03; the claude-opus-4-8-thinking variant was added
2026-07-06 and all metrics were recomputed 2026-07-07. Throughout, a bare "opus" means the
non-thinking claude-opus-4-8 and "opus-thinking" the extended-thinking variant. Execution accuracy measured against the Postgres oracle on
graphonauts's in-memory TinkerGraph (Gremlin Server 3.8.1, LDBC SNB SF1, unified
SCREAMING_SNAKE edge labels, ISO-8601 string dates). The gold Gremlin set itself was
validated first: all 15 gold queries return exactly the same rows as their gold SQL
(`scripts/validate_gold.py --target gremlin`, 15/15).

## Headline

| model                    | validity | pass@1 | component F1 | exec accuracy | result F1 |
|--------------------------|----------|--------|--------------|---------------|-----------|
| claude-opus-4-8-thinking | 1.00     | 1.00   | 0.91         | **0.87**      | 0.89      |
| gemma4:26b               | 1.00     | 0.93   | 0.91         | 0.67          | 0.77      |
| claude-opus-4-8          | 1.00     | 1.00   | 0.91         | 0.53          | 0.56      |
| qwen3-coder:30b          | 0.73     | 0.67   | 0.83         | 0.20          | 0.22      |
| llama3.2:latest          | 0.20     | 0.20   | 0.65         | 0.00          | 0.00      |

For contrast, on the same 15 queries the Cypher target reached execution accuracy 1.00 for
opus, opus-thinking and gemma alike (qwen 0.53, llama 0.20), and AQL reached 0.93 for the
same three (qwen 0.47, llama 0.00); on both of those targets extended thinking bought
nothing. Gremlin is the one target where it matters. Non-thinking opus falls to 0.53 and
gemma to 0.67, while the *same* opus model with extended thinking on climbs to 0.87 - the
only configuration that recovers most of the gap. Two effects stack here: the *same* models,
SQL and schema mapping collapse from 1.00 to ~0.6 when only the target *language* changes to
Gremlin, and then the *same* opus recovers 34 of those points (0.53 -> 0.87) when only its
*reasoning budget* changes. Gremlin is where both the language and the thinking decide the
outcome.

## Per-query execution accuracy

The `think` column is claude-opus-4-8-thinking (extended-thinking variant of the same model).

| query | opus | think | gemma | qwen | llama | note |
|-------|------|-------|-------|------|-------|------|
| q01 point lookup        | 1 | 1 | 1 | 1 | 0 | |
| q02 date range          | 0 | 0 | 0 | 0 | 0 | nobody survives the nullable column, thinking included |
| q03 filter by name      | 1 | 1 | 1 | 1 | 0 | |
| q04 top-10 by count     | 1 | 1 | 1 | 0 | 0 | |
| q05 tag usage           | 0 | 1 | 0 | 0 | 0 | only opus-thinking filters the unified edge |
| q06 friends + edge prop | 0 | 1 | 1 | 0 | 0 | thinking swaps duplicate `select` for `project` |
| q07 3-way join          | 1 | 1 | 1 | 0 | 0 | |
| q08 friends-of-friends  | 0 | 1 | 0 | 0 | 0 | hardest; thinking recovers 382 dropped rows |
| q09 reply chain         | 0 | 1 | 1 | 0 | 0 | |
| q10 LEFT JOIN count     | 1 | 1 | 1 | 0 | 0 | |
| q11 HAVING              | 1 | 1 | 1 | 0 | 0 | |
| q12 UNION               | 1 | 1 | 0 | 0 | 0 | gemma off by exactly 1 row |
| q13 NOT EXISTS          | 1 | 1 | 1 | 1 | 0 | |
| q14 group by country    | 0 | 0 | 1 | 0 | 0 | thinking still returns one grouped map |
| q15 recursive ancestors | 0 | 1 | 0 | 0 | 0 | only opus-thinking passes; opus `tree()` KeyError, qwen 2nd generation_hang |

## Anatomy of the failures (verified against the live graph)

Every opus and gemma failure was executed and inspected by hand; these are measured
causes, not guesses.

### 1. The tabular-result contract is where Gremlin translations die (q06, q08, q09, q14)

The eval compares rows positionally against the SQL SELECT order, which is exactly what
a downstream application consuming the translation would do. Opus repeatedly produced
*correct traversal logic* wrapped in the *wrong result shape*:

- **q06/q08: `select()` with duplicated labels.** `select('p2', 'p2', 'p2', 'k')` was
  meant to emit three properties of `p2` plus one of `k`. Gremlin returns a *map*, so
  the duplicate keys collapse and the row silently loses two columns. Every row
  mismatches; F1 = 0.00 despite the traversal visiting exactly the right elements. The
  idiomatic form is `project()` with distinct keys, which the same model used correctly
  elsewhere.
- **q09: nested projections.** `select('c', 'p').by(__.project(...)).by(__.project(...))`
  returns two columns of nested maps instead of five scalar columns. Again: right data,
  wrong shape.
- **q14: `group().by('name').by(count())` without `unfold()`.** Returns one giant map as
  a single row (1 row vs the oracle's 111). The aggregation itself was correct.

Cypher and AQL make this failure mode nearly impossible: `RETURN a.x, a.y` and
`RETURN {x: a.x}` map one-to-one onto a SELECT list. (AQL has exactly one instance of it:
the top-level `RETURN UNION_DISTINCT(a, b)` in q12, which returns a single array row instead
of unfolded rows and which every model, opus and gemma included, got wrong; see
`aql_results.md`.) Gremlin has at least four
result-shaping idioms (`project`, `select`, `valueMap`, `group`), each with different
row semantics, and only one of them matches a SQL result set without extra steps.

Extended thinking rewrites exactly these `select()`/nested shapes into `project()` and
recovers q06, q08 and q09; only q14's missing `unfold()` survives it (section 7).

### 2. Absent properties silently shorten rows (q02: every model failed identically, opus-thinking included)

The graph stores SQL NULLs as *absent* properties (the Neo4j-style convention).
`project(...).by('content')` on a post without content does not return null and does
not error: it **drops the key from the projected map**. In the June-2010 range 8,858 of
12,932 posts are image posts without content, so both opus and gemma returned the right
12,932 rows of which exactly the 4,074 content-bearing ones matched: F1 = 0.315, which
is precisely the measured value. The correct idiom (used by the gold query) is
`by(__.coalesce(__.values('content'), __.constant('')))`.

The deeper cause is a prompt-gating gap rather than pure model failure: the library's
NULL-handling rules (which teach exactly this coalesce idiom) are injected only when
the SQL contains NULL-related syntax. q02's SQL is a plain range scan, features `[]`,
so the models were never told the column is nullable - and nothing in the schema
mapping marks nullability either. This is the most actionable finding in the run:
either annotate nullable properties in the mapping, or always include the
absent-property rule for Gremlin, whose projection semantics (unlike Cypher's
`p.content -> null`) are unforgiving.

### 3. Unified edge labels demand endpoint discipline (q05: every base model failed; only opus-thinking applied the filter, section 7)

The graph uses one `HAS_TAG` label for Forum->Tag, Post->Tag and Comment->Tag edges,
mirroring the Neo4j convention. The SQL counts tag usage across posts and comments
only; the gold traversal therefore filters `__.in('HAS_TAG').hasLabel('Post',
'Comment')`. Both opus and gemma counted plain `__.in('HAS_TAG')`, inflating counts
with the 309,766 forum edges - close to zero row overlap in the top-20 (F1 = 0.05). In
SQL the table name *is* the filter; with unified edge labels that constraint moves into
an explicit `hasLabel()` that the models dropped. AQL dodges this trap only because its
gold uses the same unified collections with `IS_SAME_COLLECTION` filters that the
models saw patterns for; Cypher dodges it because `(m)-[:HAS_TAG]->(t)` with
`WHERE m:Post OR m:Comment` was in-distribution.

### 4. Passive-voice edge direction (q12: gemma, off by exactly one row)

Gemma traversed `__.in('HAS_CREATOR')` from posts; the edge points Post->Person, so the
creators branch of the UNION yielded nothing. The likers branch alone produced 916 of
917 rows - the one missing person is the single creator who never liked a post in that
forum. A one-token direction error (`in` vs `out`) that survives syntax validation,
survives execution, and is nearly invisible in the result. It is a textbook case of the
known edge-direction ambiguity of passive SCREAMING_SNAKE names (`HAS_CREATOR` reads
naturally in both directions), and why execution accuracy - not validity - has to be
the primary metric.

### 5. Small local models cannot produce the dialect at all (llama, qwen)

llama3.2 (3B) passed syntax on 3 of 15 and executed 0: its outputs mixed Cypher
(`MATCH`-isms), Markdown backticks, invented step names (`PO_CREATION_DATE`) and Groovy
closures. qwen3-coder:30b is more interesting: 11 of 15 valid, but its failures show a
systematic pull toward the *Gremlin console dialect* - closures (`filter{ ... }`),
lambda parameters, `g.with(...)` - which the pure TinkerPop grammar (and any
server-side execution) rejects. Most of the Gremlin text on the internet is console
tutorials, so the training prior actively fights the strict grammar. On q08 qwen fell
into a deterministic degenerate loop (`.select('final1')...as('final2')...` forever at
temperature 0) and its record is an explicit `generation_hang` failure.

### 6. q15: the recursive hierarchy walk breaks all four base models, four different ways

q15 climbs the Place hierarchy transitively (`repeat(out('IS_PART_OF'))`). Among the four base
models it is an all-fail query - each fails for a different reason - and only opus-thinking, added
later, produces a passing traversal (section 7):

- **opus: a `tree()` result shape.** `...repeat(__.out('IS_PART_OF')).emit().tree()` is a valid
  traversal, but `tree()` returns a nested tree instead of rows and the result reader cannot
  decode the tree data type at all (`KeyError: <DataType.tree: 43>`); 0 rows. It is the class-1
  result-shape failure, one idiom further out.
- **gemma: the closest miss.** `repeat(out('IS_PART_OF')).emit().order().by(loops(), asc)` into a
  clean `project()` - but `emit()` after `repeat()` never emits the depth-0 start vertex, so it
  returns the 2 ancestors where the oracle wants 3 rows (start included).
- **llama: back to the console dialect.** It reverts to `g.with('start', 'place', id: 111)...`
  with a hallucinated `IS_SUBCLASS_OF`, which the pure TinkerPop grammar rejects; never validates.
- **qwen: a second generation hang.** Exactly like q08, at temperature 0 it unrolls
  `.union(__.optional(...).by(1), ...by(2), ...)` indefinitely over a hallucinated
  `IS_SUBCLASS_OF` and had to be aborted (capture in
  `ldbc_gremlin_qwen3-coder_30b_q15.txt` next to this file); recorded as `generation_hang`.

Once again the strong models fail on *shape* (opus's tree, gemma's missing start row) while the
small models fail on *dialect* - the same split as the rest of the target.

### 7. Extended thinking closes most of the gap (opus 0.53 -> opus-thinking 0.87)

The same Opus model, given an extended-thinking budget, fixes five of the seven queries
non-thinking opus loses (q05, q06, q08, q09, q15) and lands at 0.87 - above gemma's 0.67 and 34
points above its own terse configuration. Every fix is a *shape* or *endpoint* correction of
exactly the kind sections 1-3 diagnose, and each was inspected by hand:

- **q06, q08: duplicate-label `select()` -> `project()`.** Terse opus wrote
  `select('p2', 'p2', 'p2', 'k')` (q06) and `select('p3', 'p3', 'p3', 't')` (q08); thinking
  rewrote both as `project('id', 'first_name', 'last_name', ...)` with distinct keys and
  `by(__.select('k').values('creationDate'))` for the edge property. q08 is the sharper case:
  terse opus also ordered `.out('KNOWS').out('KNOWS').where(P.neq('p1')).as('p3')`, binding `p3`
  *after* the self-exclusion so it dropped 382 of 2,805 rows; thinking moved the `as('p3')`
  before the `where` and returned all 2,805 (`translated_rows` 2,423 -> 2,805).
- **q05: the unified-edge filter, finally applied.** Thinking added the
  `where(__.in('HAS_TAG').hasLabel('Post', 'Comment'))` guard (and the matching count filter)
  that section 3 shows every terse model dropped, excluding the 309,766 forum edges and matching
  the top-20 exactly.
- **q09: vertex filter and scoped ordering.** Thinking corrected
  `hasLabel('Post').has('id', ...)` to the single-vertex `has('Post', 'id', ...)` and moved
  `order()` after the `as('c')` binding so the sort key resolves against the aliased row.
- **q15: the all-fail query, broken open.** This is the one that made section 6's
  four-different-ways point - no base model passed it. Thinking is the exception. Instead of
  opus's unsupported `tree()` (the `KeyError: <DataType.tree: 43>`), it accumulates depth in a
  sack - `withSack(0)...emit().repeat(__.out('IS_PART_OF').sack(sum).by(__.constant(1)))
  .order().by(__.sack(), asc).project(..., 'depth').by(__.sack())` - and returns the correct 3
  rows, start vertex included. It is now the only model, of five, that passes q15.

The two it does *not* fix are as telling as the five it does. On q02 and q14 thinking produced
output byte-identical to terse opus (`project(...).by('content')` and
`group().by('name').by(__.count())` with no `unfold()`), and failed the same way. q14 is a
genuine reasoning miss: the missing `unfold()` on a grouped map escaped even the thinking pass.
q02 is not: the coalesce rule that fixes absent-property projection is prompt-gated on NULL
syntax and was never injected (section 2), so the model was reasoning without the one fact it
needed. Reasoning recovers shape and endpoint errors; it cannot recover information the prompt
withheld.

## Why these results, in my opinion

1. **Gremlin is an imperative dataflow language; SQL, Cypher and AQL are declarative.**
   A SQL join translates to Cypher/AQL almost clause-by-clause. In Gremlin the model
   must simulate a cursor: track where the traverser stands after every step, label
   intermediate positions with `as()`, and hop back with `select()`. Every failure in
   class 1 and the interest-explosion variants of q08 (gemma's UI-produced attempt
   returned 35,270 rows because `.out('HAS_INTEREST')` was applied from the wrong
   position) are traverser-bookkeeping errors, a failure category that structurally
   cannot exist in the declarative targets.

2. **The result-shape space is too large, and only execution reveals the mistake.** All
   opus failures except q05 returned the right *entities* in the wrong *shape*. The
   ANTLR validator (and even our structural component-F1, which scored opus at 0.91)
   cannot see this; only the positional multiset comparison against the oracle does.
   This is a strong argument for execution-based evaluation generally: on Gremlin the
   validity-to-correctness gap was 47 points for opus (1.00 vs 0.53), versus zero
   points on Cypher.

3. **Training-data scarcity plus dialect contamination.** Gremlin is a rare language in
   public corpora, and the corpus that exists is dominated by console examples with
   Groovy closures that are invalid in the grammar-checked, server-executed setting.
   The strongest models have internalized the pure-traversal subset; the 30B/3B models
   visibly have not.

4. **Schema conventions transfer semantics into the query.** Unified edge labels
   (HAS_TAG, IS_LOCATED_IN, HAS_CREATOR shared across endpoint pairs) are idiomatic
   for graphs but move information that SQL encodes in table names into obligatory
   `hasLabel()` filters and direction choices. q05 and q12 fell exactly there. A
   mapping that spells out per-edge endpoint pairs in the prompt (it does) is
   apparently not enough; the models need the *implication* (filter when traversing a
   shared label) stated as a rule.

5. **Terseness, not capability, was the ceiling - and reasoning proves it.** The old puzzle
   was gemma edging non-thinking opus (0.67 vs 0.53, a 2-query difference on n = 15). The
   mechanism was never that a 26B local model out-knew the frontier one: gemma used `project()`
   with distinct keys everywhere (clean shape discipline) and spent a long reasoning phase per
   query, while terse opus drifted into `select()`-based shapes on exactly the queries it lost,
   answering all 15 in one iteration at about 3 s and 80 output tokens each. On a language where
   the last three steps of the chain decide correctness, that economy is the liability. Extended
   thinking is the direct test: give the *same* opus model a reasoning budget and it does the
   shape checking gemma does, jumping to 0.87 - above gemma and 34 points above its terse self.
   The frontier model was never behind; its default was just too terse for an imperative target,
   and reasoning removes the ceiling.

6. **Reasoning pays off exactly where the shape is hard to get right.** The gain is
   language-shaped, not uniform: +34 points on imperative Gremlin, and zero on declarative
   Cypher and AQL, where opus-thinking is query-for-query identical to terse opus (see the
   companion reports). Reasoning buys nothing when the clause-by-clause answer is already
   correct; it buys almost everything when correctness lives in the last few traversal steps.
   That is the cleanest single-run evidence that the Gremlin difficulty is real and addressable,
   not a modeling artefact.

## Caveats

- 15 queries, one run, temperature 0: differences under ~2 queries between models are
  noise. The cross-language comparison (1.00 -> ~0.6 for the same models) is the robust
  signal, not the exact model ranking.
- Two provenance notes on q08: qwen's record is a synthetic failure (deterministic
  infinite generation, aborted; capture in
  `ldbc_gremlin_qwen3-coder_30b_q08.txt` next to this file), and gemma's record was produced by
  the same model/prompt via the Ollama UI after the harness call repeatedly appeared
  stuck (long-thinking, non-streaming call), then validated with the ANTLR checker
  before being recorded. Neither affects the aggregate picture (both attempts failed
  execution anyway - qwen by construction, gemma's with 35,270 vs 2,805 rows).
- The structural metrics (component F1, TED) now use a real Gremlin canonicaliser
  (method-chain tree, as-label alpha-renaming), but they measure *surface* similarity:
  opus's 0.91 component F1 against 0.53 execution accuracy quantifies exactly how
  misleading surface similarity is for this language. Note opus-thinking scores the same
  0.91 component F1 as terse opus while executing at 0.87 rather than 0.53 - structure did
  not move, only correctness did.
- Extended thinking is not free: on Gremlin it cost $0.564 vs $0.411 and 111 s vs 41 s total
  across the 15 queries (roughly 1.4x cost and 2.7x latency) for its +34-point gain, and it
  engaged on 13 of 15 queries (skipping only the trivial point-lookups q01 and q03). On Cypher
  and AQL the same budget bought no execution improvement at all - on Cypher the model mostly
  declined to think (7 of 15). Reasoning is worth spending on the imperative target and largely
  wasted on the declarative ones.
