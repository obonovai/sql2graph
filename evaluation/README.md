# Evaluation harness

Measures how well `rows2graph`'s LLM-driven SQL -> graph translator performs, across a
matrix of **dataset x target language x model**. The reusable run/record/metric logic
lives in the `eval_harness` package; the notebooks are the analysis surface.

The DB-free metrics (notebooks 01-04, 06) run with no databases: Pass@k (via offline syntax
validation), structural (Exact Match / Component F1), and distance (Levenshtein / Jaccard /
normalised tree-edit), plus a report. The matrix currently covers **LDBC x {Cypher, AQL} x
4 models** (`llama3.2:latest`, `qwen3-coder:30b`, `gemma4:26b` on Ollama + `claude-opus-4-8`
on Anthropic). Results are reported per target and never mixed. Execution-accuracy metrics
(notebook 05) run the generated query on a real graph DB; they are deferred behind
`EVAL_EXECUTION=1` and need the graphonauts2 databases (see below).

## Layout

```
evaluation/
├── README.md
├── eval_harness/             ← reusable package the notebooks import
│   ├── config.py             ← RunConfig, RUN_MATRIX, validation-mode routing
│   ├── datasets.py           ← gold-dataset loading + work-item construction
│   ├── runner.py             ← run_translation, AttemptRecord, records IO
│   └── canonical.py          ← tokenise/canonicalise/components + distances (shared by 03/04)
├── datasets/
│   ├── ldbc.yaml             ← curated LDBC SNB gold set (SQL + expected_cypher/_aql)
│   └── tpch.yaml             ← TPC-H gold set (future dataset extension)
├── notebooks/
│   ├── 00_setup.ipynb            ← Ollama liveness (hard) + DB checks (warn) + dataset summary
│   ├── 01_translation_run.ipynb  ← drive the matrix, write records_<dataset>_<target>_<model>.json
│   ├── 02_behavioural_metrics.ipynb  ← Pass@1/k, iterations, duration, tokens, cost
│   ├── 03_structural_metrics.ipynb   ← Exact Match, Component F1
│   ├── 04_distance_metrics.ipynb     ← Levenshtein, Jaccard, normalised TED
│   ├── 05_execution_metrics.ipynb    ← DEFERRED: result-set metrics vs Postgres + Neo4j/ArangoDB
│   └── 06_report.ipynb               ← join + stratify + final markdown report (per-target sections)
├── validate_gold_queries.py    ← prove gold SQL == gold Cypher on Postgres/Neo4j
├── validate_gold_aql.py        ← prove gold SQL == gold AQL on Postgres/ArangoDB
├── build_arango_unified_edges.py ← build the mapping-aligned unified AQL edge collections
├── outputs/                  ← gitignored; records_*.json + metrics_*.csv
└── reports/                  ← gitignored; final.md + figures (cypher_*.png / aql_*.png)
```

## Install

```bash
uv sync --extra eval
```

Pulls `jupyter`, `pandas`, `matplotlib`, `psycopg`, `apted`, `tabulate` on top of the
library's runtime deps.

## The run matrix

Everything is driven by `RUN_MATRIX` in `eval_harness/config.py`. It currently holds 8 cells
(LDBC x {cypher, aql} x 4 models), e.g.:

```python
RUN_MATRIX = [
    RunConfig(dataset="ldbc", target="cypher", model="qwen3-coder:30b", provider="ollama"),
    RunConfig(dataset="ldbc", target="aql",    model="qwen3-coder:30b", provider="ollama"),
    # ... the other models, per target ...
]
```

Extending the evaluation is appending rows (and adding gold columns / mappings), not editing
notebooks. Records and metrics auto-stratify by `(dataset, target, model)`:

| To add | Do |
|---|---|
| another Ollama / Anthropic model | append `RunConfig(model=..., provider=...)`; for Anthropic, export `ANTHROPIC_API_KEY` and add per-model pricing in notebook 02 |
| AQL or Gremlin target | add `expected_aql` / `expected_gremlin` to the gold YAML and append `RunConfig(target=...)`. All three targets default to deployment-free `syntax` validation (AQL via a hand-ported ArangoDB grammar); pass an override for `server`/`managed` |
| TPC-H dataset | flesh out `datasets/tpch.yaml`, rely on `config/mappings/tpch.yaml`, append `RunConfig(dataset="tpch")` |

## Running the DB-free first pass

```bash
# Ollama up with the model pulled (the library talks to :11434)
ollama serve
ollama pull qwen3-coder:30b
```

Smoke-test one query first by uncommenting the `subset=("ldbc_q01",)` line in
`01_translation_run.ipynb`, then run the full pass:

