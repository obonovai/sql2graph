# SQL -> Gremlin evaluation: results and analysis

Run of 2026-07-02. 14 LDBC gold queries x 4 models (llama3.2:latest, qwen3-coder:30b,
gemma4:26b via Ollama; claude-opus-4-8 via the Anthropic API), serial model-by-model,
temperature 0, max 3 generate-validate-fix iterations with offline ANTLR (TinkerPop
grammar) validation. Execution accuracy measured against the Postgres oracle on
graphonauts2's in-memory TinkerGraph (Gremlin Server 3.8.1, LDBC SNB SF1, unified
SCREAMING_SNAKE edge labels, ISO-8601 string dates). The gold Gremlin set itself was
validated first: all 14 gold queries return exactly the same rows as their gold SQL
(`scripts/validate_gold.py --target gremlin`, 14/14).

## Headline

| model           | validity | pass@1 | component F1 | exec accuracy | result F1 |
|-----------------|----------|--------|--------------|---------------|-----------|
| gemma4:26b      | 1.00     | 0.93   | 0.92         | **0.71**      | 0.82      |
| claude-opus-4-8 | 1.00     | 1.00   | 0.92         | 0.57          | 0.60      |
| qwen3-coder:30b | 0.79     | 0.71   | 0.85         | 0.21          | 0.24      |
| llama3.2:latest | 0.21     | 0.21   | 0.67         | 0.00          | 0.00      |

For contrast, on the same 14 queries the Cypher and AQL targets reached execution
accuracy 1.00 for both opus and gemma (qwen 0.71, llama 0.29). Gremlin cut the two
strongest models roughly in half and crushed the weaker two. That gap is the headline
finding: the *same* models, the *same* SQL, the *same* schema mapping - only the target
language changed.

## Per-query execution accuracy

| query | opus | gemma | qwen | llama | note |
|-------|------|-------|------|-------|------|
| q01 point lookup        | 1 | 1 | 1 | 0 | |
| q02 date range          | 0 | 0 | 0 | 0 | nobody survives the nullable column |
| q03 filter by name      | 1 | 1 | 1 | 0 | |
| q04 top-10 by count     | 1 | 1 | 0 | 0 | |
| q05 tag usage           | 0 | 0 | 0 | 0 | nobody filters the unified edge |
| q06 friends + edge prop | 0 | 1 | 0 | 0 | |
| q07 3-way join          | 1 | 1 | 0 | 0 | |
| q08 friends-of-friends  | 0 | 0 | 0 | 0 | hardest; see below |
| q09 reply chain         | 0 | 1 | 0 | 0 | |
| q10 LEFT JOIN count     | 1 | 1 | 0 | 0 | |
| q11 HAVING              | 1 | 1 | 0 | 0 | |
| q12 UNION               | 1 | 0 | 0 | 0 | gemma off by exactly 1 row |
| q13 NOT EXISTS          | 1 | 1 | 1 | 0 | |
| q14 group by country    | 0 | 1 | 0 | 0 | |

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
`RETURN {x: a.x}` map one-to-one onto a SELECT list. Gremlin has at least four
result-shaping idioms (`project`, `select`, `valueMap`, `group`), each with different
row semantics, and only one of them matches a SQL result set without extra steps.

### 2. Absent properties silently shorten rows (q02: every model failed identically)

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

### 3. Unified edge labels demand endpoint discipline (q05: every model failed)

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

llama3.2 (3B) passed syntax on 3 of 14 and executed 0: its outputs mixed Cypher
(`MATCH`-isms), Markdown backticks, invented step names (`PO_CREATION_DATE`) and Groovy
closures. qwen3-coder:30b is more interesting: 11 of 14 valid, but its failures show a
systematic pull toward the *Gremlin console dialect* - closures (`filter{ ... }`),
lambda parameters, `g.with(...)` - which the pure TinkerPop grammar (and any
server-side execution) rejects. Most of the Gremlin text on the internet is console
tutorials, so the training prior actively fights the strict grammar. On q08 qwen fell
into a deterministic degenerate loop (`.select('final1')...as('final2')...` forever at
temperature 0) and its record is an explicit `generation_hang` failure.

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
   ANTLR validator (and even our structural component-F1, which scored opus at 0.92)
   cannot see this; only the positional multiset comparison against the oracle does.
   This is a strong argument for execution-based evaluation generally: on Gremlin the
   validity-to-correctness gap was 43 points for opus (1.00 vs 0.57), versus zero
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

5. **On gemma beating opus (0.71 vs 0.57):** with n = 14 this is a 2-query difference,
   so it should not be over-read. That said, the pattern is consistent: gemma used
   `project()` with distinct keys everywhere (perfect shape discipline) while opus
   drifted into `select()`-based shapes on exactly the queries it lost, and gemma's
   long reasoning phase (thousands of thinking tokens per query, at 10 to 40 times
   opus's latency and roughly 60 seconds per query) appears to buy real shape checking.
   Opus optimizes for terse first-shot answers (all 14 in one iteration, about 3 s and
   80 output tokens each); on a language where the last three steps of the chain decide
   correctness, that economy is a liability. A fix-loop that validated *shapes* (not
   just syntax) would likely close most of opus's gap.

## Caveats

- 14 queries, one run, temperature 0: differences under ~2 queries between models are
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
  opus's 0.92 component F1 against 0.57 execution accuracy quantifies exactly how
  misleading surface similarity is for this language.
