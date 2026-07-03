"""Validate the LDBC gold set: does each gold Cypher return the same data as its gold SQL?

For every query in evaluation/datasets/ldbc.yaml this runs the `sql` on graphonauts2's
Postgres and the `expected_cypher` on graphonauts2's Neo4j, then compares the result sets
as multisets (with the same epoch-millis date reconciliation as notebook 05, so a date
*column* in the output compares correctly while a date *predicate* mismatch still surfaces).

This checks the gold set itself - it is independent of any model.

Run:
    set -a; source .env; set +a
    NEO4J_PASSWORD=password POSTGRES_PASSWORD=password uv run python evaluation/validate_gold_queries.py
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "eval"))

import psycopg
from harness import load_dataset
from neo4j import GraphDatabase, Query
from neo4j.time import Date as Neo4jDate
from neo4j.time import DateTime as Neo4jDateTime

# --- connection config (mirrors notebook 05) ---
PG_DSN = (
    f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
    f"port={os.environ.get('POSTGRES_PORT', '5433')} "
    f"user={os.environ.get('POSTGRES_USER', 'graphonaut')} "
    f"password={os.environ.get('POSTGRES_PASSWORD', 'password')} "
    f"dbname={os.environ.get('POSTGRES_DB', 'graphonaut')}"
)
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_DB = os.environ.get("NEO4J_DATABASE", "neo4j")
TIMEOUT_S = int(os.environ.get("EVAL_QUERY_TIMEOUT", "60"))


# --- date-reconciling multiset comparator (copied from notebook 05) ---
def _to_epoch_ms(v):
    if isinstance(v, (Neo4jDate, Neo4jDateTime)):
        v = v.to_native()  # Neo4j temporal -> stdlib date/datetime, handled below
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_dt.UTC)
        return int(v.timestamp() * 1000)
    if isinstance(v, _dt.date):
        return int(_dt.datetime(v.year, v.month, v.day, tzinfo=_dt.UTC).timestamp() * 1000)
    return v


def _date_columns(rows):
    cols = set()
    for r in rows:
        vals = list(r.values()) if isinstance(r, dict) else list(r)
        for j, v in enumerate(vals):
            if isinstance(v, (_dt.date, _dt.datetime)):
                cols.add(j)
    return cols


def _norm_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (Neo4jDate, Neo4jDateTime)):
        return v.iso_format().split("+")[0].split("[")[0]
    if isinstance(v, _dt.datetime):
        return v.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        as_int = int(v)
        return str(as_int) if abs(v - as_int) < 1e-9 else f"{v:.6f}"
    s = str(v)
    if len(s) >= 19 and s[4] == "-" and s[7] == "-" and (s[10] == "T" or s[10] == " "):
        return s.replace(" ", "T")[:19]
    return s


def _norm_row(row, date_cols=frozenset()):
    vals = list(row.values()) if isinstance(row, dict) else list(row)
    return tuple(
        str(_to_epoch_ms(v)) if (j in date_cols and v is not None) else _norm_value(v)
        for j, v in enumerate(vals)
    )


def compare_rowsets(ref_rows, trans_rows, date_cols=frozenset()):
    ref = Counter(_norm_row(r, date_cols) for r in ref_rows)
    trans = Counter(_norm_row(r, date_cols) for r in trans_rows)
    overlap = sum((ref & trans).values())
    n_ref = sum(ref.values())
    n_trans = sum(trans.values())
    precision = overlap / n_trans if n_trans else (1.0 if n_ref == 0 else 0.0)
    recall = overlap / n_ref if n_ref else (1.0 if n_trans == 0 else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"match": ref == trans, "result_f1": f1, "sql_rows": n_ref, "cypher_rows": n_trans}


# --- query runners ---
def run_postgres(sql):
    try:
        with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_S * 1000}")
            cur.execute(sql)
            return cur.fetchall(), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def run_cypher(driver, query):
    try:
        with driver.session(database=NEO4J_DB) as session:
            result = session.run(Query(query, timeout=TIMEOUT_S))
            return [r.data() for r in result], None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def main():
    if not os.environ.get("NEO4J_PASSWORD"):
        sys.exit("Export NEO4J_PASSWORD (and source .env) before running.")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, os.environ["NEO4J_PASSWORD"]))
    rows = []
    try:
        for q in load_dataset("ldbc"):
            cypher = q.expected.get("cypher")
            if not cypher:
                continue
            sql_rows, sql_err = run_postgres(q.sql)
            cy_rows, cy_err = run_cypher(driver, cypher)
            cmp = compare_rowsets(sql_rows, cy_rows, _date_columns(sql_rows))
            err = sql_err or cy_err
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
        driver.close()

    hdr = f"{'query':10} {'diff':7} {'match':6} {'sql_rows':>9} {'cypher_rows':>12} {'F1':>5}  note"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        line = (
            f"{r['query_id']:10} {r['difficulty']:7} {str(r['match']):6} "
            f"{r['sql_rows']:>9} {r['cypher_rows']:>12} {r['result_f1']:>5.2f}  {r['note']}"
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
        print("  MISMATCH (gold SQL and gold Cypher disagree):", [r["query_id"] for r in mismatch])
    if vacuous:
        print("  vacuous (oracle returns 0 rows; not a real test):", [r["query_id"] for r in vacuous])
    if errored:
        print("  errored:", [r["query_id"] for r in errored])

    sys.exit(1 if (mismatch or errored) else 0)


if __name__ == "__main__":
    main()
