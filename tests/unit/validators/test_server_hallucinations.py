"""Unit tests for schema-hallucination reporting in the server validators.

These exercise the pure notification-filtering / catalogue-cross-check logic
with fakes, so they need no live database. The live round-trips are covered by
the integration suite (``tests/integration/test_neo4j.py``,
``tests/integration/test_arango.py``).
"""

from __future__ import annotations

from typing import cast

from neo4j import NotificationClassification, ResultSummary

from sql2graph.validators.aql.server import _unknown_collection_errors, _validate_aql_sync
from sql2graph.validators.cypher.server import _unrecognized_notifications

# --- Cypher: filter EXPLAIN notifications down to the UNRECOGNIZED class ------


class _FakeGql:
    """Stands in for a neo4j ``GqlStatusObject``."""

    def __init__(
        self,
        *,
        is_notification: bool,
        classification: object,
        gql_status: str = "01N50",
        status_description: str = "desc",
    ) -> None:
        self.is_notification = is_notification
        self.classification = classification
        self.gql_status = gql_status
        self.status_description = status_description


class _FakeSummary:
    def __init__(self, objs: list[_FakeGql]) -> None:
        self.gql_status_objects = objs


def _notifications(*objs: _FakeGql) -> list[str]:
    """Run the notification filter over fake status objects.

    The fakes are duck-typed, so cast past the concrete ``ResultSummary`` hint.
    """
    return _unrecognized_notifications(cast(ResultSummary, _FakeSummary(list(objs))))


def test_unrecognized_notification_is_surfaced() -> None:
    errors = _notifications(
        _FakeGql(
            is_notification=True,
            classification=NotificationClassification.UNRECOGNIZED,
            gql_status="01N50",
            status_description="The label `Ghost` does not exist. Verify the spelling.",
        )
    )
    assert len(errors) == 1
    assert "Ghost" in errors[0]
    assert "01N50" in errors[0]


def test_non_unrecognized_notifications_are_ignored() -> None:
    """Performance/deprecation notifications must not fail an otherwise valid query."""
    errors = _notifications(
        _FakeGql(is_notification=True, classification=NotificationClassification.PERFORMANCE),
        _FakeGql(is_notification=True, classification=NotificationClassification.DEPRECATION),
        _FakeGql(is_notification=True, classification=NotificationClassification.GENERIC),
    )
    assert errors == []


def test_success_status_object_is_not_a_notification() -> None:
    """The outcome status object (00000/02000) has is_notification=False and is skipped."""
    errors = _notifications(
        _FakeGql(
            is_notification=False,
            classification=NotificationClassification.UNRECOGNIZED,
            gql_status="00000",
        )
    )
    assert errors == []


def test_no_notifications_is_empty() -> None:
    """Managed mode (notifications_min_severity=OFF) yields no notifications at all."""
    assert _notifications() == []


# --- AQL: cross-check referenced collections against the catalogue -----------


class _FakeAql:
    def __init__(self, collections: list[str], *, raise_exc: Exception | None = None) -> None:
        self._collections = collections
        self._raise = raise_exc

    def validate(self, _query: str) -> dict[str, object]:
        if self._raise is not None:
            raise self._raise
        return {"parsed": True, "collections": self._collections, "bind_vars": []}


class _FakeDb:
    def __init__(
        self,
        *,
        referenced: list[str],
        existing: list[str],
        validate_exc: Exception | None = None,
        collections_exc: Exception | None = None,
    ) -> None:
        self.name = "testdb"
        self.aql = _FakeAql(referenced, raise_exc=validate_exc)
        self._existing = existing
        self._collections_exc = collections_exc

    def collections(self) -> list[dict[str, str]]:
        if self._collections_exc is not None:
            raise self._collections_exc
        return [{"name": n} for n in self._existing]


def test_aql_reports_unknown_collection() -> None:
    db = _FakeDb(referenced=["users", "ghost"], existing=["users"])
    errors = _validate_aql_sync(db, "FOR u IN users RETURN u", check_collections=True)
    assert len(errors) == 1
    assert "ghost" in errors[0]
    assert "testdb" in errors[0]


def test_aql_known_collections_pass() -> None:
    db = _FakeDb(referenced=["users"], existing=["users", "orders"])
    assert _validate_aql_sync(db, "FOR u IN users RETURN u", check_collections=True) == []


def test_aql_check_disabled_suppresses_everything() -> None:
    """check_collections=False is how managed mode keeps its empty DB silent."""
    db = _FakeDb(referenced=["ghost"], existing=[])
    assert _validate_aql_sync(db, "FOR g IN ghost RETURN g", check_collections=False) == []


def test_aql_syntax_error_short_circuits_before_catalogue_check() -> None:
    db = _FakeDb(referenced=[], existing=[], validate_exc=RuntimeError("boom"))
    errors = _validate_aql_sync(db, "FOR u IN", check_collections=True)
    assert errors and "boom" in errors[0]


def test_aql_catalogue_lookup_failure_degrades_to_no_errors() -> None:
    """If the catalogue can't be read we withhold a verdict, not fail a valid parse."""
    db = _FakeDb(referenced=["ghost"], existing=[], collections_exc=RuntimeError("no perms"))
    assert _validate_aql_sync(db, "FOR g IN ghost RETURN g", check_collections=True) == []


def test_aql_no_referenced_collections_skips_catalogue() -> None:
    db = _FakeDb(referenced=[], existing=["users"], collections_exc=AssertionError("must not be called"))
    assert _unknown_collection_errors(db, {"collections": []}) == []
