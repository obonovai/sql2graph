# Evaluation harness

Measures how well `rows2graph`'s LLM-driven SQL -> graph translator performs, across a
matrix of **dataset x target language x model**. The reusable run/record/metric logic
lives in the `eval_harness` package; the notebooks are the analysis surface.

The first deliverable runs **DB-free**: Cypher only, Ollama `qwen3-coder:30b` only, LDBC
only. It produces Pass@k (via Cypher syntax validation), structural (Exact Match /
Component F1), and distance (Levenshtein / Jaccard / normalised tree-edit) metrics plus a
report - with no databases. Execution-accuracy metrics are scaffolded but deferred (they
need the graphonauts2 databases; see below).

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
│   ├── 05_execution_metrics.ipynb    ← DEFERRED: result-set metrics vs Postgres/Neo4j
│   └── 06_report.ipynb               ← join + stratify + final markdown report
├── outputs/                  ← gitignored; records_*.json + metrics_*.csv
└── reports/                  ← gitignored; final.md + figures
```

## Install

```bash
uv sync --extra eval
```

Pulls `jupyter`, `pandas`, `matplotlib`, `psycopg`, `apted`, `tabulate` on top of the
library's runtime deps.

## The run matrix

Everything is driven by `RUN_MATRIX` in `eval_harness/config.py`. The default is one cell:

```python
RUN_MATRIX = [RunConfig(dataset="ldbc", target="cypher", model="qwen3-coder:30b", provider="ollama")]
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
- `reports/final.md` + `reports/figures/`: headline + stratified tables, Pass@k bars,
  distance distributions, Component-F1 heatmap, and a manual error-taxonomy template.

## Execution metrics (deferred, prepared)

Notebook `05` runs the generated query on a real graph DB and the gold SQL on Postgres
(the oracle), comparing result multisets. It is gated behind `EVAL_EXECUTION=1` and needs
the graphonauts2 databases, loaded with LDBC SNB SF=1:

```bash
docker compose -f /Users/ivona.obonova/school/graphonauts2/docker/postgres.compose.yml up -d
docker compose -f /Users/ivona.obonova/school/graphonauts2/docker/neo4j.compose.yml up -d
# ArangoDB only when an AQL row is added:
# docker compose -f /Users/ivona.obonova/school/graphonauts2/docker/arangodb.compose.yml up -d
```

Then load LDBC SF=1 into each (see `graphonauts2/notes/commands.md`), export
`NEO4J_PASSWORD` and `EVAL_EXECUTION=1`, and run `05`.

**Datetime storage (former defect D7, resolved):** graphonauts2 now loads
`creationDate`/`birthday`/`joinDate` into Neo4j as native temporal types (`DateTime`/`Date`),
so the gold Cypher `datetime('...')` / `date('...')` predicates match directly. Reload Neo4j
after any loader change before trusting execution results.

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