```bash
export MPLBACKEND=Agg
uv run jupyter nbconvert --to notebook --execute --inplace evaluation/notebooks/00_setup.ipynb
uv run jupyter nbconvert --to notebook --execute --inplace evaluation/notebooks/01_translation_run.ipynb
uv run jupyter nbconvert --to notebook --execute --inplace evaluation/notebooks/0{2,3,4,6}_*.ipynb
```

or interactively: `uv run jupyter lab evaluation/notebooks/`.

`01` is the only notebook that calls the LLM; it writes `records_*.json` incrementally and
resumes (query ids already on disk are skipped). `02`-`04` and `06` are DB-free and consume
those records. Notebook `05` is **deferred** and excluded from the pass.

## Outputs

- `outputs/records_<dataset>_<target>_<model>.json`: every attempt with its TranslationResult
  fields and `token_usage` (reported first-class by the library; no log scraping).
- `outputs/metrics_{behavioural,structural,distance}.csv`: per-record metrics, keyed by
  `(dataset, target, model, query_id, difficulty)`.
- `reports/final.md` + `reports/figures/`: a dedicated section per target (`SQL -> Cypher`,
  `SQL -> AQL`) with headline + stratified tables, Pass@k bars, distance distributions,
  Component-F1 heatmap, and a manual error-taxonomy template. Cypher and AQL are never
  combined in one table or figure; figures are target-prefixed (`cypher_*` / `aql_*`).

## Execution metrics (deferred, prepared)

Notebook `05` runs the generated query on a real graph DB and the gold SQL on Postgres
(the oracle), comparing result multisets. Cypher runs on Neo4j, **AQL on ArangoDB** (db
`graphonauts`). It is gated behind `EVAL_EXECUTION=1` and needs the graphonauts2 databases,
loaded with LDBC SNB SF=1:

```bash
docker compose -f /Users/ivona.obonova/school/graphonauts2/docker/postgres.compose.yml up -d
docker compose -f /Users/ivona.obonova/school/graphonauts2/docker/neo4j.compose.yml up -d
docker compose -f /Users/ivona.obonova/school/graphonauts2/docker/arangodb.compose.yml up -d
```

Load LDBC SF=1 into each (see `graphonauts2/notes/commands.md`; the ArangoDB loader
populates db `graphonauts`), then wire the AQL execution collections and validate the gold
set before trusting any model numbers:

```bash
# 1. Build the mapping-aligned unified edge collections the gold + generated AQL reference
#    (KNOWS, HAS_CREATOR, HAS_TAG, ...). Re-run after every ArangoDB (re)load -- mandatory.
ARANGO_PASSWORD=password uv run python evaluation/build_arango_unified_edges.py

# 2. Prove the gold AQL matches the Postgres oracle (SQL vs AQL multiset compare). Expect
#    14 genuine matches, 0 MISMATCH/error before running any model.
POSTGRES_PASSWORD=password ARANGO_PASSWORD=password \
  uv run python evaluation/validate_gold_aql.py

# 3. Run notebook 05 (both targets -> Neo4j + ArangoDB + Postgres must be up).
EVAL_EXECUTION=1 NEO4J_PASSWORD=... ARANGO_PASSWORD=password POSTGRES_PASSWORD=password \
  ARANGO_DATABASE=graphonauts \
  uv run jupyter nbconvert --to notebook --execute --inplace evaluation/notebooks/05_execution_metrics.ipynb
```

Passwords are asserted lazily per target, so a Cypher-only or AQL-only subset only needs
that backend up.

**Datetime storage:** graphonauts2 loads `creationDate`/`birthday`/`joinDate` into Neo4j as
native temporal types (`DateTime`/`Date`) and into ArangoDB as ISO-8601 strings
(`DATE_ISO8601`-compatible). The comparator canonicalises the columns the Postgres oracle
reports as dates to epoch-millis on all sides, so gold `datetime(...)` (Cypher) and string
`>= '2010-06-01'` (AQL) predicates both match. Reload the graph DB after any loader change
before trusting execution results.

## Gold dataset notes

`datasets/ldbc.yaml` is a curated mix of hand-authored translation-difficulty queries and
graphonauts2's validated set, aligned to `config/mappings/ldbc.yaml`. Conventions:

- **KNOWS is directed** (`-[:KNOWS]->`), matching the directed `friend_id` SQL joins and the
  directed `knows` edge actually loaded in graphonauts2 (defect D2).
- Graph properties are camelCase, SQL columns snake_case; multi-valued `person_email` /
  `person_speaks` are excluded (the mapping does not express list properties, defect D4).
- RETURN column order is aligned to the SQL SELECT order (the execution comparator in 05 is
  positional).

TPC-H stays static-only until a TPC-H Postgres oracle exists (graphonauts2 ships none).
