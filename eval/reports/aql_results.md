# SQL -> AQL evaluation: results and analysis

Run of 2026-07-03. 14 LDBC gold queries x 4 models (llama3.2:latest, qwen3-coder:30b,
gemma4:26b via Ollama; claude-opus-4-8 via the Anthropic API), serial model-by-model,
temperature 0, max 3 generate-validate-fix iterations with offline syntax validation against
a hand-ported ArangoDB grammar (Flex+Bison, 3.11 branch, best-effort). Execution accuracy
measured against the Postgres oracle on graphonauts's ArangoDB (database `graphonauts`, LDBC
SNB SF1, camelCase attributes, ISO-8601 string dates, unified SCREAMING_SNAKE edge collections
built by `scripts/build_arango_unified_edges.py`). Optional text (image-post content) comes
back as `""` where Postgres has NULL and is reconciled by the comparator; each query runs under
a 180s ceiling (raised from 120s). The gold AQL set itself was validated first: all 14 gold
queries return exactly the same rows as their gold SQL (`scripts/validate_gold.py --target
aql`, 14/14).

## Headline

| model           | validity | pass@1 | component F1 | exec accuracy | result F1 |
|-----------------|----------|--------|--------------|---------------|-----------|
| claude-opus-4-8 | 1.00     | 1.00   | 0.91         | **0.93**      | 0.93      |
| gemma4:26b      | 1.00     | 0.93   | 0.90         | **0.93**      | 0.93      |
| qwen3-coder:30b | 1.00     | 1.00   | 0.86         | 0.50          | 0.50      |
| llama3.2:latest | 0.86     | 0.50   | 0.57         | 0.00          | 0.00      |

For contrast, on the same 14 queries Cypher reached execution accuracy 1.00 for both opus and
gemma (qwen 0.57, llama 0.21), and Gremlin only 0.57 (opus) and 0.71 (gemma). AQL sits between
them: both strong models are near-perfect but lose *exactly one* query - q12 - and it lands on
the one place AQL forces an imperative result-shaping decision, which is the failure class that
dominates Gremlin. The same models, the same SQL, the same schema mapping: only the target
language changed.

## Per-query execution accuracy

| query | opus | gemma | qwen | llama | note |
|-------|------|-------|------|-------|------|
| q01 point lookup        | 1 | 1 | 1 | 0 | llama emits the placeholder `LabelA` |
| q02 date range          | 1 | 1 | 1 | 0 | |
| q03 filter by name      | 1 | 1 | 1 | 0 | |
| q04 top-10 by count     | 1 | 1 | 0 | 0 | qwen: `COLLECT ... INTO` miscount |
| q05 tag usage           | 1 | 1 | 0 | 0 | opus correct but ~123s; qwen wrong direction |
| q06 friends + edge prop | 1 | 1 | 1 | 0 | |
| q07 3-way join          | 1 | 1 | 0 | 0 | qwen invents a `HAS_TYPE` hop |
| q08 friends-of-friends  | 1 | 1 | 0 | 0 | qwen over-counts, 21,084 vs 2,805 |
| q09 reply chain         | 1 | 1 | 0 | 0 | qwen: `replyOfPostId` property, not `REPLY_OF` |
| q10 LEFT JOIN count     | 1 | 1 | 1 | 0 | |
| q11 HAVING              | 1 | 1 | 1 | 0 | |
| q12 UNION               | 0 | 0 | 0 | 0 | everyone: `RETURN UNION_DISTINCT(...)` array |
| q13 NOT EXISTS          | 1 | 1 | 1 | 0 | |
| q14 group by country    | 1 | 1 | 0 | 0 | qwen: `IS_SUBCLASS_OF` hallucination |

q12 is the only query all four fail, opus and gemma included. No query is passed by all four,
because llama fails every one.

## Anatomy of the failures (reconstructed from the records and the row cache)

Each failure below was read off the recorded translation, the recorded ArangoDB error string,
and the cached result rows that produced the metrics.

