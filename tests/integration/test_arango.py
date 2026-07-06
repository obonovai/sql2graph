"""Integration tests exercising a real ArangoDB server validator.

Collection-hallucination reporting needs a populated catalogue *and* a config
with ``check_collections=True`` -- which the managed validator deliberately does
not use (it forces suppression on its empty database). So these provision an
ArangoDB container directly via the same helper managed mode uses, seed one
collection, and drive :class:`AqlServerValidator` against it.
"""

from __future__ import annotations

import pytest

from sql2graph.validators.aql.server import AqlServerValidator

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("docker_available")
def test_real_aql_reports_and_suppresses_collection_hallucination() -> None:
    from arango.client import ArangoClient

    from sql2graph.validators.provision import arango as arango_provision

    container, managed_config = arango_provision.start()
    try:
        # managed_config has check_collections=False (empty-DB suppression);
        # build a sibling that reports, and seed one real collection.
        reporting_config = managed_config.model_copy(update={"check_collections": True})
        db = ArangoClient(hosts=reporting_config.url).db(
            reporting_config.database,
            username=reporting_config.username,
            password=reporting_config.password,
        )
        db.create_collection("people")

        reporting = AqlServerValidator(reporting_config)
        try:
            unknown = reporting.validate("FOR x IN nonexistent RETURN x")
            known = reporting.validate("FOR p IN people RETURN p")
        finally:
            reporting.close()

        assert unknown, "expected the unknown collection to be reported as a hallucination"
        assert any("nonexistent" in e for e in unknown), unknown
        assert known == [], f"a seeded collection must pass, got: {known}"

        # Suppression: the managed config (check_collections=False) stays silent
        # even over a collection that does not exist.
        suppressed = AqlServerValidator(managed_config)
        try:
            errors = suppressed.validate("FOR x IN nonexistent RETURN x")
        finally:
            suppressed.close()
        assert errors == [], f"check_collections=False should suppress, got: {errors}"
    finally:
        container.stop()
