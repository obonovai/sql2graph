"""DDL extraction: parsing CREATE/ALTER TABLE into a relational schema."""

from __future__ import annotations

import pytest

from sql2graph.mapping_builder.ddl import DdlParseError, extract_schema_from_ddl
from sql2graph.mapping_builder.project import project_to_mapping


def test_extract_inline_pk_and_not_null() -> None:
    schema = extract_schema_from_ddl("CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(5) NOT NULL, note TEXT);")
    table = schema.table("t")
    assert table is not None
    assert table.primary_key == ("id",)
    assert table.column_names() == ["id", "name", "note"]
    nullable = {c.name: c.nullable for c in table.columns}
    assert nullable == {"id": True, "name": False, "note": True}


def test_extract_table_level_and_composite_pk() -> None:
    schema = extract_schema_from_ddl("CREATE TABLE t (a INT, b INT, c INT, PRIMARY KEY (a, b));")
    table = schema.table("t")
    assert table is not None
    assert table.primary_key == ("a", "b")


def test_extract_inline_table_level_and_named_foreign_keys() -> None:
    ddl = """
    CREATE TABLE parent (id INT PRIMARY KEY);
    CREATE TABLE child (
      pid INT REFERENCES parent(id),
      qid INT,
      rid INT,
      FOREIGN KEY (qid) REFERENCES parent(id),
      CONSTRAINT fk_r FOREIGN KEY (rid) REFERENCES parent(id)
    );
    """
    child = extract_schema_from_ddl(ddl).table("child")
    assert child is not None
    by_col = {fk.columns[0]: fk for fk in child.foreign_keys}
    assert set(by_col) == {"pid", "qid", "rid"}
    assert all(fk.ref_table == "parent" and fk.ref_columns == ("id",) for fk in child.foreign_keys)
    assert by_col["rid"].name == "fk_r"  # named constraint preserved


def test_extract_drops_schema_qualifier_and_preserves_casing() -> None:
    schema = extract_schema_from_ddl('CREATE TABLE public."MyTbl" (Id INT PRIMARY KEY);')
    table = schema.table("MyTbl")
    assert table is not None
    assert table.name == "MyTbl"  # bare name, original casing
    assert table.schema == "public"
    assert table.primary_key == ("Id",)


def test_extract_skips_views_and_records_them() -> None:
    schema = extract_schema_from_ddl("CREATE TABLE t (id INT PRIMARY KEY); CREATE VIEW v AS SELECT 1;")
    assert schema.table("v") is None
    assert schema.table_names() == {"t"}
    assert [(o.name, o.kind) for o in schema.skipped_objects] == [("v", "view")]


def test_extract_self_referential_fk_and_omitted_ref_columns() -> None:
    schema = extract_schema_from_ddl("CREATE TABLE node (id INT PRIMARY KEY, parent_id INT REFERENCES node);")
    table = schema.table("node")
    assert table is not None
    fk = table.foreign_keys[0]
    assert fk.columns == ("parent_id",) and fk.ref_table == "node" and fk.ref_columns == ()


def test_extract_no_pk_table() -> None:
    table = extract_schema_from_ddl("CREATE TABLE t (a INT, b INT);").table("t")
    assert table is not None
    assert table.primary_key == ()


def test_extract_parse_error_raises_ddlparseerror() -> None:
    with pytest.raises(DdlParseError):
        extract_schema_from_ddl("CREATE TABLE (((((")


def test_extract_unterminated_string_raises_ddlparseerror() -> None:
    # A tokenizer-level failure (sqlglot TokenError, which is NOT a ParseError)
    # must still surface as DdlParseError, not an uncaught error that the web API
    # would turn into an HTTP 500 instead of a clean 400.
    with pytest.raises(DdlParseError):
        extract_schema_from_ddl("CREATE TABLE t (a VARCHAR(5) DEFAULT 'oops);")


def test_extract_alter_table_adds_primary_and_foreign_keys() -> None:
    # pg_dump / migration style: define every table first, then add keys via
    # ALTER TABLE. These used to be dropped silently (no edge, no audit record).
    ddl = """
    CREATE TABLE region (regionkey INT, name VARCHAR(25));
    CREATE TABLE nation (nationkey INT, name VARCHAR(25), regionkey INT);
    ALTER TABLE ONLY region ADD CONSTRAINT region_pkey PRIMARY KEY (regionkey);
    ALTER TABLE ONLY nation ADD CONSTRAINT nation_pkey PRIMARY KEY (nationkey);
    ALTER TABLE ONLY nation ADD CONSTRAINT nation_region_fk FOREIGN KEY (regionkey) REFERENCES region(regionkey);
    """
    schema = extract_schema_from_ddl(ddl)
    region = schema.table("region")
    nation = schema.table("nation")
    assert region is not None and region.primary_key == ("regionkey",)
    assert nation is not None and nation.primary_key == ("nationkey",)
    assert [(fk.columns, fk.ref_table, fk.name) for fk in nation.foreign_keys] == [
        (("regionkey",), "region", "nation_region_fk")
    ]
    # End to end: the ALTER-declared FK now becomes an edge.
    mapping = project_to_mapping(schema).mapping
    assert any(e.source_table == "nation" and e.source_foreign_key == ["regionkey"] for e in mapping.edges)


def test_extract_alter_table_unnamed_foreign_key() -> None:
    ddl = """
    CREATE TABLE a (id INT PRIMARY KEY);
    CREATE TABLE b (id INT PRIMARY KEY, aid INT);
    ALTER TABLE b ADD FOREIGN KEY (aid) REFERENCES a(id);
    """
    b = extract_schema_from_ddl(ddl).table("b")
    assert b is not None
    assert [(fk.columns, fk.ref_table, fk.name) for fk in b.foreign_keys] == [(("aid",), "a", None)]


def test_extract_alter_against_unknown_table_is_recorded() -> None:
    ddl = "CREATE TABLE a (id INT PRIMARY KEY); ALTER TABLE ghost ADD CONSTRAINT f FOREIGN KEY (x) REFERENCES a(id);"
    schema = extract_schema_from_ddl(ddl)
    assert schema.table_names() == {"a"}
    assert any(o.name == "ghost" and "not defined" in o.reason for o in schema.skipped_objects)


def test_extract_ctas_is_recorded_as_skipped() -> None:
    schema = extract_schema_from_ddl("CREATE TABLE a (id INT PRIMARY KEY); CREATE TABLE b AS SELECT 1 AS x;")
    assert schema.table_names() == {"a"}
    assert any(o.name == "b" and o.kind == "table" for o in schema.skipped_objects)


def test_extract_duplicate_bare_name_keeps_first_and_records_it() -> None:
    # Schema qualifiers are dropped, so two qualified tables can collapse to one
    # bare name; keep the first definition rather than letting both reach the
    # projection (which would crash on duplicate labels).
    schema = extract_schema_from_ddl(
        "CREATE TABLE sales.orders (id INT PRIMARY KEY, a INT); CREATE TABLE archive.orders (id INT PRIMARY KEY, b INT);"
    )
    assert [t.name for t in schema.tables] == ["orders"]
    kept = schema.table("orders")
    assert kept is not None and kept.column_names() == ["id", "a"]  # the first definition
    assert any("duplicate table name" in o.reason for o in schema.skipped_objects)
