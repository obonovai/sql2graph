# SQL -> Cypher evaluation: results and analysis

Run of 2026-07-03. 15 LDBC gold queries x 4 models (llama3.2:latest, qwen3-coder:30b,
gemma4:26b via Ollama; claude-opus-4-8 via the Anthropic API), serial model-by-model,
temperature 0, max 3 generate-validate-fix iterations with offline ANTLR (Neo4j Cypher
grammar) validation. Execution accuracy measured against the Postgres oracle on
graphonauts's Neo4j (LDBC SNB SF1, camelCase properties, unified SCREAMING_SNAKE
relationship types, native temporal dates so `datetime('...')` predicates match directly).
The gold Cypher set itself was validated first: all 15 gold queries return exactly the same
rows as their gold SQL (`scripts/validate_gold.py --target cypher`, 15/15).

## Headline

| model           | validity | pass@1 | component F1 | exec accuracy | result F1 |
|-----------------|----------|--------|--------------|---------------|-----------|
| claude-opus-4-8 | 1.00     | 1.00   | 0.98         | **1.00**      | 1.00      |
| gemma4:26b      | 1.00     | 1.00   | 0.98         | **1.00**      | 1.00      |
| qwen3-coder:30b | 1.00     | 1.00   | 0.93         | 0.53          | 0.56      |
| llama3.2:latest | 1.00     | 0.87   | 0.82         | 0.20          | 0.21      |

For contrast, on the same 15 queries the AQL target reached execution accuracy 0.93 for
both opus and gemma (qwen 0.47, llama 0.00), and Gremlin only 0.53 (opus) and 0.67 (gemma).
Cypher is the one target where both strong models are flawless - 30 for 30 - so every bit of
variance here lives in the two small local models. The same models, the same SQL, the same
schema mapping: only the target language changed.

## Per-query execution accuracy

| query | opus | gemma | qwen | llama | note |
|-------|------|-------|------|-------|------|
| q01 point lookup        | 1 | 1 | 1 | 1 | |
| q02 date range          | 1 | 1 | 1 | 0 | llama: `WITH` drops `p` from scope |
| q03 filter by name      | 1 | 1 | 1 | 0 | llama invents a `KNOWS` hop |
| q04 top-10 by count     | 1 | 1 | 1 | 1 | |
| q05 tag usage           | 1 | 1 | 0 | 0 | qwen: cartesian double `OPTIONAL MATCH`, timed out |
| q06 friends + edge prop | 1 | 1 | 0 | 0 | qwen projects the anchor's date, not the edge's |
| q07 3-way join          | 1 | 1 | 1 | 0 | |
| q08 friends-of-friends  | 1 | 1 | 1 | 0 | |
| q09 reply chain         | 1 | 1 | 0 | 0 | qwen: property filter, not `REPLY_OF` |
| q10 LEFT JOIN count     | 1 | 1 | 0 | 0 | both locals: all forums, zero counts |
| q11 HAVING              | 1 | 1 | 1 | 0 | |
| q12 UNION               | 1 | 1 | 0 | 0 | |
| q13 NOT EXISTS          | 1 | 1 | 1 | 1 | |
| q14 group by country    | 1 | 1 | 0 | 0 | qwen: `IS_SUBCLASS_OF` hallucination |
| q15 recursive ancestors | 1 | 1 | 0 | 0 | llama: single `IS_PART_OF` hop, not `*`; qwen: constant `depth`, drops start row |

The two strong models pass all 15; q01, q04 and q13 are passed by everyone; no query defeats
all four. The interesting content is entirely in why the two local models fail.

## Anatomy of the failures (reconstructed from the records and the row cache)

Every failure below was read off the recorded translation (`generated_query` vs
`expected_query`), the recorded execution error, and the cached result rows that produced the
metrics. There were no strong-model failures to dissect - opus and gemma execute all 15
correctly - so this is a study of the two local models.

