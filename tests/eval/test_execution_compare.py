"""Offline tests for the execution comparator (harness.execution).

The comparator was extracted from notebook 05, where it had been proven against
live graphonauts backends (gold sets validated 14/14 per target). These tests
lock the extracted semantics without needing any database: date reconciliation
across driver representations, the '' -> NULL gate, float int-collapse, and the
multiset precision/recall/F1 math.
"""

from __future__ import annotations

import datetime as dt

from harness.execution import compare_rowsets, norm_value, parse_iso, to_epoch_ms
from neo4j.time import DateTime as Neo4jDateTime

MIDSUMMER = dt.datetime(2010, 6, 1, 12, 30, 45, tzinfo=dt.UTC)


def test_parse_iso_accepts_dates_and_datetimes() -> None:
    assert parse_iso("1989-12-03") == dt.date(1989, 12, 3)
    parsed = parse_iso("2010-02-14T15:32:10.447Z")
    assert isinstance(parsed, dt.datetime)
    assert parsed.tzinfo is not None


def test_parse_iso_rejects_non_dates() -> None:
    assert parse_iso("hello world") is None
    assert parse_iso("12345") is None
    assert parse_iso("2010-13-99") is None  # date-shaped but invalid


def test_to_epoch_ms_reconciles_driver_representations() -> None:
    # Postgres native datetime, an ISO string (ArangoDB / the JSON reference
    # cache), and a Neo4j temporal must all land on the same epoch-millis.
    from_native = to_epoch_ms(MIDSUMMER)
    from_iso = to_epoch_ms("2010-06-01T12:30:45+00:00")
    from_neo4j = to_epoch_ms(Neo4jDateTime(2010, 6, 1, 12, 30, 45, 0, tzinfo=dt.UTC))
    assert from_native == from_iso == from_neo4j


def test_to_epoch_ms_leaves_non_dates_alone() -> None:
    assert to_epoch_ms(42) == 42
    assert to_epoch_ms("not a date") == "not a date"


def test_norm_value_collapses_int_valued_floats() -> None:
    # Postgres COUNT(*) comes back as int, some drivers as float; both must compare equal.
    assert norm_value(3.0) == norm_value(3) == "3"
    assert norm_value(3.5) == "3.500000"


def test_norm_value_does_not_fold_free_text_or_ids() -> None:
    # A strict ISO shape is required to fold; free text, partial dates, and plain
    # integers pass through untouched (no false collisions).
    assert norm_value("Augustine_of_Hippo") == "Augustine_of_Hippo"
    assert norm_value("2010 was a good year") == "2010 was a good year"
    assert norm_value("2010-06") == "2010-06"
    assert norm_value(933000000000000) == "933000000000000"


def test_compare_rowsets_exact_match() -> None:
    ref = [("alice", 1), ("bob", 2)]
    out = compare_rowsets(ref, [("bob", 2), ("alice", 1)])
    assert out["execution_accuracy"] == 1.0
    assert out["result_f1"] == 1.0
    assert out["reference_rows"] == out["translated_rows"] == 2


def test_compare_rowsets_partial_overlap_f1() -> None:
    ref = [("a",), ("b",), ("c",), ("d",)]
    trans = [("a",), ("b",), ("x",), ("y",)]
    out = compare_rowsets(ref, trans)
    assert out["execution_accuracy"] == 0.0
    assert out["result_precision"] == 0.5
    assert out["result_recall"] == 0.5
    assert out["result_f1"] == 0.5
    assert abs(out["result_jaccard_dist"] - (1 - 2 / 6)) < 1e-9


def test_compare_rowsets_multiset_duplicates_matter() -> None:
    # Same distinct values but different multiplicities must not be a match.
    out = compare_rowsets([("a",), ("a",)], [("a",)])
    assert out["execution_accuracy"] == 0.0
    assert out["result_recall"] == 0.5


def test_empty_as_null_gate() -> None:
    # ArangoDB/Gremlin return '' where the Postgres oracle has NULL text.
    ref = [("alice", None)]
    trans = [("alice", "")]
    assert compare_rowsets(ref, trans, empty_as_null=True)["execution_accuracy"] == 1.0
    # The Cypher path keeps '' and NULL distinct.
    assert compare_rowsets(ref, trans, empty_as_null=False)["execution_accuracy"] == 0.0


def test_date_reconciliation_end_to_end() -> None:
    # Oracle native datetime vs translated ISO string reconcile per value, no hint needed.
    ref = [("alice", MIDSUMMER)]
    trans = [("alice", "2010-06-01T12:30:45Z")]
    assert compare_rowsets(ref, trans)["execution_accuracy"] == 1.0
    # Bare dates (e.g. Gremlin birthday, length-10 ISO) fold to the same instant too.
    ref_d = [("bob", dt.date(1989, 12, 3))]
    trans_d = [("bob", "1989-12-03")]
    assert compare_rowsets(ref_d, trans_d)["execution_accuracy"] == 1.0


def test_vacuous_zero_rows_both_sides() -> None:
    out = compare_rowsets([], [])
    assert out["execution_accuracy"] == 1.0
    assert out["result_precision"] == 1.0
    assert out["result_recall"] == 1.0
    assert out["result_jaccard_dist"] == 0.0


class _FakeUnraisable:
    """Duck-types sys.UnraisableHookArgs for testing the noise-suppression hook."""

    def __init__(self, obj, exc) -> None:
        self.object = obj
        self.exc_value = exc
        self.exc_type = type(exc)
        self.exc_traceback = None
        self.err_msg = None


def test_silence_driver_noise_quiets_loggers_and_finalizers() -> None:
    import logging
    import sys

    from harness import execution as ex

    prev_hook = sys.unraisablehook
    prev_flag = ex._noise_silenced
    names = ("neo4j.notifications", "gremlinpython", "asyncio")
    prev_levels = {n: logging.getLogger(n).level for n in names}
    try:
        # Install our hook on top of a sentinel so we can observe delegation.
        delegated: list = []
        sys.unraisablehook = lambda u: delegated.append(u)
        ex._noise_silenced = False  # force a fresh install regardless of prior runner calls
        ex.silence_driver_noise()

        assert logging.getLogger("neo4j.notifications").level == logging.ERROR
        assert logging.getLogger("gremlinpython").level == logging.CRITICAL
        assert logging.getLogger("asyncio").level == logging.CRITICAL

        hook = sys.unraisablehook
        # Swallowed by object repr (the gremlin aiohttp transport finalizer)...
        hook(_FakeUnraisable("<function AiohttpTransport.__del__ at 0x1>", RuntimeError("boom")))
        # ...and by exception message (a bare closed-loop finalizer error).
        hook(_FakeUnraisable("<object>", RuntimeError("Event loop is closed")))
        assert delegated == []
        # An unrelated unraisable is delegated to the previous hook untouched.
        marker = ValueError("unrelated")
        hook(_FakeUnraisable("<object>", marker))
        assert len(delegated) == 1 and delegated[0].exc_value is marker
    finally:
        sys.unraisablehook = prev_hook
        ex._noise_silenced = prev_flag
        for n, lvl in prev_levels.items():
            logging.getLogger(n).setLevel(lvl)