### 1. q12 UNION: the one query nobody gets, opus and gemma included

Every model gets the two halves of the UNION exactly right and then fails on the last line.
The gold assembles the two branches and *unfolds* the union back into rows:

```aql
FOR x IN UNION_DISTINCT(creators, likers)
  RETURN x
```

Opus (and gemma, and qwen) instead return the union as a value:

```aql
LET a = ( FOR f IN Forum FILTER f.id == 137439023186
    FOR po IN OUTBOUND f CONTAINER_OF FOR p IN OUTBOUND po HAS_CREATOR
      RETURN { id: p.id, first_name: p.firstName, last_name: p.lastName } )
LET b = ( FOR f IN Forum FILTER f.id == 137439023186
    FOR po IN OUTBOUND f CONTAINER_OF FOR p IN INBOUND po LIKES
      RETURN { id: p.id, first_name: p.firstName, last_name: p.lastName } )
RETURN UNION_DISTINCT(a, b)
```

`RETURN UNION_DISTINCT(a, b)` emits a *single row* holding the whole 917-element array, so the
positional compare against the oracle's 917 rows fails outright (translated_rows = 1). This is
the AQL manifestation of Gremlin's number-one failure - right data, wrong result shape - and it
is the single point in the language where the otherwise clause-by-clause mapping from SQL breaks
down: a collection-returning function has to be re-unfolded into rows with a `FOR`, an
imperative step that has no counterpart in the SQL the model is translating.

### 2. Everything else, the strong models get right (13/14)

Opus and gemma each pass the other 13. Their AQL is idiomatic throughout:
`LET member_count = LENGTH(FOR ... RETURN 1)` for junction-table aggregations,
`FOR v, e IN OUTBOUND ... KNOWS` to read an edge property, `IS_SAME_COLLECTION('Post', m)` to
filter the unified `HAS_TAG` collection. The only wrinkle is opus's q05, which is a *correct*
translation with a performance problem (see caveats), not a translation error.

### 3. llama3.2: it cannot produce the dialect, and it says so (0/14)

llama's failure is the most literal in the whole matrix: it emits the translation prompt's
*example placeholder identifiers* verbatim. Collections come out as `LabelA`, `LabelF`,
`LabelC`, `LabelTag`; edges as `REL_AB`, `REL_KNOWS`, `REL_BC`, `REL_FORUM_HAS_MEMBER`:

```aql
FOR v IN LabelA
  FILTER v.id == 933
  RETURN { id: v.id, firstName: v.firstName, lastName: v.lastName, ... }
```

These are valid AQL identifiers, so 12 of 14 pass the grammar check, but none of them exist in
the database, so every one hits `[HTTP 404][ERR 1203] collection or view not found: LabelA`.
The rest of llama's output is broken in other ways: q13 uses the edge label as a function,
`FOR k IN KNOWS(p)` (`[ERR 1540] usage of unknown function 'KNOWS()'`); q05 references an
unbound variable; and q09 and q12 never validate at all (a `SORT` after `RETURN`; a SQL-style
`UNION` between two `FOR` blocks). Nothing executes.

### 4. qwen3-coder: fluent AQL, the same graph-schema mistakes it makes in Cypher (7/14)

qwen writes clean, idiomatic AQL (component F1 0.855) and passes the seven queries that need
only a straight traversal (q01-q03, q06, q10, q11, q13). It loses the rest to graph-schema
misunderstandings that are, tellingly, the *same ones it shows on Cypher*:

- **q14: `FOR country IN OUTBOUND city IS_SUBCLASS_OF`** - the identical `IS_SUBCLASS_OF`-for-
  `IS_PART_OF` hallucination as its Cypher q14; 0 rows.
- **q09: `FILTER c.replyOfPostId == 412317942891`** - the identical property-instead-of-
  `REPLY_OF`-traversal error as its Cypher q09; 0 rows.
