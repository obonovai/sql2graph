# Evaluation metrics

**How every score in the `sql2graph` evaluation harness is defined and computed - the methodology companion to [eval/README.md](README.md), which covers how to run it.**

Each metric scores a *generated* graph query (the LLM translator's output for one
SQL query) against a *gold* reference query for the same target language, and - for
the execution family only - against a live result set produced by running both the
gold and the generated query. Every metric is computed per record (one attempt =
one `(dataset, target, model, query_id)` cell), then aggregated in the notebooks
(per model, per target, per difficulty; targets are never mixed).

The metrics fall into families:

- **(a) Exact and structural string metrics** - Exact Match, Component F1,
  Levenshtein, Jaccard. String/token comparisons over a canonical form. DB-free.
- **(b) Tree-structural metrics** - normalized tree-edit distance (APTED) over a
  per-target parse tree. DB-free.
- **(c) Execution-based metrics** - execution accuracy and result-set
  precision/recall/F1/Jaccard, from running the query on a real graph DB and
  comparing result multisets against a Postgres oracle. Needs the databases up.
- **(d) Behavioural and cost/efficiency accounting** - Pass@1 / Pass@k (from offline
  syntax validation), iterations, duration, billed tokens, and USD cost. This is the
  notebook-02 family; the harness bundles validation-based success with cost in
  `metrics_behavioural.csv`, so they are documented together here.

Families (a) and (b) share one preprocessing step, **canonicalisation**, described
first because it governs what those metrics can and cannot see.

## Summary

| Metric | Family | Source function | Module | CSV column | Range | Direction | Needs DB |
|---|---|---|---|---|---|---|---|
| Exact Match | a | `exact_match` | `eval/harness/canonical.py` | `exact_match` | {0,1} | higher = better | no |
| Component F1 | a | `component_f1` | `eval/harness/components.py` | `component_f1_overall`, `f1_*` | [0,1] | higher = better | no |
| Levenshtein | a | `levenshtein` | `eval/harness/distances.py` | `levenshtein` | [0,1] | lower = better | no |
| Jaccard | a | `jaccard` | `eval/harness/distances.py` | `jaccard` | [0,1] | lower = better | no |
| Normalized TED | b | `normalized_ted` | `eval/harness/distances.py` | `normalized_ted` | ~[0,1] | lower = better | no |
| Execution accuracy | c | `compare_rowsets` | `eval/harness/execution.py` | `execution_accuracy` | {0,1} | higher = better | yes |
| Result precision/recall/F1 | c | `compare_rowsets` | `eval/harness/execution.py` | `result_precision`, `result_recall`, `result_f1` | [0,1] | higher = better | yes |
| Result Jaccard distance | c | `compare_rowsets` | `eval/harness/execution.py` | `result_jaccard_dist` | [0,1] | lower = better | yes |
| Pass@1 | d | derived (notebook 02) | `eval/harness/runner.py` fields | `pass_at_1` | {0,1} | higher = better | no |
| Pass@k | d | derived (notebook 02) | `eval/harness/runner.py` fields | `validation_passed` | {0,1} | higher = better | no |
| Billed input tokens | d | `billed_input_tokens` | `eval/harness/pricing.py` | `billed_input_tokens` | ≥0 | - | no |
| USD cost | d | `usd_cost` | `eval/harness/pricing.py` | `cost_usd` | ≥0 | lower = better | no |

Note the direction convention: `levenshtein`, `jaccard`, `normalized_ted`, and
`result_jaccard_dist` are **distances** (0 = identical, higher = more divergent);
everything else in [0,1] is a similarity/accuracy where higher is better. The report
notebook derives a display value `structural_similarity = 1 - normalized_ted` in
`plots.headline_bars` so it can be plotted alongside the higher-is-better metrics.

---

## Canonicalisation and alpha-renaming (shared preprocessing)

**Definition.** A target-aware normal form of a query string, so that two queries
that differ only in incidental surface details compare equal. Implemented by
`canonicalize(query, target)` in `eval/harness/canonical.py`; `exact_match`,
`component_f1`, `levenshtein`, `jaccard`, and `normalized_ted` all consume this
form (directly, or via `tokenize` / `clause_heads_for`).

**What it normalises (and what it deliberately does not).** `canonicalize` performs,
in order:

1. **Comment stripping** - `strip_comments` removes `//`, `--` line comments and
   `/* ... */` block comments.
