# Model comparison: qwen3-coder:30b vs claude-opus-4-8

SQL -> Cypher translation on the LDBC SNB benchmark, generated 2026-06-30; execution metrics re-run 2026-07-01 after graphonauts2 switched Neo4j to native temporal date storage.

## What was run

Both models translated the same **14 curated LDBC queries** (relational SQL -> Neo4j Cypher) through the rows2graph generate-validate-fix loop, with the schema supplied by `examples/mappings/ldbc.yaml`. Identical harness, identical gold set, identical validators. Two layers of scoring:

- **Static** (no database): Pass@1 via Cypher syntax validation, Exact Match and Component-F1 against the gold Cypher, and token tree-edit / Levenshtein / Jaccard distances.
- **Execution** (graphonauts2 SF1 databases): the generated Cypher is run on Neo4j and compared, as a result-set multiset, against the gold SQL run on Postgres (the oracle). This is the strongest signal - it answers "did the translation preserve semantics?".

`qwen3-coder:30b` ran locally via Ollama; `claude-opus-4-8` ran via the Anthropic API.

## Headline numbers

| Metric | qwen3-coder:30b | claude-opus-4-8 |
|---|---|---|
| Pass@1 (syntax valid, first try) | 14/14 (1.00) | 14/14 (1.00) |
| Exact Match | 5/14 (0.357) | 4/14 (0.286) |
| Component-F1 | 0.928 | **0.986** |
| Normalized tree-edit distance (lower is better) | 0.159 | **0.074** |
| **Execution accuracy** | **10/14 (0.714)** | **13/14 (0.929)** |
| Result-set F1 | 0.723 | **0.929** |
| Mean iterations | 1.00 | 1.00 |
| Mean wall-clock per query | 41.0 s (local) | 2.4 s (API) |
| Total cost | $0.00 (local) | ~$0.36 |

Both models pass syntax validation on every query on the first try, so **Pass@1 does not separate them**. The separation appears only under execution: Opus is right on 13 of 14, qwen on 10 of 14 - and, as detailed below, two of qwen's ten "passes" are hollow, so the real gap is wider than the headline.

## Per-query execution verdict

| Query | What it tests | qwen | opus |
|---|---|:---:|:---:|
| q01 | point lookup by id | pass | pass |
| q02 | date-range filter on Post | pass | **fail** |
| q03 | string-equality lookup | pass | pass |
| q04 | top forums by member count (agg) | pass | pass |
| q05 | most-used tags across Post + Comment | **fail** | pass |
| q06 | friends of a person (edge property) | **fail** | pass |
| q07 | 3-way join with date filter | pass | pass |
| q08 | friends-of-friends sharing a tag | pass | pass |
| q10 | comments replying to a post | pass* | pass |
| q11 | forums incl. empty (LEFT JOIN) | **fail** | pass |
| q12 | persons with > 5 friends (HAVING) | pass | pass |
| q13 | forum post-creators OR likers (UNION) | pass* | pass |
| q14 | persons with no friends (NOT EXISTS) | pass | pass |
| q15 | persons per country (place traversal) | **fail** | pass |

`*` = vacuous pass: the oracle returns zero rows for the chosen id, so any query that also returns zero scores 1.0. See the caveat below - qwen's q10 and q13 "pass" this way despite containing real bugs.

## What qwen3-coder:30b did

qwen produced syntactically valid Cypher every time, and got the simple and several hard queries genuinely right (q01, q03, q04, q08 with 2,805 rows, q12 with 5,407, q14 with 1,642). But on six queries it diverged from the graph model, and the divergences fall into one consistent theme: **it carried relational, foreign-key-as-property thinking into the graph instead of traversing edges.**

- **q06 - projection error.** Returned `p1.creationDate` (person 933's own date) instead of `k.creationDate` (the KNOWS relationship's date). Right row count, wrong column.
- **q10 - property hallucination (masked).** Filtered `c.replyOfPostId = ...`, a property that does not exist in the graph (it is the `REPLY_OF` edge). Returns zero rows; passes only because the oracle is also empty for that post.
- **q11 - direction error.** Wrote `(f)<-[:HAS_MEMBER]-(p)` (incoming) when the graph has `(Forum)-[:HAS_MEMBER]->(Person)`. Every forum's member count collapses to zero (result F1 = 0.12).
- **q13 - property hallucination (masked).** Filtered `po.forum_id = ...` instead of traversing `(:Forum)-[:CONTAINER_OF]->(:Post)`. Wrong structure; passes only because the oracle is empty for that forum.
- **q15 - edge-type hallucination.** Used `-[:IS_SUBCLASS_OF]->` (a TagClass-to-TagClass edge) instead of `-[:IS_PART_OF]->` (the Place hierarchy). Returns zero rows.
- **q05 - performance.** Restructured the polymorphic tag count into a double `OPTIONAL MATCH` over 1M posts and 2M comments; the query timed out at 60 s on Neo4j.

So of qwen's 14: **8 genuinely correct (now including q02 and q07, which its `datetime('...')` predicates translate faithfully once Neo4j stores native temporals), 4 real execution failures (q05, q06, q11, q15), and 2 vacuous passes hiding property-hallucination bugs (q10, q13)**. The static metrics already hinted at the structural failures - Component-F1's weakest buckets for qwen were `directions` (0.77) and `where` (0.77) - but only execution made the bugs concrete.

## What claude-opus-4-8 did