- **q07 / q08: an invented `HAS_TYPE` edge** to reach tags. q08 also matches interests by a
  `tag_id` property rather than a shared Tag node, producing a cartesian over-count of 21,084
  rows against the correct 2,805.
- **q05:** traverses `OUTBOUND t HAS_TAG` (the wrong direction - posts and comments point *to*
  tags), so 0 rows.
- **q04:** a `COLLECT forum_id = f.id ... INTO member_list` whose `LENGTH(member_list)`
  miscounts members - the right 10 rows with wrong counts.
- **q12:** the same `RETURN UNION_DISTINCT(...)` array-shape error as everyone else.

Four queries (q05, q09, q12, q14) fail for qwen in *both* languages, for the same reason each
time. The errors are language-independent - the clearest evidence in the run that the
local-model gap is about the graph model, not the AQL dialect.

### 5. A metric artefact worth flagging

`f1_node_labels` is only 0.695 for opus *and* gemma on AQL, against 1.00 on Cypher. That is not
a modeling error: AQL addresses entities as collections/documents (`FOR p IN Person`), which the
structural canonicaliser scores differently from Cypher's `(:Person)`. Both models execute at
0.93. It is one more reason to read execution, not structure, as ground truth.

## Why these results, in my opinion

1. **AQL is declarative, with one imperative seam.** Like Cypher, a capable model maps SQL to
   AQL almost clause-by-clause and nearly aces it - except that AQL's collection-returning
   `UNION_DISTINCT` has to be re-unfolded with an explicit `FOR ... RETURN`. That single
   result-shaping step is the failure class that pervades Gremlin, and it is precisely why q12
   is the only query even opus and gemma miss. AQL is, in effect, "Cypher plus one
   Gremlin-shaped trap."

2. **The unified-edge convention is handled here.** The models saw the SCREAMING_SNAKE
   collection patterns (`HAS_TAG`, `HAS_CREATOR`) and the `IS_SAME_COLLECTION` filter, and the
   collections exist only because `build_arango_unified_edges.py` builds them. Given both, the
   endpoint-discipline problem that sank q05 on Gremlin does not sink it here.

3. **The local-model gap is conceptual and language-independent.** llama cannot produce AQL at
   all and falls back on the prompt's placeholder identifiers; qwen writes fluent AQL but
   carries the exact graph-schema misconceptions it shows in Cypher. Neither is a dialect
   problem - qwen's four shared failures reproduce error-for-error across the two languages.

4. **Structural similarity is misleading again.** The node-label artefact understates the strong
   models, and qwen's 0.855 structure sits far above its 0.500 execution. Only execution against
   the oracle separates fluent-but-wrong from correct.

## Caveats

- 14 queries, one run, temperature 0: sub-2-query differences are noise. The robust signals are
  categorical - q12 defeats everyone, the two strong models are otherwise near-perfect, the two
  local models trail badly.
- The 180s ceiling (`EVAL_QUERY_TIMEOUT`, raised from 120s) matters here. Opus's q05 is a
  *correct* translation that materialises full documents into arrays instead of counting with
  `RETURN 1`; it runs ~123s and only counts as a pass at 180s. That single change moved opus's
  headline from 0.857 to 0.929, tying gemma. `translated_runtime_s` keeps slow-but-correct
  queries visible.
- Vacuous matches (both stores return 0 rows -> accuracy 1.0) can flatter a latent bug; qwen's
  empty results are *not* flattered, because their reference sets are non-empty and they score 0.
- Execution depends on the environment: the unified SCREAMING_SNAKE edge collections must be
  rebuilt by `scripts/build_arango_unified_edges.py` after every ArangoDB (re)load (database
  `graphonauts`), or the traversals 404 and score 0. ISO-8601 string dates and `""`-vs-NULL text
  are reconciled by the comparator.
- llama's q09 and q12 never pass static validation (54/56 AQL translations validate). Validity
  is not correctness: here even the 12 that validate execute at 0.
