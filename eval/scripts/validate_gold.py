"""Validate the gold set: does each gold graph query return the same data as its gold SQL?

For every query in eval/gold/<dataset>.yaml this runs the `sql` on graphonauts2's
Postgres (the oracle) and the target's expected_<target> gold query on its graph DB
(cypher -> Neo4j, aql -> ArangoDB, gremlin -> Gremlin Server), then compares the
result sets as date-reconciling multisets via harness.execution - the same comparator
notebook 05 uses, so the gold proof and the model scoring can never drift.

This checks the gold set itself - it is independent of any model. Expect every
non-vacuous pair to genuinely match (exit code 1 on any MISMATCH or error).

Credentials come from config/servers/*.yaml (via harness.execution); a local run needs no
exported passwords, though NEO4J_PASSWORD / ARANGO_PASSWORD / POSTGRES_* still override per field.

Per-target prerequisites:
  cypher   Neo4j up with LDBC SF1 loaded.
  aql      ArangoDB up (db `graphonauts`) with the unified SCREAMING_SNAKE edge
           collections from examples/mappings/<dataset>.yaml built first:
               uv run python eval/scripts/build_arango_unified_edges.py
  gremlin  Gremlin Server up and LOADED (in-memory TinkerGraph; reload after any
           container restart, with Neo4j/ArangoDB stopped - the Docker VM is
           memory-tight). From graphonauts2:
               uv run graphonauts load gremlin && uv run graphonauts verify gremlin

Run:
    uv run python eval/scripts/validate_gold.py --target cypher
    uv run python eval/scripts/validate_gold.py --target aql
    uv run python eval/scripts/validate_gold.py --target gremlin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "eval"))

from harness import load_dataset
from harness.execution import (
    EMPTY_AS_NULL_TARGETS,
    RUNNERS,
    close_clients,
    compare_rowsets,
    date_columns,
    run_postgres,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prove the gold SQL and gold graph queries agree.")
    parser.add_argument("--target", required=True, choices=("cypher", "aql", "gremlin"))
    parser.add_argument("--dataset", default="ldbc")
    args = parser.parse_args()
    target = args.target

    # Credentials default from config/servers/*.yaml (env still overrides); a genuine
    # connection failure surfaces per-query as an execution error below, not up front.
    rows_col = f"{target}_rows"
    rows = []
    try:
        for q in load_dataset(args.dataset):
            gold = q.expected.get(target)
            if not gold:
                continue
            sql_rows, _, sql_err = run_postgres(q.sql)
            trans_rows, _, trans_err = RUNNERS[target](gold)
            cmp = compare_rowsets(
                sql_rows, trans_rows, date_columns(sql_rows), empty_as_null=target in EMPTY_AS_NULL_TARGETS
            )
            match = cmp["execution_accuracy"] == 1.0
            err = sql_err or trans_err
            if err:
                note = "ERROR"
            elif match and cmp["reference_rows"] == 0:
                note = "vacuous (0 rows both sides)"
            elif match:
                note = "ok"
            else:
                note = "MISMATCH"
            rows.append({
                "query_id": q.id,
                "difficulty": q.difficulty,
                "match": match,
                "result_f1": cmp["result_f1"],
                "sql_rows": cmp["reference_rows"],
                rows_col: cmp["translated_rows"],
                "error": err,
                "note": note,
            })
    finally:
        close_clients()

    hdr = f"{'query':10} {'diff':7} {'match':6} {'sql_rows':>9} {rows_col:>12} {'F1':>5}  note"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        line = (
            f"{r['query_id']:10} {r['difficulty']:7} {str(r['match']):6} "
            f"{r['sql_rows']:>9} {r[rows_col]:>12} {r['result_f1']:>5.2f}  {r['note']}"
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
        print(f"  MISMATCH (gold SQL and gold {target} disagree):", [r["query_id"] for r in mismatch])
    if vacuous:
        print("  vacuous (oracle returns 0 rows; not a real test):", [r["query_id"] for r in vacuous])
    if errored:
        print("  errored:", [r["query_id"] for r in errored])

    sys.exit(1 if (mismatch or errored) else 0)


if __name__ == "__main__":
    main()
