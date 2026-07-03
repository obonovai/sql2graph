"""Validate the LDBC gold set: does each gold Gremlin return the same data as its gold SQL?

For every query in evaluation/datasets/ldbc.yaml this runs the `sql` on graphonauts2's
Postgres and the `expected_gremlin` on graphonauts2's Gremlin Server (in-memory TinkerGraph),
then compares the result sets as multisets. It is the Gremlin analogue of
validate_gold_aql.py (SQL vs AQL) and validate_gold_queries.py (SQL vs Cypher).

The comparator is imported from validate_gold_aql so the semantics cannot drift: rows are
compared positionally (project() maps arrive insertion-ordered, matching SQL SELECT order),
date columns reconcile to epoch-millis (the graph stores ISO-8601 strings), and '' == NULL
(gold traversals project nullable columns via coalesce(values(x), constant(''))).

Prerequisites (see graphonauts2 docs/gremlin/LOADING_BRIEF.md): the gremlin-graphonaut
container must be up and LOADED -- TinkerGraph is in-memory, so after any container restart
re-run, from graphonauts2:
    uv run graphonauts load gremlin && uv run graphonauts verify gremlin
(run with neo4j-graphonaut and arangodb-graphonaut stopped; the Docker VM is memory-tight).

This checks the gold set itself - it is independent of any model.

Run:
    set -a; source .env; set +a
    POSTGRES_PASSWORD=password uv run python evaluation/validate_gold_gremlin.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "eval"))

from gremlin_python.driver.client import Client
from harness import load_dataset
from validate_gold import TIMEOUT_S, _date_columns, compare_rowsets, run_postgres

GREMLIN_URL = os.environ.get("GREMLIN_URL", "ws://localhost:8182/gremlin")
GREMLIN_TRAVERSAL_SOURCE = os.environ.get("GREMLIN_TRAVERSAL_SOURCE", "g")


def run_gremlin(client, query):
    try:
        result_set = client.submit(query, request_options={"evaluationTimeout": TIMEOUT_S * 1000})
        return list(result_set.all().result()), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def main():
    client = Client(GREMLIN_URL, GREMLIN_TRAVERSAL_SOURCE)

    rows = []
    try:
        for q in load_dataset("ldbc"):
            gremlin = q.expected.get("gremlin")
            if not gremlin:
                continue
            sql_rows, sql_err = run_postgres(q.sql)
            gremlin_rows, gremlin_err = run_gremlin(client, gremlin)
            cmp = compare_rowsets(sql_rows, gremlin_rows, _date_columns(sql_rows))
            # compare_rowsets labels the translated side 'aql_rows'; rename for this report.
            cmp["gremlin_rows"] = cmp.pop("aql_rows")
            err = sql_err or gremlin_err
            if err:
                note = "ERROR"
            elif cmp["match"] and cmp["sql_rows"] == 0:
                note = "vacuous (0 rows both sides)"
            elif cmp["match"]:
                note = "ok"
            else:
                note = "MISMATCH"
            rows.append({"query_id": q.id, "difficulty": q.difficulty, **cmp, "error": err, "note": note})
    finally:
        client.close()

    hdr = f"{'query':10} {'diff':7} {'match':6} {'sql_rows':>9} {'gremlin_rows':>12} {'F1':>5}  note"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        line = (
            f"{r['query_id']:10} {r['difficulty']:7} {str(r['match']):6} "
            f"{r['sql_rows']:>9} {r['gremlin_rows']:>12} {r['result_f1']:>5.2f}  {r['note']}"
        )
        if r["error"]:
            line += f"  [{r['error'][:70]}]"
        print(line)

    genuine = [r for r in rows if r["match"] and r["sql_rows"] > 0 and not r["error"]]
    vacuous = [r for r in rows if r["match"] and r["sql_rows"] == 0 and not r["error"]]
    mismatch = [r for r in rows if not r["match"] and not r["error"]]
    errored = [r for r in rows if r["error"]]
    print()
    print(
        f"Summary: {len(rows)} gold pairs | genuine match: {len(genuine)} | "
        f"vacuous (0 rows): {len(vacuous)} | MISMATCH: {len(mismatch)} | error: {len(errored)}"
    )
    if mismatch:
        print("  MISMATCH (gold SQL and gold Gremlin disagree):", [r["query_id"] for r in mismatch])
    if vacuous:
        print("  vacuous (oracle returns 0 rows; not a real test):", [r["query_id"] for r in vacuous])
    if errored:
        print("  errored:", [r["query_id"] for r in errored])

    sys.exit(1 if (mismatch or errored) else 0)


if __name__ == "__main__":
    main()