2. **Alpha-renaming of pattern bindings** - `_binding_names` collects the query's
   binding identifiers per target (Cypher `(x)`/`[x]` pattern variables via
   `_CYPHER_BINDING`; AQL `FOR`/`LET` loop variables via `_AQL_BINDING`; Gremlin
   `.as('x')` step labels via `_GREMLIN_BINDING`), and `alpha_rename` rewrites them
   in first-seen order to `v0, v1, v2, …`. Variable *names* therefore do not affect
   any metric, but the number and reuse pattern of bindings still does.
3. **Keyword upper-casing** - every token is upper-cased iff it is in that target's
   keyword set (`keywords_for`); string literals (leading `'`/`"`) are left verbatim.
   Gremlin's keyword set is empty on purpose: its step names are case-sensitive
   method names (`hasLabel` ≠ `HASLABEL`), so only whitespace and `.as` labels are
   normalised there.

Because the canonical form is `tokenize`'s output re-joined with single spaces,
**whitespace and newlines are normalised** to one space between tokens. What is
**not** normalised: clause order (token order is preserved), property/column names,
literal values (numbers and non-binding strings are kept as-is), and semantic
equivalence of any kind. Two logically equivalent queries with reordered clauses or
different property spellings are *not* canonically equal.

**Edge cases.** `alpha_rename` rewrites every `\b`-delimited occurrence of a binding
name, including inside string literals - so a binding whose name collides with an
unrelated identifier or literal (e.g. Gremlin `as('name')` vs `by('name')`) is
renamed there too. The convention of short binding labels (`a`, `p1`, `po`) keeps
this a non-issue in practice. `_paren_map` is robust to unbalanced parentheses in
invalid model output (a missing close matches end-of-stream instead of raising).

Notebooks 03 and 04 open with an **identity sanity test**: `exact_match(ref, ref)`,
`component_f1(ref, ref)['overall']`, and every distance of a gold query against
itself must be exactly the identity value (True / 1.0 / 0.0) for all gold queries, or
the notebook raises - a guard that canonicalisation has not silently broken.

---

## (a) Exact and structural string metrics

### Exact Match

**Definition.** A binary indicator that the generated query is *string-identical* to
the gold query after canonicalisation:

```
exact_match = 1  iff  canonicalize(translated, target) == canonicalize(expected, target)
```

**What it captures / blind spots.** The strictest structural signal: it credits only
translations that match the gold token-for-token modulo the normalisations above
(comments, whitespace, keyword case, binding names). It is blind to semantic
equivalence - a correct translation that reorders independent clauses, spells a
property differently, or picks a different-but-valid variable *count* scores 0. Exact
Match is therefore a lower bound on correctness and is expected to be low; its value
is as a high-precision "definitely right" signal, not a completeness measure.

**How it's computed here.** `exact_match(translated, expected, target)` in
`eval/harness/canonical.py`. Notebook 03 stores it as the boolean column
`exact_match`; a missing/empty generated query scores `False`.

### Component F1

**Definition.** Structural queries decompose into typed components; Component F1
scores the generated query against the gold *per component*, then averages. The eight
components (from the `Components` dataclass) are: `node_labels`, `edge_types`,
`directions`, `where` tokens, `return` tokens, `order` tokens, `limit` value, and
`aggregations`. For each, an F1 is computed and the `overall` score is their unweighted
mean:

```
component_f1.overall = mean(f1_node_labels, f1_edge_types, f1_directions,
                            f1_where, f1_return, f1_order, f1_limit, f1_aggregations)
```

Per-component F1 uses set overlap (`_set_f1`) - with precision = |t ∩ e| / |t| over
the translated set and recall = |t ∩ e| / |e| over the expected set, then
`F1 = 2PR/(P+R)`. Two special cases: `directions` is an ordered/repeated component
scored with multiset overlap (`_list_f1`, `Counter` intersection over sums), and
`limit` is a scalar scored as exact equality of the `LIMIT` value (1.0 if equal else
0.0). Empty-vs-empty scores 1.0; empty-vs-nonempty (or zero overlap) scores 0.0.

**What it captures / blind spots.** A graceful, partial-credit structural score: it
rewards getting the right labels, edge types, traversal directions, filter/projection
tokens, and aggregations even when the full string differs. Blind spots follow from
the extraction being regex/scan-based rather than a real parse: `where`/`return`/
`order` are **bags of raw canonical tokens**, so any token difference (a renamed
property, an extra keyword) lowers the sub-score without regard to meaning, and the
label/edge heuristics (Cypher TitleCase labels vs SCREAMING_SNAKE edge types) can
misclassify on unconventional casing. It does not check clause order or nesting.

