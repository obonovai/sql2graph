"""Execution-based comparison engine: oracle rows vs translated-query rows.

The shared implementation behind notebook 05 (execution metrics) and
``eval/scripts/validate_gold.py`` (gold-set validation): DB connection config,
the date-reconciling multiset comparator, one query executor per backend, and
the per-target record-execution loop. Extracted from notebook 05, which had
the proven superset of the comparator semantics (the validator scripts used
to carry drifted copies).

Comparator semantics: the Postgres oracle defines which output columns are
dates; those reconcile to epoch-millis on all sides so Postgres timestamps,
Neo4j native temporals, and ArangoDB/Gremlin ISO-8601 strings compare equal.
``empty_as_null=True`` (the AQL/Gremlin paths, see EMPTY_AS_NULL_TARGETS) also
maps '' -> None: ArangoDB stores absent optional text as '' and the gold
Gremlin projects NULLs via ``coalesce(values(x), constant(''))``, both meaning
Postgres NULL.

Import-light by design: DB drivers load lazily inside the runners (passwords
asserted on first use per target), and pandas inside :func:`execute_records`,
so importing this module needs nothing running and nothing optional installed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import queue as _queue
import threading as _threading
from collections import Counter
from pathlib import Path
from time import perf_counter

from neo4j.time import Date as Neo4jDate
from neo4j.time import DateTime as Neo4jDateTime

# --- connection config (graphonauts backends; see eval/README.md) ---
# Postgres is the source oracle, not a config/servers/ target, so its DSN keeps its own
# env-with-default lookups; the defaults match graphonauts's postgres compose.
PG_DSN = (
    f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
    f"port={os.environ.get('POSTGRES_PORT', '5433')} "
    f"user={os.environ.get('POSTGRES_USER', 'graphonaut')} "
    f"password={os.environ.get('POSTGRES_PASSWORD', 'password')} "
    f"dbname={os.environ.get('POSTGRES_DB', 'graphonaut')}"
)

# The graph-DB connection settings (Neo4j/ArangoDB/Gremlin) are sourced from the library's
# server configs under config/servers/*.yaml, so a local eval run needs no exported passwords.
# Env vars still override per field (env value wins; otherwise the config value is used) via
# the _server_cfg() helper below. Loaded lazily -- sql2graph.load_server_config pulls in the
# DB drivers through the validators package, and this module is import-light by design (see the
# module docstring), so nothing loads until a runner is first used.
_CONFIG_SERVERS_DIR = Path(__file__).resolve().parents[2] / "config" / "servers"
_server_cfg_cache: dict = {}


def _server_cfg(name: str):
    """Load and cache the config/servers/<name>.yaml server config (lazily)."""
    if name not in _server_cfg_cache:
        from sql2graph import load_server_config

        _server_cfg_cache[name] = load_server_config(_CONFIG_SERVERS_DIR / f"{name}.yaml")
    return _server_cfg_cache[name]


# Per-query timeout ceiling, seconds (server-side where the backend supports it).
TIMEOUT_S = int(os.environ.get("EVAL_QUERY_TIMEOUT", "180"))

POSTGRES_DATASETS = {"ldbc"}  # datasets with a loaded Postgres oracle
TARGET_DB = {"cypher": "neo4j", "aql": "arangodb", "gremlin": "gremlin"}  # graph DB per target
# Targets whose backends return '' where the Postgres oracle has NULL text.
EMPTY_AS_NULL_TARGETS = frozenset({"aql", "gremlin"})


# --- row normalisation ---
def parse_iso(s: str):
    """ArangoDB/Gremlin return dates as ISO strings ('2010-02-14T15:32:10.447Z' or bare
    '1989-12-03'). Parse to stdlib date/datetime so they reconcile via to_epoch_ms;
    None for non-date strings (they fall through to string compare)."""
    if len(s) < 10 or s[4] != "-" or s[7] != "-":
        return None
    try:
        if len(s) == 10:
            return _dt.date.fromisoformat(s)
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_epoch_ms(v):
    """Canonicalise a date-typed value to epoch-millis so Postgres timestamps, Neo4j native
    temporals (DateTime/Date), and ISO strings (ArangoDB/Gremlin, or the JSON reference
    cache) all compare equal."""
    if isinstance(v, (Neo4jDate, Neo4jDateTime)):
        v = v.to_native()  # Neo4j temporal -> stdlib date/datetime, handled below
    if isinstance(v, str):
        parsed = parse_iso(v)
        if parsed is not None:
            v = parsed
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_dt.UTC)
        return int(v.timestamp() * 1000)
    if isinstance(v, _dt.date):
        return int(_dt.datetime(v.year, v.month, v.day, tzinfo=_dt.UTC).timestamp() * 1000)
    return v


def date_columns(rows) -> set[int]:
    """Column positions the oracle (Postgres) returns as date/datetime."""
    cols = set()
    for r in rows:
        vals = list(r.values()) if isinstance(r, dict) else list(r)
        for j, v in enumerate(vals):
            if isinstance(v, (_dt.date, _dt.datetime)):
                cols.add(j)
    return cols


def norm_value(v, empty_as_null: bool = False):
    if v is None:
        return None
    if empty_as_null and v == "":
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


def norm_row(row, date_cols=frozenset(), empty_as_null: bool = False) -> tuple:
    vals = list(row.values()) if isinstance(row, dict) else list(row)
    return tuple(
        str(to_epoch_ms(v)) if (j in date_cols and v is not None) else norm_value(v, empty_as_null)
        for j, v in enumerate(vals)
    )


# --- multiset comparison ---
def compare_rowsets(ref_rows, trans_rows, date_cols=frozenset(), empty_as_null: bool = False) -> dict:
    """Compare oracle rows against translated-query rows as normalised multisets."""
    ref = Counter(norm_row(r, date_cols, empty_as_null) for r in ref_rows)
    trans = Counter(norm_row(r, date_cols, empty_as_null) for r in trans_rows)
    overlap = sum((ref & trans).values())
    n_ref = sum(ref.values())
    n_trans = sum(trans.values())
    union = sum((ref | trans).values())
    precision = overlap / n_trans if n_trans else (1.0 if n_ref == 0 else 0.0)
    recall = overlap / n_ref if n_ref else (1.0 if n_trans == 0 else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "execution_accuracy": 1.0 if ref == trans else 0.0,
        "result_precision": precision,
        "result_recall": recall,
        "result_f1": f1,
        "result_jaccard_dist": 1.0 - (overlap / union) if union else 0.0,
        "reference_rows": n_ref,
        "translated_rows": n_trans,
    }


# --- query executors: each returns (rows, runtime_seconds, error_or_None) ---
# Drivers are built lazily so each backend's password is only required when that
# target actually runs, and importing this module needs no backend at all.
def run_postgres(sql: str):
    import psycopg

    t0 = perf_counter()
    try:
        with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_S * 1000}")
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as exc:
        return [], perf_counter() - t0, f"{type(exc).__name__}: {exc}"
    return rows, perf_counter() - t0, None


_neo4j_driver = None


def _neo4j():
    global _neo4j_driver
    if _neo4j_driver is None:
        from neo4j import GraphDatabase

        cfg = _server_cfg("neo4j")
        uri = os.environ.get("NEO4J_URI", cfg.uri)
        user = os.environ.get("NEO4J_USER", cfg.username)
        password = os.environ.get("NEO4J_PASSWORD", cfg.password)
        _neo4j_driver = GraphDatabase.driver(uri, auth=(user, password))
    return _neo4j_driver


def _neo4j_database() -> str:
    return os.environ.get("NEO4J_DATABASE", _server_cfg("neo4j").database)


def run_cypher(query: str):
    from neo4j import Query

    t0 = perf_counter()
    try:
        with _neo4j().session(database=_neo4j_database()) as session:
            result = session.run(Query(query, timeout=TIMEOUT_S))
            rows = [r.data() for r in result]
    except Exception as exc:
        return [], perf_counter() - t0, f"{type(exc).__name__}: {exc}"
    return rows, perf_counter() - t0, None


_arango_client = None
_arango_db = None


def _arango():
    global _arango_client, _arango_db
    if _arango_db is None:
        from arango import ArangoClient

        cfg = _server_cfg("arangodb")
        url = os.environ.get("ARANGO_URL", cfg.url)
        user = os.environ.get("ARANGO_USER", cfg.username)
        password = os.environ.get("ARANGO_PASSWORD", cfg.password)
        # graphonauts loads LDBC into ArangoDB database 'graphonauts'. The config's database
        # ('ldbc') is the library's server-validation example DB -- a deliberately separate
        # target -- so the eval path keeps 'graphonauts' (env override still honoured; both
        # ARANGO_DATABASE and the old ARANGO_DB spelling are accepted).
        database = os.environ.get("ARANGO_DATABASE") or os.environ.get("ARANGO_DB", "graphonauts")
        # HTTP read timeout kept above the server-side max_runtime so a slow query surfaces
        # as a clean AQL timeout, not an HTTP ReadTimeout.
        _arango_client = ArangoClient(hosts=url, request_timeout=TIMEOUT_S + 30)
        _arango_db = _arango_client.db(database, username=user, password=password)
    return _arango_db


def run_aql(query: str):
    # AQL RETURN {...} yields dicts, same shape as run_cypher's r.data(); the comparator
    # reads them positionally (RETURN key order must match the SQL SELECT order).
    t0 = perf_counter()
    try:
        cursor = _arango().aql.execute(query, max_runtime=float(TIMEOUT_S))
        rows = list(cursor)
    except Exception as exc:
        return [], perf_counter() - t0, f"{type(exc).__name__}: {exc}"
    return rows, perf_counter() - t0, None


def _shape_gremlin_row(row):
    # norm_row iterates each row (dict.values() or list(...)); a scalar Gremlin result
    # (bare count(), values() of one column) would crash it, so wrap it as a 1-tuple.
    return row if isinstance(row, (dict, list, tuple)) else (row,)


def run_gremlin(query: str):
    # Gremlin runs in a fresh daemon thread per query, for two reasons:
    # 1. gremlinpython's Client cannot be constructed on a thread with a RUNNING asyncio
    #    loop (the Jupyter kernel's) -- it raises "Cannot run the event loop while another
    #    loop is running". A fresh thread has no loop.
    # 2. A lost/oversized response frame can wedge the websocket and block .result()
    #    forever (observed: server and kernel both idle mid-run). The daemon thread plus a
    #    client-side timeout turns any wedge into a recorded execution_error, and an
    #    abandoned daemon thread cannot block interpreter shutdown.
    t0 = perf_counter()
    outcome = _queue.Queue()
    # Resolve config on this (main) thread; the worker thread only touches the driver.
    gcfg = _server_cfg("gremlin")
    url = os.environ.get("GREMLIN_URL", gcfg.url)
    traversal_source = os.environ.get("GREMLIN_TRAVERSAL_SOURCE", gcfg.traversal_source)

    def _work():
        try:
            from gremlin_python.driver.client import Client

            client = Client(url, traversal_source)  # unauthenticated (TinkerGraph)
            result_set = client.submit(query, request_options={"evaluationTimeout": TIMEOUT_S * 1000})
            rows = [_shape_gremlin_row(r) for r in result_set.all().result(timeout=TIMEOUT_S + 30)]
            client.close()
            outcome.put(("ok", rows))
        except Exception as exc:
            outcome.put(("err", f"{type(exc).__name__}: {exc}"))

    _threading.Thread(target=_work, daemon=True).start()
    try:
        kind, payload = outcome.get(timeout=TIMEOUT_S + 60)
    except _queue.Empty:
        return [], perf_counter() - t0, "ClientHang: no response within client-side timeout (connection abandoned)"
    if kind == "err":
        return [], perf_counter() - t0, payload
    return payload, perf_counter() - t0, None


RUNNERS = {"cypher": run_cypher, "aql": run_aql, "gremlin": run_gremlin}


def close_clients() -> None:
    """Close the lazy driver singletons (safe to call when none were opened)."""
    global _neo4j_driver, _arango_client, _arango_db
    if _neo4j_driver is not None:
        _neo4j_driver.close()
        _neo4j_driver = None
    if _arango_client is not None:
        _arango_client.close()
        _arango_client = None
        _arango_db = None


# --- per-target execution loop (notebook 05's engine) ---
def execute_records(records: list[dict], target: str, cache: dict, cache_path: Path):
    """Execute every runnable record of one target; returns the metric rows as a DataFrame.

    The Postgres oracle rows are cached in ``cache`` (persisted to ``cache_path`` keyed by
    ``dataset:query_id``), so re-runs and other targets touch nothing but their graph DB.
    Records that failed validation (or errored) score 0 without touching the backend.
    """
    import pandas as pd

    runnable = [r for r in records if r["dataset"] in POSTGRES_DATASETS and r["target"] == target]
    print(f"{target}: {len(runnable)} executable record(s).")
    rows_out = []
    for idx, rec in enumerate(runnable, start=1):
        qid = rec["query_id"]
        ckey = f"{rec['dataset']}:{qid}"
        print(f"[{idx:3d}/{len(runnable)}] {qid} ({rec['target']})", end=" ", flush=True)
        if ckey in cache:
            ref_rows = [tuple(r) for r in cache[ckey]["rows"]]
            ref_runtime = cache[ckey]["runtime"]
            ref_error = cache[ckey].get("error")
            dcols = set(cache[ckey].get("date_cols", []))
        else:
            ref_rows, ref_runtime, ref_error = run_postgres(rec["sql"])
            dcols = sorted(date_columns(ref_rows))
            cache[ckey] = {
                "rows": [list(r) for r in ref_rows],
                "runtime": ref_runtime,
                "error": ref_error,
                "date_cols": dcols,
            }
            cache_path.write_text(json.dumps(cache, default=str))
            dcols = set(dcols)
        out = {
            "dataset": rec["dataset"],
            "target": rec["target"],
            "model": rec["model"],
            "query_id": qid,
            "difficulty": rec["difficulty"],
            "validation_passed": rec["validation_passed"],
            "reference_runtime_s": ref_runtime,
            "reference_error": ref_error,
            "translated_runtime_s": None,
            "execution_error": None,
            "execution_accuracy": 0.0,
            "result_precision": 0.0,
            "result_recall": 0.0,
            "result_f1": 0.0,
            "result_jaccard_dist": 1.0,
            "reference_rows": len(ref_rows),
            "translated_rows": 0,
        }
        if ref_error is not None:
            print(f"REF ERROR ({ref_error})")
            rows_out.append(out)
            continue
        if not rec["validation_passed"] or not rec.get("generated_query"):
            print("skip (translation invalid)")
            rows_out.append(out)
            continue
        trans_rows, trans_runtime, trans_error = RUNNERS[target](rec["generated_query"])
        out["translated_runtime_s"] = trans_runtime
        out["execution_error"] = trans_error
        if trans_error is not None:
            print(f"EXEC ERROR ({trans_error[:60]})")
            rows_out.append(out)
            continue
        out.update(compare_rowsets(ref_rows, trans_rows, dcols, empty_as_null=target in EMPTY_AS_NULL_TARGETS))
        marker = "ok" if out["execution_accuracy"] == 1.0 else "ne"
        print(
            f"{marker} EX={out['execution_accuracy']:.0f} F1={out['result_f1']:.2f} "
            f"ref={out['reference_rows']} trans={out['translated_rows']}"
        )
        rows_out.append(out)
    return pd.DataFrame(rows_out)