Opus produced correct Cypher on **13 of 14 queries**. Its only failure is q02, where it filtered a timestamp column with `date('2010-06-01')` instead of `datetime('...')`; comparing a `datetime` property to a `date` literal returns null in Neo4j, so it gets zero rows (see the caveat below). q07, which it translated with `datetime('...')`, passes. On everything else it traversed the graph correctly, and it fixed every one of qwen's structural bugs:

- **q06**: used `k.creationDate` (the edge property) - correct projection.
- **q11**: used `(f)-[:HAS_MEMBER]->(p)` - correct direction.
- **q15**: used `-[:IS_PART_OF]->` - correct edge type.
- **q05**: produced essentially the hand-written gold - `MATCH (m)-[:HAS_TAG]->(t:Tag) WHERE m:Post OR m:Comment` - the polymorphic label-disjunction that is both correct and efficient (no timeout).
- **q10 / q13**: used the correct `REPLY_OF` and `CONTAINER_OF` edge traversals (genuinely correct patterns that happen to return zero rows for the chosen ids), where qwen used hallucinated properties.

Opus was also far faster here (2.4 s vs 41.0 s per query) and cost about 36 cents total - though the speed gap reflects local 30B inference on this machine versus a cloud API, not just the models.

## Side-by-side: the four queries that separated them

```
q06  projection (edge date vs person date)
  gold:  (p1:Person {id:933})-[k:KNOWS]->(p2) RETURN ... k.creationDate AS friendship_date
  opus:  (p1:Person {id:933})-[k:KNOWS]->(p2) RETURN ... k.creationDate AS friendship_date     CORRECT
  qwen:  (p1:Person {id:933})-[:KNOWS]->(p2)  RETURN ... p1.creationDate AS friendshipDate      wrong column

q11  edge direction (HAS_MEMBER)
  opus:  OPTIONAL MATCH (f)-[:HAS_MEMBER]->(p:Person)     CORRECT
  qwen:  OPTIONAL MATCH (f)<-[:HAS_MEMBER]-(p:Person)     flipped -> all counts 0

q15  edge type (place hierarchy)
  opus:  (p:Person)-[:IS_LOCATED_IN]->(city)-[:IS_PART_OF]->(country)        CORRECT
  qwen:  (p:Person)-[:IS_LOCATED_IN]->(city)-[:IS_SUBCLASS_OF]->(country)    wrong edge -> 0 rows

q05  scan shape (polymorphic tag count)
  opus:  MATCH (m)-[:HAS_TAG]->(t:Tag) WHERE m:Post OR m:Comment ...         CORRECT + efficient
  qwen:  MATCH (t:Tag) OPTIONAL MATCH (t)<-[:HAS_TAG]-(:Post)
                       OPTIONAL MATCH (t)<-[:HAS_TAG]-(:Comment) ...          timed out
```

## Caveats (read before quoting the numbers)

- **Exact Match is not a quality signal here.** Opus scores *lower* on Exact Match (4/14) than qwen (5/14) despite being better on every other metric, because Opus adds equivalent aliases (`AS id`, `AS title`) and phrasings that differ from the gold string. This is exactly why Component-F1, tree-edit distance, and execution accuracy are the metrics to trust.
- **The former shared q02/q07 failures are resolved (storage fix).** graphonauts2 originally stored `creationDate` as an epoch-millis Integer, which no `datetime('...')`/`date('...')` predicate could match; it now loads native `DateTime`/`Date` temporals. After reloading Neo4j, q07 passes for both models and q02 passes for qwen. Opus's q02 still fails, but for a genuine translation reason: it wrote `p.creationDate >= date('2010-06-01')`, comparing a `datetime` property to a `date` literal (a type mismatch Neo4j evaluates to null). The faithful translation of a timestamp-column range filter is `datetime('...')` - what qwen, the gold, and Opus's own q07 use.
- **Two of qwen's passes are vacuous.** q10 and q13 score 1.0 only because the oracle returns zero rows for the chosen ids; qwen's queries there contain real property-hallucination bugs (`replyOfPostId`, `forum_id`) that are simply masked. So the genuine comparison is **Opus 13/14 correct vs qwen 8/14 correct** - the headline execution gap (13 vs 10) understates qwen's deficit.
- **Cost includes prompt caching.** The $0.36 figure prices all billed tokens at Opus 4.8's $5/$25 per Mtok: uncached input and output at the base rate, plus the cached system block (cached across the fix loop) at 1.25x for writes and 0.1x for reads of the input rate. Those cache tokens dominate the bill - pricing only the uncached input/output undercounts it by roughly 9x. qwen is free because it runs locally.
- **Single dataset, single target, n=14.** This is LDBC/Cypher only. The harness is built to extend to AQL, Gremlin, TPC-H, and more models, but those numbers do not exist yet.

## Bottom line

On syntax, the two models are indistinguishable (both 100% Pass@1). On semantics, they are not: **Opus 4.8 translated 13 of 14 queries correctly (its one miss a date-vs-datetime type slip on q02), while qwen3-coder:30b translated 8 of 14 correctly**, with a consistent failure mode of treating graph relationships as relational foreign-key columns - wrong edge direction, wrong edge type, hallucinated FK properties, and one query rewritten into a form that does not scale. The exercise also shows why execution-based evaluation matters: every one of these differences is invisible to Pass@1 and partly hidden even from string-similarity metrics.