### 1. The declarative target is nearly free for a capable model (opus, gemma: 30/30)

A SQL join maps to a Cypher `MATCH` pattern almost clause-by-clause, and `RETURN a.x, a.y`
lines up one-to-one with the SELECT list. There is simply no result-shape space to get lost
in, which is where most Gremlin translations die. The two schema-convention traps that beat
*every* model on Gremlin also evaporate here:

- **Nullable columns (q02).** A post without content reads back as `p.content -> null`, which
  slots into the result exactly like the SQL NULL. (On Gremlin the same absent property drops
  the key from the projected map and shortens the row.)
- **Unified edge labels (q05).** The shared `HAS_TAG` label is disambiguated with an
  in-distribution `WHERE m:Post OR m:Comment`; opus and gemma both wrote it and both passed.

Worth noting: opus and gemma are frequently *not* an exact string match to the gold (they use
`WITH ... count()` where the gold aggregates directly, equivalent direction handling, and so
on) yet execute correctly every time. For these two models validity, structure and execution
all sit at ~1.00 - the validity-to-correctness gap is zero, versus 47 points for opus on
Gremlin.

### 2. llama3.2: relationship tables become nodes (3/15)

llama's signature error is a data-modeling one: it treats SQL junction/relationship tables as
node labels rather than edges. Across its failures the pattern is unmistakable -

- q06: `OPTIONAL MATCH (k:Knows {person_id: p1.id})` - the `knows` table as a `:Knows` node.
- q07: `(po)-[:HAS_TAG]->(pht:TagHasPost {post_id: po.id})` - `post_has_tag` as a node.
- q10: `(f)-[:HAS_MEMBER]->(fm:ForumHasMember)` - `forum_has_member` as a node.
- q12: `(l:LikesPost {person_id: p.id})-[:LIKES]->(po:Post)` - `likes_post` as a node.

None of these labels exist in the graph, so each returns empty or null-filled rows. On top of
that, two failures are hard runtime errors that survive static validation: q02 puts
`WITH c.creationDate AS creation_date` before `RETURN p.id`, dropping `p` from scope
(`Variable p not defined`), and q12's `NOT EXISTS((l:LikesPost ...)-[:LIKES]->(po))` binds a
new variable inside a pattern expression (`PatternExpressions are not allowed to introduce new
variables: 'l'`). llama also hallucinates freely - q03 traverses `KNOWS` from `Person {id: 0}`,
q14 invents a literal `Country {name: 'country_name'}` (emitting the SQL alias as data) - and
leaks snake_case properties (`po.creation_date`, `p.first_name`). It passes only the three
shallowest queries: q01 (an exact-match point lookup), q04 (the one aggregation it happened to
model as a real edge) and q13 (no-friends). q15 adds a new failure shape: for the transitive
Place-ancestor walk it writes a single `OPTIONAL MATCH (p)-[:IS_PART_OF]->(parent)` hop instead
of a variable-length `[:IS_PART_OF*0..]` path, returning one row where the walk should climb
three levels.

### 3. qwen3-coder: fluent Cypher, wrong graph semantics (8/15)

qwen is the more instructive case. Its labels and clause structure are near-perfect (component
F1 0.925), and it never models a junction table as a node. It loses exactly the queries that
turn on edge *semantics*:

- **Direction (q10).** `OPTIONAL MATCH (f)<-[:HAS_MEMBER]-(p:Person)` reverses the edge (it
  points Forum->Person), so `count(p)` is 0 for every forum.
- **Traversal vs property (q09).** `WHERE c.replyOfPostId = 412317942891` invents a scalar
  property instead of walking `-[:REPLY_OF]->`; the property does not exist, so 0 rows.
