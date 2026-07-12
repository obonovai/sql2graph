"""Execution-based comparison engine: oracle rows vs translated-query rows.

The shared *primitives* behind notebook 05 (execution metrics) and
``eval/scripts/validate_gold.py`` (gold-set validation): DB connection config,
the date-reconciling multiset comparator, and one query executor per backend.
Notebook 05 builds its per-record execute-and-compare loop on top of these, so
that flow reads in the notebook; this module keeps only the reusable pieces
(the proven superset of the comparator semantics, since the validator scripts
used to carry drifted copies).

Comparator semantics: temporal cells fold per value to epoch-millis, so Postgres
timestamps, Neo4j native temporals, and ArangoDB/Gremlin ISO-8601 strings compare
equal regardless of engine. Any cell that is a native/driver temporal or matches
an ISO date/datetime reconciles; genuine integers and free text pass through.
``empty_as_null=True`` (the AQL/Gremlin paths, see EMPTY_AS_NULL_TARGETS) also
maps '' -> None: ArangoDB stores absent optional text as '' and the gold
Gremlin projects NULLs via ``coalesce(values(x), constant(''))``, both meaning
Postgres NULL.

Import-light by design: DB drivers load lazily inside the runners (passwords
asserted on first use per target), so importing this module needs nothing
running and nothing optional installed.
"""

from __future__ import annotations

import datetime as _dt
import os
import queue as _queue
import threading as _threading
from collections import Counter
from pathlib import Path
from time import perf_counter

from neo4j.time import Date as Neo4jDate
from neo4j.time import DateTime as Neo4jDateTime

# Pure-stdlib (re only), so importing it keeps this module's import-light contract.
from .arango_edges import expand_unified_edges

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


def norm_value(v, empty_as_null: bool = False):
    # Temporals (native, Neo4j driver, or ISO string) fold to epoch-millis so every
    # engine's representation of an instant compares equal; genuine ints and free text
    # pass through. bool is checked before int (bool is an int subclass).
    if v is None:
        return None
    if empty_as_null and v == "":
        return None
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (Neo4jDate, Neo4jDateTime, _dt.datetime, _dt.date)):
        return str(to_epoch_ms(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        as_int = int(v)
        return str(as_int) if abs(v - as_int) < 1e-9 else f"{v:.6f}"
    if isinstance(v, str):
        parsed = parse_iso(v)
        if parsed is not None:
            return str(to_epoch_ms(parsed))
        return v
    return str(v)


def norm_row(row, empty_as_null: bool = False) -> tuple:
    vals = list(row.values()) if isinstance(row, dict) else list(row)
    return tuple(norm_value(v, empty_as_null) for v in vals)


# --- multiset comparison ---
def compare_rowsets(ref_rows, trans_rows, empty_as_null: bool = False) -> dict:
    """Compare oracle rows against translated-query rows as normalised multisets."""
    ref = Counter(norm_row(r, empty_as_null) for r in ref_rows)
    trans = Counter(norm_row(r, empty_as_null) for r in trans_rows)
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


# --- driver noise suppression ---
_noise_silenced = False


def silence_driver_noise() -> None:
    """Quiet the graph drivers' non-fatal chatter so notebook 05 shows only its per-query
    status lines. Idempotent.

    Suppresses three unrelated sources that otherwise bury the status prints when a
    translated query is wrong or times out: Neo4j server notifications (unknown
    property/label warnings), gremlinpython's ERROR dump of a server error (already
    surfaced as a caught execution_error), and the aiohttp/asyncio "unclosed session" +
    transport ``__del__`` tracebacks that fire when a Gremlin client is garbage-collected
    after its worker thread's event loop has closed.
    """
    global _noise_silenced
    if _noise_silenced:
        return
    import logging
    import sys

    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    logging.getLogger("gremlinpython").setLevel(logging.CRITICAL)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

    _prev_hook = sys.unraisablehook

    def _hook(unraisable):
        # Drop the known-noisy finalizer errors from gremlinpython's aiohttp transport
        # (a Client GC'd after its worker-thread loop closed); delegate anything else.
        obj = repr(getattr(unraisable, "object", ""))
        exc = unraisable.exc_value
        if (
            "AiohttpTransport" in obj
            or "ClientResponse" in obj
            or "ClientSession" in obj
            or (
                isinstance(exc, RuntimeError)
                and str(exc)
                in ("Event loop is closed", "Cannot run the event loop while another loop is running")
            )
        ):
            return
        _prev_hook(unraisable)

    sys.unraisablehook = _hook
    _noise_silenced = True


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

    silence_driver_noise()
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
    silence_driver_noise()
    # graphonauts loads split snake_case edge collections; the gold/candidate AQL names the
    # unified SCREAMING_SNAKE edges. Rewrite unified -> split at query time so no ArangoDB
    # collection has to be materialised (see harness.arango_edges). No-op for edge-free queries.
    query = expand_unified_edges(query)
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
    silence_driver_noise()
    t0 = perf_counter()
    outcome = _queue.Queue()
    # Resolve config on this (main) thread; the worker thread only touches the driver.
    gcfg = _server_cfg("gremlin")
    url = os.environ.get("GREMLIN_URL", gcfg.url)
    traversal_source = os.environ.get("GREMLIN_TRAVERSAL_SOURCE", gcfg.traversal_source)

    def _work():
        client = None
        try:
            from gremlin_python.driver.client import Client

            client = Client(url, traversal_source)  # unauthenticated (TinkerGraph)
            result_set = client.submit(query, request_options={"evaluationTimeout": TIMEOUT_S * 1000})
            rows = [_shape_gremlin_row(r) for r in result_set.all().result(timeout=TIMEOUT_S + 30)]
            outcome.put(("ok", rows))
        except Exception as exc:
            outcome.put(("err", f"{type(exc).__name__}: {exc}"))
        finally:
            # Close on the worker thread (no running loop) in every path; the transport's
            # close() is idempotent, so this prevents the __del__ noise at the source.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

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