**How it's computed here.** `component_f1(translated, expected, target)` in
`eval/harness/components.py`, dispatching per target via `components_of`:

- **Cypher** (`_components_cypher`): regexes over comment-stripped text pull node
  labels (`:Label`), edge types (`[:EDGE_TYPE]`, then `edge_types -= node_labels` to
  drop TitleCase false hits), directions (`->` / `<-`), and aggregations
  (`count|sum|min|max|avg|collect`). `where`/`return`/`order`/`limit` tokens come from
  `_clause_body` over the *canonicalised* token stream, sliced by clause heads
  (`WHERE`, `RETURN`, `ORDER BY`, `LIMIT`).
- **AQL** (`_components_aql`): `FOR x IN Collection` → node labels; `OUTBOUND/INBOUND/
  ANY <edge>` → edge types + directions; `COUNT|SUM|MIN|MAX|AVERAGE|…` → aggregations;
  `FILTER`/`RETURN`/`SORT`/`LIMIT` clause bodies for the token sets.
- **Gremlin** (`_components_gremlin`): a method chain has no clause heads, so a
  balanced-paren scan (`_paren_map`) attributes each step call's arguments -
  `hasLabel(...)` and 3-arg `has(label,…)` → node labels; `out/in/both/outE/inE/bothE`
  → edge types + directions; filter steps (`has`, `where`, `is`, `and`, `or`, …) →
  where tokens; `project`/`select`/`values`/`valueMap`/… → return tokens; `order` +
  `by(…)` → order tokens; `limit(…)` → limit value; aggregation steps
  (`count`, `sum`, `group`, `fold`, …) → aggregations.

Notebook 03 stores `component_f1_overall` plus the eight `f1_<component>` columns; a
missing generated query scores 0.0 across all of them.

### Levenshtein (normalized token edit distance)

**Definition.** The token-level edit distance between the canonical forms, normalised
to [0,1] by the longer token sequence:

```
levenshtein = editdistance(tokens(translated), tokens(expected)) / max(|t|, |e|)
```

`editdistance` is the standard insertion/deletion/substitution Levenshtein cost,
computed over *tokens* (not characters) by `_levenshtein_tokens`. It is a **distance**
(0 = identical, 1 = maximally different); the identity test requires 0.

**What it captures / blind spots.** A smooth "how many token edits away" signal that,
unlike Exact Match, degrades gracefully. Because it is order-sensitive at token
granularity, it penalises clause reordering heavily even when the reordered query is
equivalent, and it treats every token substitution as equal cost regardless of
semantic weight.

**How it's computed here.** `levenshtein(translated, expected, target)` in
`eval/harness/distances.py`, tokenising via `canonicalize(...).split(" ")`. Empty
denominator → 0.0. Notebook 04 stores it as `levenshtein`; a missing generated query
scores the worst value, 1.0.

### Jaccard (token-set distance)

**Definition.** One minus the Jaccard index of the two canonical *token sets*:

```
jaccard = 1 - |set(t) ∩ set(e)| / |set(t) ∪ set(e)|
```

**Despite the name, the function returns a Jaccard *distance*, not a similarity**
(lower = better, 0 = identical token vocabulary). The token sets are deduplicated, so
this ignores both order and repetition - it measures only which distinct canonical
tokens the two queries share.

**What it captures / blind spots.** A bag-of-tokens vocabulary overlap: robust to
reordering (unlike Levenshtein) but blind to structure, order, token multiplicity,
and - like all the string metrics - semantic equivalence. A query with the right
tokens arranged wrongly scores well here.

**How it's computed here.** `jaccard(translated, expected, target)` in
`eval/harness/distances.py`, over `set(canonicalize(...).split(" "))`. Both-empty or
empty-union → 0.0. Notebook 04 stores it as `jaccard`; a missing generated query
scores 1.0.

---

## (b) Tree-structural metrics

### Normalized tree-edit distance (APTED)

**Definition.** The query is parsed into a small ordered tree per target language;
the APTED tree-edit distance between the generated and gold trees is normalised by the
larger tree's node count:

```
normalized_ted = APTED(tree(translated), tree(expected)) / max(size(t), size(e))
```

