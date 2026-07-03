"""Offline tests for the execution comparator (harness.execution).

The comparator was extracted from notebook 05, where it had been proven against
live graphonauts2 backends (gold sets validated 14/14 per target). These tests
lock the extracted semantics without needing any database: date reconciliation
across driver representations, the '' -> NULL gate, float int-collapse, and the
multiset precision/recall/F1 math.
"""

from __future__ import annotations

import datetime as dt

from harness.execution import compare_rowsets, date_columns, norm_value, parse_iso, to_epoch_ms
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


def test_date_columns_come_from_the_oracle_rows() -> None:
    rows = [("alice", dt.date(1989, 12, 3), 7), ("bob", dt.date(1990, 1, 1), 9)]
    assert date_columns(rows) == {1}
    assert date_columns([]) == set()


def test_norm_value_collapses_int_valued_floats() -> None:
    # Postgres COUNT(*) comes back as int, some drivers as float; both must compare equal.
    assert norm_value(3.0) == norm_value(3) == "3"
    assert norm_value(3.5) == "3.500000"


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


def test_date_column_reconciliation_end_to_end() -> None:
    # Column 1 is a date on the oracle side; the translated side returns ISO strings.
    ref = [("alice", MIDSUMMER)]
    trans = [("alice", "2010-06-01T12:30:45Z")]
    dcols = date_columns(ref)
    assert dcols == {1}
    assert compare_rowsets(ref, trans, date_cols=dcols)["execution_accuracy"] == 1.0
    # Without the date-column hint the representations differ as plain strings...
    # (both sides normalise to the same 19-char ISO form, so this still matches)
    assert compare_rowsets(ref, trans)["execution_accuracy"] == 1.0


def test_vacuous_zero_rows_both_sides() -> None:
    out = compare_rowsets([], [])
    assert out["execution_accuracy"] == 1.0
    assert out["result_precision"] == 1.0
    assert out["result_recall"] == 1.0
    assert out["result_jaccard_dist"] == 0.0
