"""``analyze_sql`` tests: features + source tables + parse status + column refs."""

from __future__ import annotations

from rows2graph.sql_features import ALL_FEATURES, SqlFeature, analyze_sql


def test_analyze_sql_reports_features_tables_and_parse_ok() -> None:
    a = analyze_sql("SELECT * FROM orders o JOIN customer c ON o.custkey = c.custkey")
    assert a.parse_ok is True
    assert a.source_tables == frozenset({"orders", "customer"})
    assert SqlFeature.JOIN in a.features


def test_analyze_sql_parse_failure_keeps_all_features_and_flags_parse() -> None:
    # The load-bearing fallback: an unparseable query still ships every rule
    # (so the prompt isn't silently trimmed) but parse_ok records the failure
    # and the table set is empty (so any coverage check is a no-op).
    a = analyze_sql("SELECT ;;; FROM")
    assert a.parse_ok is False
    assert a.features == ALL_FEATURES
    assert a.source_tables == frozenset()


def test_analyze_sql_excludes_cte_and_derived_table_names() -> None:
    # A CTE name and a derived-table alias look like tables to a naive
    # find_all(exp.Table); the scope-based extractor must report only the real
    # underlying tables.
    cte = analyze_sql("WITH recent AS (SELECT * FROM orders) SELECT * FROM recent r JOIN customer c ON r.x = c.x")
    assert cte.source_tables == frozenset({"orders", "customer"})
    derived = analyze_sql("SELECT * FROM (SELECT * FROM orders) sub JOIN customer c ON 1 = 1")
    assert derived.source_tables == frozenset({"orders", "customer"})


def test_analyze_sql_strips_schema_qualifier_and_preserves_casing() -> None:
    a = analyze_sql('SELECT * FROM public.Orders, "Customer"')
    # Schema qualifier dropped (bare name); original casing preserved so an
    # error message echoes what the user wrote.
    assert a.source_tables == frozenset({"Orders", "Customer"})


def test_analyze_sql_no_tables_for_tableless_and_dml() -> None:
    assert analyze_sql("SELECT 1").source_tables == frozenset()
    # DML/DDL enumerate no SELECT-scope sources, so coverage never fires on them.
    assert analyze_sql("INSERT INTO persons (id) VALUES (1)").source_tables == frozenset()


_GAP_SQL = (
    "SELECT f.id, f.title, COUNT(fhm.person_id) AS member_count "
    "FROM forum f LEFT JOIN forum_has_member fhm ON fhm.forum_id = f.id "
    "GROUP BY f.id, f.title ORDER BY member_count DESC"
)


def test_analyze_sql_extracts_qualified_column_refs() -> None:
    # Qualified columns resolve to their real table; the SELECT alias
    # (member_count) must NOT appear as a column.
    assert analyze_sql(_GAP_SQL).column_refs == frozenset(
        {("forum", "id"), ("forum", "title"), ("forum_has_member", "forum_id"), ("forum_has_member", "person_id")}
    )


def test_analyze_sql_attributes_single_table_unqualified_columns() -> None:
    # In a single-table leaf scope, bare columns attribute to the sole table.
    assert analyze_sql("SELECT id, title FROM forum").column_refs == frozenset({("forum", "id"), ("forum", "title")})


def test_analyze_sql_does_not_leak_subquery_columns() -> None:
    # The leaf-scope gate: the outer scope has a child subquery, so its
    # unqualified columns are NOT attributed; `person_id` must bind to `knows`
    # (its own leaf), never to `persons`.
    refs = analyze_sql("SELECT p.full_name FROM persons p WHERE p.id IN (SELECT person_id FROM knows)").column_refs
    assert ("persons", "person_id") not in refs
    assert ("knows", "person_id") in refs
    assert ("persons", "full_name") in refs


def test_analyze_sql_no_column_refs_for_star_cte_alias_and_dml() -> None:
    assert analyze_sql("SELECT * FROM persons").column_refs == frozenset()
    assert analyze_sql("SELECT 1").column_refs == frozenset()
    assert analyze_sql("INSERT INTO persons (id) VALUES (1)").column_refs == frozenset()
    # A CTE alias is not a real table, so a column off it is excluded.
    assert analyze_sql("WITH r AS (SELECT * FROM forum) SELECT r.id FROM r").column_refs == frozenset()
