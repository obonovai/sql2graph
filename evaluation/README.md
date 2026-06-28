# Evaluation harness

A notebook-based evaluation pipeline that measures how well `rows2graph`'s LLM-driven SQL → Cypher / AQL translator preserves semantics across the gold-standard TPC-H and LDBC datasets.

## Layout

```
evaluation/
├── README.md                 ← this file
├── datasets/
│   ├── tpch.yaml             ← TPC-H gold-standard SQL / Cypher / AQL triples
│   └── ldbc.yaml             ← LDBC SNB ditto (aligned to graphonauts2's clean schema)
├── notebooks/
│   ├── 00_setup.ipynb            ← verify env + DB connectivity, summarise dataset
│   ├── 01_translation_run.ipynb  ← drive SQLTranslator, save records.json
│   ├── 02_behavioural_metrics.ipynb  ← Pass@1/k, iterations, tokens, cost
│   ├── 03_structural_metrics.ipynb   ← Exact Match, Component F1
│   ├── 04_distance_metrics.ipynb     ← Levenshtein, Jaccard, normalised TED
│   ├── 05_execution_metrics.ipynb    ← Postgres / Neo4j / ArangoDB result-set metrics
│   └── 06_report.ipynb               ← aggregate + stratify + final markdown report
├── outputs/                  ← gitignored; per-notebook intermediate artifacts
└── reports/                  ← gitignored; final markdown + plots
```

## Install

```bash
uv sync --extra eval
```

This pulls in `jupyter`, `pandas`, `matplotlib`, `psycopg`, `apted`, and `tabulate` on top of the library's runtime deps (`anthropic`, `neo4j`, `python-arango`).

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `01_translation_run.ipynb` | Direct-API key for Claude. |
| `NEO4J_PASSWORD` | `00_setup`, `05_execution_metrics` | Bolt password for the local Neo4j container. |
| `ARANGO_PASSWORD` | `00_setup`, `05_execution_metrics` | HTTP password for the local ArangoDB container. |
| `POSTGRES_PASSWORD` | `00_setup`, `05_execution_metrics` | Defaults to `password` (matches graphonauts2's compose). |

Export each in your shell before launching Jupyter.

## Bring up the databases

The evaluation reads SQL from graphonauts2's Postgres and runs translated graph queries against Neo4j and ArangoDB. Bring up all three from `/Users/ivona.obonova/school` (which is the parent of both repos):

```bash
docker compose -f graphonauts2/docker/postgres.compose.yml up -d
docker compose -f graphonauts2/docker/neo4j.compose.yml up -d
docker compose -f graphonauts2/docker/arangodb.compose.yml up -d
```

Then ensure LDBC SNB SF=1 data is loaded into each. See `graphonauts2/notes/commands.md` for the canonical load commands.

`00_setup.ipynb` asserts every DB is reachable and contains LDBC data before downstream notebooks run.

## Running

Notebooks are numbered so `jupyter nbconvert --to notebook --execute --inplace evaluation/notebooks/0*.ipynb` runs them in sequence. Or, interactively:

```bash
uv run jupyter lab evaluation/notebooks/
```

**Run `00` and `01` first.** `01` does the expensive LLM calls (one per gold-query × dialect) and caches results in `evaluation/outputs/records.json`. `02`-`05` consume that file. `06` joins everything into the final report.

Notebook `05` is the only one that needs all three databases up; `02`-`04` are DB-free.

## Outputs

After a full run you'll find:

- `evaluation/outputs/records.json`: every translation attempt with its TranslationResult fields plus scraped Anthropic token counts.
- `evaluation/outputs/metrics_*.csv`: per-record metric values from each `0[2-5]` notebook.
- `evaluation/reports/final.md`: headline + stratified tables, Pass@k curves, error-taxonomy template.
- `evaluation/reports/figures/`: plots referenced from the markdown report.

## Scope

- **LDBC** has full execution-based metrics (SQL on Postgres ↔ Cypher on Neo4j ↔ AQL on ArangoDB).
- **TPC-H** gets static-only metrics (structural, distance, behavioural). Execution metrics for TPC-H are future work since graphonauts2 doesn't ship a TPC-H Postgres.
