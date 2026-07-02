"""Validate the LDBC gold set: does each gold AQL return the same data as its gold SQL?

For every query in evaluation/datasets/ldbc.yaml this runs the `sql` on graphonauts2's
Postgres and the `expected_aql` on graphonauts2's ArangoDB, then compares the result sets as
multisets. It is the AQL analogue of validate_gold_queries.py (which does SQL vs Cypher).

The gold AQL uses the *unified* SCREAMING_SNAKE edge names from config/mappings/ldbc.yaml
(KNOWS, HAS_CREATOR, HAS_TAG, ...). Those collections must exist in the ArangoDB -- build
them first with:
    ARANGO_PASSWORD=password uv run python evaluation/build_arango_unified_edges.py

This checks the gold set itself - it is independent of any model.

Run:
    set -a; source .env; set +a
    POSTGRES_PASSWORD=password ARANGO_PASSWORD=password uv run python evaluation/validate_gold_aql.py
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "evaluation"))

import psycopg
from arango import ArangoClient

from eval_harness import load_dataset

# --- connection config (mirrors notebook 05 / validate_gold_queries.py) ---
PG_DSN = (
    f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
    f"port={os.environ.get('POSTGRES_PORT', '5433')} "
    f"user={os.environ.get('POSTGRES_USER', 'graphonaut')} "
    f"password={os.environ.get('POSTGRES_PASSWORD', 'password')} "
    f"dbname={os.environ.get('POSTGRES_DB', 'graphonaut')}"
)
ARANGO_URL = os.environ.get("ARANGO_URL", "http://localhost:8529")
ARANGO_USER = os.environ.get("ARANGO_USER", "root")
ARANGO_PASSWORD = os.environ.get("ARANGO_PASSWORD", "password")
ARANGO_DB = os.environ.get("ARANGO_DB", "graphonauts")
TIMEOUT_S = int(os.environ.get("EVAL_QUERY_TIMEOUT", "60"))


# --- date-reconciling multiset comparator (from notebook 05, extended for AQL) ---
def _parse_iso(s: str):
    """ArangoDB returns dates as ISO strings ('2010-02-14T15:32:10.447Z', '1989-12-03').

    Parse them to stdlib date/datetime so they reconcile with Postgres native dates via
    _to_epoch_ms. Returns None for non-date strings (they fall through to string compare).
    """
    if len(s) < 10 or s[4] != "-" or s[7] != "-":
        return None
    try:
        if len(s) == 10:
            return _dt.date.fromisoformat(s)
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_epoch_ms(v):
    if isinstance(v, str):  # AQL date column -> parse ISO string, else leave as-is
        parsed = _parse_iso(v)
        if parsed is not None:
            v = parsed
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_dt.timezone.utc)
        return int(v.timestamp() * 1000)
    if isinstance(v, _dt.date):
        return int(_dt.datetime(v.year, v.month, v.day, tzinfo=_dt.timezone.utc).timestamp() * 1000)
    return v


def _date_columns(rows):
    """Column indices that hold a native date/datetime on the Postgres (oracle) side."""
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
    if v == "":  # ArangoDB stores absent optional text (e.g. image-post content) as "",
        return None  # where Postgres has NULL -- reconcile the two to the same value.
    if isinstance(v, bool):
        return "True" if v else "False"
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
    return {"match": ref == trans, "result_f1": f1, "sql_rows": n_ref, "aql_rows": n_trans}


# --- query runners ---
def run_postgres(sql):
    try:
        with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_S * 1000}")
            cur.execute(sql)
            return cur.fetchall(), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def run_aql(db, query):
    try:
        cursor = db.aql.execute(query, max_runtime=float(TIMEOUT_S))
        return list(cursor), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def main():
    # HTTP read timeout above the server-side max_runtime, so a slow query surfaces as a
    # clean AQL timeout error (caught below) rather than an HTTP ReadTimeout.
    client = ArangoClient(hosts=ARANGO_URL, request_timeout=TIMEOUT_S + 30)
    db = client.db(ARANGO_DB, username=ARANGO_USER, password=ARANGO_PASSWORD)

    rows = []
    for q in load_dataset("ldbc"):
        aql = q.expected.get("aql")
        if not aql:
            continue
        sql_rows, sql_err = run_postgres(q.sql)
        aql_rows, aql_err = run_aql(db, aql)
        cmp = compare_rowsets(sql_rows, aql_rows, _date_columns(sql_rows))
        err = sql_err or aql_err
        if err:
            note = "ERROR"
        elif cmp["match"] and cmp["sql_rows"] == 0:
            note = "vacuous (0 rows both sides)"
        elif cmp["match"]:
            note = "ok"
        else:
            note = "MISMATCH"
        rows.append({"query_id": q.id, "difficulty": q.difficulty, **cmp, "error": err, "note": note})

    hdr = f"{'query':10} {'diff':7} {'match':6} {'sql_rows':>9} {'aql_rows':>9} {'F1':>5}  note"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        line = (
            f"{r['query_id']:10} {r['difficulty']:7} {str(r['match']):6} "
            f"{r['sql_rows']:>9} {r['aql_rows']:>9} {r['result_f1']:>5.2f}  {r['note']}"
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
        print("  MISMATCH (gold SQL and gold AQL disagree):", [r["query_id"] for r in mismatch])
    if vacuous:
        print("  vacuous (oracle returns 0 rows; not a real test):", [r["query_id"] for r in vacuous])
    if errored:
        print("  errored:", [r["query_id"] for r in errored])

    sys.exit(1 if (mismatch or errored) else 0)


if __name__ == "__main__":
    main()