Each node-insert, node-delete, and label-rename costs 1 (`_TreeConfig.rename` returns
0 for equal labels else 1; APTED's default insert/delete cost is 1). It is a
**distance** in roughly [0,1] (0 = identical tree; the identity test requires 0).

**What it captures / blind spots.** Unlike the flat string metrics, TED is aware of
tree structure - clause presence and ordering, and (for Gremlin) traversal nesting.
The blind spot is how *shallow* the tree is for the clause-based targets:

- **Cypher / AQL** (`parse_to_clause_tree`): a two-level tree - root `QUERY`, one
  child `ClauseNode` per clause head (label = the head, e.g. `MATCH`, `WHERE`,
  `RETURN`, `FOR`, `FILTER`, `SORT`), and each clause's body tokens as leaf children.
  Tokens before the first head go under a `<prelude>` node. So TED sees clause
  order and per-clause token membership/order, but no deeper structure (property
  access, nested expressions collapse to sibling leaves).
- **Gremlin** (`_gremlin_chain_tree`): the *real* method-chain tree - one node per
  step call (label = step name), that call's arguments as children, and nested calls
  (anonymous `__.` traversals, `P.gt(...)` predicates) recursing into subtrees; chaining
  dots and argument commas are structural separators, not nodes. TED here measures
  genuine step-level structure.

Because the denominator is `max(size)` rather than the sum, the normalised value can
in principle exceed a naive [0,1] intuition on very size-mismatched trees, but in
practice it behaves as a [0,1] structural distance and is treated as such throughout
(the report plots `1 - normalized_ted` as a similarity).

**How it's computed here.** `normalized_ted(translated, expected, target)` in
`eval/harness/distances.py`, using the `apted` library (`APTED(...).compute_edit_distance()`)
with the `_TreeConfig` above; `parse_to_clause_tree` / `_gremlin_chain_tree` build the
trees from the canonical form. Notebook 04 stores it as `normalized_ted`; a missing
generated query scores 1.0.

---

## (c) Execution-based metrics

This family is the semantic ground truth: it *runs* the generated query on the target
graph DB and the gold SQL on Postgres (the oracle), then compares the two result sets.
The reusable pieces, the per-backend executors and the comparison core `compare_rowsets`,
live in `eval/harness/execution.py`; notebook 05 builds its per-record execute-and-compare
loop (`execute_records`) on top of them, so that flow is visible in the notebook.

**The oracle and the executors.** For each runnable record, `run_postgres` executes
the gold SQL (its rows are cached per query in `execution_rows_cache.json`, so only
the first target pays the Postgres cost). The generated graph query runs through the
per-target runner in `RUNNERS`: `run_cypher` (Neo4j), `run_aql` (ArangoDB db
`graphonauts`), or `run_gremlin` (TinkerGraph, run on a fresh daemon thread with a
client-side timeout so a wedged websocket becomes a recorded error, not a hang). Each
executor returns `(rows, runtime_seconds, error_or_None)` and enforces a server-side
`TIMEOUT_S` (`EVAL_QUERY_TIMEOUT`, default 180 s in the recorded run). A record scores
all-zero **without touching the backend** if it failed offline validation, has no
generated query, or the oracle itself errored; a translation that errors at execution
time scores all-zero with the error recorded in `execution_error`.

### Execution accuracy

**Definition.** A binary indicator that the generated query's result set equals the
oracle's, compared as **multisets** (bags) of normalised rows:

```
execution_accuracy = 1  iff  Counter(norm rows of translated) == Counter(norm rows of reference)
```

**What it captures / blind spots.** The strongest correctness signal available: it is
insensitive to query syntax and rewards any query that produces exactly the right
rows. Two properties of the multiset comparison matter:

- **Order-insensitive, multiplicity-sensitive.** Rows are compared as a `Counter`, so
  row *ordering* is ignored - `ORDER BY` correctness is *not* enforced by execution
  accuracy - but duplicate counts must match (bag, not set, semantics).
- **Positional column comparison.** Rows are compared by column position, so gold and
  generated projections must list columns in the same order (the gold set aligns
  `RETURN` column order to the SQL `SELECT` order).

**How it's computed here.** `compare_rowsets(ref_rows, trans_rows, empty_as_null)`
returns `execution_accuracy = 1.0 if ref == trans else 0.0` over the two `Counter`s of
`norm_row` tuples.

#### Row normalisation and the date-reconciling comparator

Before comparison every row passes through `norm_row` → `norm_value`, which reconciles
the very different value representations each backend returns:

- **Dates.** Temporal cells fold *per value* to epoch-milliseconds via `to_epoch_ms`, so
  every backend's representation of an instant compares equal: Postgres timestamps, Neo4j
  native temporals (`neo4j.time.Date`/`DateTime` → stdlib via `to_native`), and ArangoDB/
  Gremlin ISO-8601 strings (parsed by `parse_iso`, tz-naive treated as UTC). Any cell
  that is a native/driver temporal, or a string matching a full ISO date/datetime,
  reconciles; genuine integers and free text pass through untouched. This is what lets a
  Cypher `datetime(...)` predicate and an AQL string `>= '2010-06-01'` compare equal to
  the same Postgres date. (The graphonauts datetime unification guarantees strict,
  UTC-consistent ISO/native forms across the stores, so this per-value fold replaced an
  earlier per-column oracle that scanned the Postgres rows to decide which columns were
  dates.)
- **Empty-as-null.** For AQL and Gremlin (`EMPTY_AS_NULL_TARGETS`), `norm_value` maps
  `'' → None`: ArangoDB stores absent optional text as `''`, and the gold Gremlin
  projects NULLs as `''` via `coalesce(values(x), constant(''))`; both mean Postgres
  NULL.
- **Other types.** Booleans → `"True"`/`"False"`; ints → `str`; floats → integer
  string when within 1e-9 of an integer else 6-decimal. `None` stays `None`.

### Result precision / recall / F1

**Definition.** Multiset overlap statistics between generated (`trans`) and oracle
(`ref`) rows, letting a *partially* correct result set score above zero where binary
accuracy would be 0:

```
overlap   = sum((ref & trans) counts)     # multiset intersection
precision = overlap / n_trans
recall    = overlap / n_ref
result_f1 = 2 * precision * recall / (precision + recall)
```

**What it captures / blind spots.** Graded execution correctness - how much of the
right answer is present (recall) and how much of the produced answer is right
(precision). Empty-set edge cases are handled explicitly: with no translated rows,
precision is 1.0 iff the reference is also empty else 0.0; with no reference rows,
recall is 1.0 iff the translated set is empty else 0.0; F1 is 0.0 when precision +
recall is 0. Same order/positional caveats as execution accuracy. Note `result_f1 = 1.0`
coincides with `execution_accuracy = 1.0` (both require multiset equality).

**How it's computed here.** `compare_rowsets` in `eval/harness/execution.py`, stored by
notebook 05 as `result_precision`, `result_recall`, `result_f1` (alongside
`reference_rows` / `translated_rows` counts and `reference_runtime_s` /
`translated_runtime_s`).

### Result Jaccard distance

**Definition.** One minus the multiset Jaccard index of the two result sets:

```
result_jaccard_dist = 1 - overlap / union      # union = sum((ref | trans) counts)
```

A **distance** (lower = better; 0 when the multisets are identical, 1 when they are
disjoint; 0.0 when both are empty). Computed alongside the P/R/F1 in `compare_rowsets`
and stored as `result_jaccard_dist` (invalid/errored records default to 1.0).

---

## (d) Behavioural and cost/efficiency accounting

This family measures the *process*, not the query text: whether the translator
produced a valid query and at what token/dollar cost. It is assembled in notebook 02
from the fields the runner records per attempt (`AttemptRecord` in
`eval/harness/runner.py`) and written to `metrics_behavioural.csv`. Note that
"validation" here means **offline syntax validation** (the in-process ANTLR grammar
validator selected per target, default mode `"syntax"`), *not* execution - a query can
pass validation and still be semantically wrong.

### Pass@1 and Pass@k

**Definition.** The translator runs a generate → validate → fix loop up to
`RunConfig.max_iterations` (default 3); `iterations_used` is the number of validate
calls performed and `validation_passed` is whether the final query passed. From these:

```
pass_at_1 (per record)  = validation_passed AND iterations_used == 1
Pass@1    (aggregate)   = mean(pass_at_1)        over a group
Pass@k    (aggregate)   = mean(validation_passed) over a group,  k = max iterations_used
```

**The `k` here is the self-repair iteration budget, not independent samples.** Because
the run uses `temperature = 0.0` and a single deterministic sample per query, `k` does
*not* mean "k sampled generations" (the classic Pass@k). Instead Pass@1 is the fraction
of queries that validated on the *first* attempt with no repair, and Pass@k is the
fraction that validated *at all* within the up-to-`k` self-repair iterations - so
Pass@k ≥ Pass@1, and their gap is exactly the queries the fix loop rescued. Notebook 02
labels the second column `pass@{MAX_ITERATIONS}` using the maximum `iterations_used`
observed across the loaded records.

**What it captures / blind spots.** A syntactic-validity success rate and a measure of
how much the self-repair loop helps. Its blind spot is that it certifies *validity*,
not *correctness*: it cannot distinguish a valid-but-wrong translation from a right
one (that is what the structural, distance, and execution families are for).

**How it's computed here.** `validation_passed`, `iterations_used` are recorded by
`translate_one` (driven by notebook 01's run loop) from the library's `TranslationResult`;
`pass_at_1` and the Pass@k aggregate are derived in notebook 02. Stored columns:
`validation_passed`, `pass_at_1`.

### Token usage (billed tokens)

**Definition.** The prompt/response token counts for the translation, taken first-class
from the library's `TokenUsage` (see [../docs/api.md](../docs/api.md); type defined in
`src/sql2graph/llm/usage.py`). `TokenUsage` carries `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_creation_tokens`, and a computed `total_tokens` (the sum of
all four). For Anthropic the translator caches the whole system prompt
(`cache_control: ephemeral`), so `input_tokens` is only the *uncached* prompt delta and
the bulk lands in the two cache fields; both cache fields are always 0 for Ollama. The
harness therefore reports **billed input tokens** as the full prompt size:

```
billed_input_tokens = input_tokens + cache_read_tokens + cache_creation_tokens
```

matching what platform.claude.com shows as "tokens in".

**How it's computed here.** `billed_input_tokens(...)` in `eval/harness/pricing.py`;
the raw four fields come straight from `result.token_usage` via `AttemptRecord`.
Notebook 02 stores `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens`, and the derived `billed_input_tokens`.

### USD cost

**Definition.** The dollar cost of one call (or a summed set), pricing all four token
buckets at per-million-token rates:

```
cost_usd = ( input_tokens          * pin
           + cache_creation_tokens * pin * CACHE_WRITE_MULT   (1.25)
           + cache_read_tokens     * pin * CACHE_READ_MULT    (0.10)
           + output_tokens         * pout ) / 1e6
```

Cache *writes* are billed at 1.25× the input rate (the 5-minute ephemeral TTL the
translator uses) and cache *reads* at 0.10×; output uses its own rate. `(pin, pout)`
comes from `rate_for(provider, model)`: the exact model in `MODEL_PRICING`
(e.g. `claude-opus-4-8` → `(5.0, 25.0)` USD/Mtok), else the provider default in
`PROVIDER_DEFAULT_PRICING` (`ollama → (0.0, 0.0)` - local, free), else `(0.0, 0.0)`.
For Ollama every rate is 0, so the cache multipliers are moot.

**What it captures / blind spots.** Real billed cost, cache-aware, so it matches the
provider's dashboard rather than a naive `input+output` estimate. It is only as
accurate as the rate table: a new Anthropic model must be added to `MODEL_PRICING`
(a test enforces this) or it silently falls back to the Opus-class provider default.

**How it's computed here.** `usd_cost(...)` in `eval/harness/pricing.py`; notebook 02
applies it per record as `cost_usd` and derives a `cost_per_success_usd` aggregate
(total cost / number of validated queries). `iterations_used` and `duration_seconds`
(wall-clock, from `TranslationResult`) round out the efficiency picture.

---

## Which metric to trust for what

- **Execution accuracy is the semantic ground truth** - it is the only metric that
  answers "does the query return the right rows?" Prefer it whenever the databases are
  up. Its caveats: it needs the loaded graph DBs plus the Postgres oracle, it is
  order-insensitive (so it does not verify `ORDER BY`), and it is positional (columns
  must line up). Read `result_f1` / `result_precision` / `result_recall` next to it to
  see *how* a near-miss missed.
- **TED, Component F1, and the string distances are DB-free structural proxies** - use
  them for fast, always-available signal and for diagnosing *where* a translation
  diverges (Component F1 pinpoints the wrong component; TED flags clause/step-structure
  errors; Levenshtein/Jaccard give smooth surface similarity). They cannot see
  semantic equivalence, so a structurally different but correct query will score low -
  they under-credit, they do not over-credit.
- **Exact Match is a high-precision floor** - a 1 is definitely right, but a 0 says
  little; never read it as an error rate.
- **Pass@k measures validity, not correctness** - a high Pass@k with low execution
  accuracy means the model writes syntactically clean queries that compute the wrong
  thing. Read it together with cost (`cost_usd`, `billed_input_tokens`, iterations) to
  weigh a model's success against what it spent to get there.
