"""Feature-detection tests for ``detect_features`` (rows2graph.sql_features)."""

from __future__ import annotations

from rows2graph.sql_features import ALL_FEATURES, SqlFeature, detect_features


def test_detect_features_plain_select_is_empty() -> None:
    assert detect_features("SELECT a, b FROM t") == frozenset()


def test_detect_features_like() -> None:
    assert SqlFeature.LIKE in detect_features("SELECT a FROM t WHERE name LIKE '%x%'")


def test_detect_features_ilike() -> None:
    assert SqlFeature.LIKE in detect_features("SELECT a FROM t WHERE name ILIKE '%x%'")


def test_detect_features_join() -> None:
    assert SqlFeature.JOIN in detect_features("SELECT a.x FROM a JOIN b ON a.id = b.aid")


def test_detect_features_group_by() -> None:
    feats = detect_features("SELECT k, COUNT(*) FROM t GROUP BY k")
    assert SqlFeature.AGGREGATION in feats


def test_detect_features_aggregate_without_group_by() -> None:
    assert SqlFeature.AGGREGATION in detect_features("SELECT COUNT(*) FROM t")


def test_detect_features_having_implies_aggregation() -> None:
    assert SqlFeature.AGGREGATION in detect_features("SELECT k, COUNT(*) c FROM t GROUP BY k HAVING COUNT(*) > 5")


def test_detect_features_order_limit() -> None:
    feats = detect_features("SELECT a FROM t ORDER BY a LIMIT 10")
    assert SqlFeature.ORDER_LIMIT in feats


def test_detect_features_cte() -> None:
    feats = detect_features("WITH c AS (SELECT * FROM t) SELECT * FROM c")
    assert SqlFeature.CTE in feats
    # A bare CTE without any nested SELECT inside an expression must NOT
    # light up SUBQUERY: distinguishing the two clusters is the whole point.
    assert SqlFeature.SUBQUERY not in feats


def test_detect_features_union() -> None:
    assert SqlFeature.UNION in detect_features("SELECT a FROM t UNION SELECT b FROM u")


def test_detect_features_window() -> None:
    feats = detect_features("SELECT a, ROW_NUMBER() OVER (ORDER BY b) FROM t")
    assert SqlFeature.WINDOW in feats


def test_detect_features_case() -> None:
    feats = detect_features("SELECT CASE WHEN a > 0 THEN 1 ELSE 0 END FROM t")
    assert SqlFeature.CASE in feats


def test_detect_features_scalar_subquery() -> None:
    feats = detect_features("SELECT (SELECT MAX(b) FROM u) FROM t")
    assert SqlFeature.SUBQUERY in feats


def test_detect_features_in_subquery() -> None:
    feats = detect_features("SELECT a FROM t WHERE a IN (SELECT b FROM u)")
    assert SqlFeature.SUBQUERY in feats


def test_detect_features_exists_subquery() -> None:
    feats = detect_features("SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.x = t.x)")
    assert SqlFeature.SUBQUERY in feats


def test_detect_features_distinct() -> None:
    assert SqlFeature.DISTINCT in detect_features("SELECT DISTINCT a FROM t")


def test_detect_features_temporal_date_literal() -> None:
    # A comparison against an ISO date/timestamp string literal must light up
    # TEMPORAL so the date()/datetime() wrapping rule ships.
    assert SqlFeature.TEMPORAL in detect_features("SELECT a FROM lineitem WHERE shipdate >= '1995-03-01'")
    assert SqlFeature.TEMPORAL in detect_features(
        "SELECT a FROM post WHERE creation_date >= '2010-06-01' AND creation_date < '2010-07-01'"
    )


def test_detect_features_temporal_not_fired_on_non_date() -> None:
    # An integer key, ordinary string equality, and a plain numeric predicate
    # must NOT light up TEMPORAL: the detector keys on date-shaped literals.
    assert SqlFeature.TEMPORAL not in detect_features("SELECT a FROM supplier WHERE suppkey = 1337")
    assert SqlFeature.TEMPORAL not in detect_features("SELECT a FROM supplier WHERE name = 'Supplier#000000666'")
    assert SqlFeature.TEMPORAL not in detect_features("SELECT a, b FROM t WHERE x = 1")


def test_detect_features_scalar_functions() -> None:
    # String/number/coalesce scalars and a non-temporal CAST light up SCALAR so
    # the per-target function-mapping chunk ships only when it is needed.
    assert SqlFeature.SCALAR in detect_features("SELECT UPPER(name) FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT a || b FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT COALESCE(a, b) FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT LENGTH(name) FROM t")
    assert SqlFeature.SCALAR in detect_features("SELECT CAST(x AS INTEGER) FROM t")


def test_detect_features_scalar_not_fired_on_plain_or_temporal() -> None:
    # A plain select has no scalar functions; a *temporal* cast belongs to
    # TEMPORAL and must not also drag in the scalar-function chunk.
    assert SqlFeature.SCALAR not in detect_features("SELECT a, b FROM t WHERE x = 1")
    assert SqlFeature.SCALAR not in detect_features("SELECT a FROM t WHERE d >= CAST('1995-03-01' AS DATE)")


def test_detect_features_null_predicate() -> None:
    # IS NULL / IS NOT NULL light up NULL so the per-target null-handling chunk ships.
    assert SqlFeature.NULL in detect_features("SELECT a FROM t WHERE col IS NULL")
    assert SqlFeature.NULL in detect_features("SELECT a FROM t WHERE col IS NOT NULL")


def test_detect_features_null_not_fired_without_null_test() -> None:
    # Ordinary predicates must not light up NULL.
    assert SqlFeature.NULL not in detect_features("SELECT a FROM t WHERE x = 1")
    assert SqlFeature.NULL not in detect_features("SELECT a FROM t WHERE name = 'x'")


def test_detect_features_parse_failure_returns_all() -> None:
    # Garbage SQL must yield the full feature set so no rule chunk is
    # silently stripped from the prompt.
    assert detect_features("SELECT ;;; FROM") == ALL_FEATURES