- **Edge-property projection (q06).** It drops the edge binding and returns `p1.creationDate`
  (the anchor person's date) where the gold returns the `KNOWS` edge's `k.creationDate` -
  right rows, wrong values.
- **Hallucinated type (q14).** `-[:IS_SUBCLASS_OF]->` for the city->country hop instead of
  `IS_PART_OF`; 0 rows.
- **Malformed UNION leg (q12).** It filters on a non-existent `po.forum_id` property (forum
  membership is the `CONTAINER_OF` edge) and reverses `HAS_CREATOR`; both legs return 0.
- **Cartesian blow-up (q05).** Two `OPTIONAL MATCH`es on the shared tag produce a
  post x comment product that, for popular tags, does not finish inside the 180s ceiling and
  is killed (`TransactionTimedOut`).
- **Depth bookkeeping (q15).** For the transitive Place-ancestor walk it wraps the traversal
  in a `CALL {}` subquery whose result it discards, then emits a constant `1 AS depth` instead
  of `length(path)` and omits the depth-0 start node; only one of three rows matches
  (result_f1 0.33).

Tellingly, qwen writes `HAS_CREATOR` in the *correct* direction in q07 (which passes) and the
*wrong* direction in q12 - these are per-query lapses, not a systematic direction bug.

### 4. q10: the partial-credit trap (both local models)

Both local models return exactly 90,492 rows for q10 - the full forum list, matching the LEFT
JOIN's cardinality - but with every member count zero (llama via the phantom `ForumHasMember`
node, qwen via the reversed edge). The rows that happen to match are the forums that are
genuinely empty, giving result_f1 = 0.1218 for both, identically. Execution accuracy is still
0: the right number of rows with the wrong values is a failure, and only a positional multiset
compare against the oracle catches it.

## Why these results, in my opinion

1. **Cypher is declarative and shape-faithful.** A SQL join becomes a `MATCH` pattern almost
   clause-by-clause, and `RETURN a.x, a.y` is one-to-one with the SELECT list. The single
   largest failure category on Gremlin - right entities, wrong result shape - structurally
   cannot occur here, which is most of why the strong models jump from ~0.6 on Gremlin to a
   clean 1.00.

2. **The schema-convention traps are absorbed by the language.** Nullable properties read back
   as `null`, and unified edge labels are handled with an ordinary `WHERE m:Post OR m:Comment`.
   The two traps that beat everyone on Gremlin cost the capable models nothing on Cypher.

3. **The local-model gap is conceptual, not dialectal.** llama has not internalized that a SQL
   junction table is an edge, not a node; qwen writes fluent, idiomatic Cypher but misreads
   edge direction, edge-vs-property and relationship names. Neither is a syntax problem - all
   60 translations pass static validation. And the gap is language-independent: qwen makes the
   *identical* `IS_SUBCLASS_OF` and `replyOfPostId` mistakes in AQL (see `aql_results.md`).

4. **Structural similarity badly overstates local-model correctness.** qwen scores 0.925
   component F1 but 0.533 execution; llama 0.823 vs 0.200. Only execution against the oracle
   separates fluent-but-wrong from correct. Even for opus and gemma the weakest structural axis
   is edge direction (0.883), but on Cypher it never actually costs them a result.

## Caveats

- 15 queries, one run, temperature 0: differences under ~2 queries are noise. The robust
  signal here is categorical - both strong models are perfect, both local models are far from
  it - not any fine-grained ranking.
- q10 (both local models, result_f1 0.12) and qwen's q15 (result_f1 0.33) score 0 despite
  partially correct output - the right rows with the wrong values - and are counted as
  failures.
- qwen's q05 is a genuine cartesian blow-up killed at the 180s ceiling (`EVAL_QUERY_TIMEOUT`),
  not a fast query penalised for engine speed; `translated_runtime_s` records the 180s.
- Strong-model queries are frequently not exact matches and carry non-zero edit distance
  (semantically equivalent phrasings). This is exactly why execution accuracy, not structural
  similarity, is the primary metric.
- Vacuous matches (both stores return 0 rows -> accuracy 1.0) do not arise here: every gold
  result is non-empty (minimum 1 row, at q01).
