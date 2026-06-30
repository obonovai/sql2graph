"""Static tests for the schema-mapping builder (DDL -> SchemaMapping).

No network and no real LLM: the optional refinement pass is exercised with
in-process fake :class:`~rows2graph.llm.LLMClient` doubles. The tests pin the
DDL extraction, the relational-to-graph heuristics (especially junction-table
detection and key choice), the validity-by-construction guarantee, closeness to
the hand-authored ``tpch.yaml``, YAML round-tripping, and the anti-hallucination
guardrail on the LLM pass.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from rows2graph import EdgeMapping, NodeMapping, SchemaMapping, build_mapping, build_mapping_async
from rows2graph.llm.usage import ChatReply, TokenUsage
from rows2graph.mapping_builder import mapping_to_yaml
from rows2graph.mapping_builder.ddl import DdlParseError, extract_schema_from_ddl
from rows2graph.mapping_builder.diff import diff_mappings
from rows2graph.mapping_builder.naming import (
    edge_type_for_fk,
    junction_to_edge_type,
    table_to_label,
)
from rows2graph.mapping_builder.project import (
    CoverageReport,
    choose_primary_key,
    is_junction_table,
    project_to_mapping,
)
from rows2graph.mapping_builder.refine import refine_mapping, refine_mapping_async, validate_against_schema
from rows2graph.mapping_builder.relational import ForeignKey

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "mappings"

# A graphonauts-style TPC-H schema whose column names match the shipped
# config/mappings/tpch.yaml (bare keys: regionkey, partkey, ...), so the
# generated skeleton can be compared against the hand-authored mapping.
TPCH_DDL = """
CREATE TABLE region (regionkey INT PRIMARY KEY, name VARCHAR(25), comment VARCHAR(152));
CREATE TABLE nation (nationkey INT PRIMARY KEY, name VARCHAR(25), comment VARCHAR(152), regionkey INT REFERENCES region(regionkey));
CREATE TABLE supplier (suppkey INT PRIMARY KEY, name VARCHAR(25), address VARCHAR(40), phone VARCHAR(15), acctbal DECIMAL(15,2), comment VARCHAR(101), nationkey INT REFERENCES nation(nationkey));
CREATE TABLE customer (custkey INT PRIMARY KEY, name VARCHAR(25), address VARCHAR(40), phone VARCHAR(15), acctbal DECIMAL(15,2), mktsegment VARCHAR(10), comment VARCHAR(117), nationkey INT REFERENCES nation(nationkey));
CREATE TABLE part (partkey INT PRIMARY KEY, name VARCHAR(55), mfgr VARCHAR(25), brand VARCHAR(10), type VARCHAR(25), size INT, container VARCHAR(10), retailprice DECIMAL(15,2), comment VARCHAR(23));
CREATE TABLE orders (orderkey INT PRIMARY KEY, orderstatus CHAR(1), totalprice DECIMAL(15,2), orderdate DATE, orderpriority VARCHAR(15), clerk VARCHAR(15), shippriority INT, comment VARCHAR(79), custkey INT REFERENCES customer(custkey));
CREATE TABLE lineitem (
  orderkey INT REFERENCES orders(orderkey),
  partkey INT REFERENCES part(partkey),
  suppkey INT REFERENCES supplier(suppkey),
  linenumber INT, quantity DECIMAL(15,2), extendedprice DECIMAL(15,2), discount DECIMAL(15,2), tax DECIMAL(15,2),
  returnflag CHAR(1), linestatus CHAR(1), shipdate DATE, commitdate DATE, receiptdate DATE,
  shipinstruct VARCHAR(25), shipmode VARCHAR(10), comment VARCHAR(44),
  PRIMARY KEY (orderkey, linenumber)
);
CREATE TABLE partsupp (
  partkey INT REFERENCES part(partkey),
  suppkey INT REFERENCES supplier(suppkey),
  availqty INT, supplycost DECIMAL(15,2), comment VARCHAR(199),
  PRIMARY KEY (partkey, suppkey)
);
"""


# ---------------------------------------------------------------------------
# DDL extraction
# ---------------------------------------------------------------------------


def test_extract_inline_pk_and_not_null() -> None:
    schema = extract_schema_from_ddl("CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(5) NOT NULL, note TEXT);")
    table = schema.table("t")
    assert table is not None
    assert table.primary_key == ("id",)
    assert table.column_names() == ["id", "name", "note"]
    nullable = {c.name: c.nullable for c in table.columns}
    assert nullable == {"id": True, "name": False, "note": True}


def test_extract_table_level_and_composite_pk() -> None:
    schema = extract_schema_from_ddl(
        "CREATE TABLE t (a INT, b INT, c INT, PRIMARY KEY (a, b));"
    )
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
    schema = extract_schema_from_ddl(
        "CREATE TABLE t (id INT PRIMARY KEY); CREATE VIEW v AS SELECT 1;"
    )
    assert schema.table("v") is None
    assert schema.table_names() == {"t"}
    assert [(o.name, o.kind) for o in schema.skipped_objects] == [("v", "view")]


def test_extract_self_referential_fk_and_omitted_ref_columns() -> None:
    schema = extract_schema_from_ddl(
        "CREATE TABLE node (id INT PRIMARY KEY, parent_id INT REFERENCES node);"
    )
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
    assert any(e.source_table == "nation" and e.source_foreign_key == "regionkey" for e in mapping.edges)


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


# ---------------------------------------------------------------------------
# Naming heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "label"),
    [
        ("region", "Region"),
        ("orders", "Order"),
        ("line_item", "LineItem"),
        ("line_items", "LineItem"),
        ("categories", "Category"),
        ("boxes", "Box"),
        ("people", "Person"),
        ("status", "Status"),  # stop-set: not a plural
        ("series", "Series"),
        ("address", "Address"),
        ("forum_has_member", "ForumHasMember"),
        ("lineitem", "Lineitem"),  # documented gap: no separator to split on
    ],
)
def test_table_to_label(table: str, label: str) -> None:
    assert table_to_label(table) == label


def test_table_to_label_non_ascii_falls_back_to_identifier() -> None:
    # All-non-ASCII or all-symbol names have no ASCII tokens to PascalCase; the
    # label must still be non-blank or NodeMapping's NonBlankStr would reject it.
    assert table_to_label("用户") == "用户"  # CJK
    assert table_to_label("_") == "_"


def test_edge_type_for_fk() -> None:
    assert edge_type_for_fk(ForeignKey(("regionkey",), "region"), target_label="Region") == "HAS_REGION"
    assert edge_type_for_fk(ForeignKey(("moderator_person_id",), "person"), target_label="Person") == "MODERATOR_PERSON"
    assert edge_type_for_fk(ForeignKey(("reply_of_comment_id",), "comment"), target_label="Comment") == "REPLY_OF_COMMENT"
    assert edge_type_for_fk(ForeignKey(("id",), "person"), target_label="Person") == "HAS_PERSON"


def test_junction_to_edge_type() -> None:
    assert junction_to_edge_type("knows") == "KNOWS"
    assert junction_to_edge_type("study_at") == "STUDY_AT"


# ---------------------------------------------------------------------------
# Junction-table predicate
# ---------------------------------------------------------------------------


def test_junction_detected_for_association_table() -> None:
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    partsupp = schema.table("partsupp")
    assert partsupp is not None
    assert is_junction_table(partsupp, schema) is True


def test_junction_detected_for_self_association() -> None:
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE person (id INT PRIMARY KEY);
        CREATE TABLE knows (a INT REFERENCES person(id), b INT REFERENCES person(id), since DATE, PRIMARY KEY (a, b));
        """
    )
    knows = schema.table("knows")
    assert knows is not None
    assert is_junction_table(knows, schema) is True


def test_surrogate_key_two_fk_table_is_not_a_junction() -> None:
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE person (id INT PRIMARY KEY);
        CREATE TABLE organisation (id INT PRIMARY KEY);
        CREATE TABLE study_at (id INT PRIMARY KEY, person_id INT REFERENCES person(id),
            organisation_id INT REFERENCES organisation(id), class_year INT);
        """
    )
    study_at = schema.table("study_at")
    assert study_at is not None
    assert is_junction_table(study_at, schema) is False


def test_referenced_table_is_never_a_junction() -> None:
    # `link` looks like a junction, but `child` references it, so it must stay a node.
    schema = extract_schema_from_ddl(
        """
        CREATE TABLE a (id INT PRIMARY KEY);
        CREATE TABLE b (id INT PRIMARY KEY);
        CREATE TABLE link (aid INT REFERENCES a(id), bid INT REFERENCES b(id), PRIMARY KEY (aid, bid));
        CREATE TABLE child (id INT PRIMARY KEY, link_aid INT REFERENCES link(aid));
        """
    )
    link = schema.table("link")
    assert link is not None
    assert is_junction_table(link, schema) is False


# ---------------------------------------------------------------------------
# Key choice and projection validity
# ---------------------------------------------------------------------------


def test_choose_primary_key_prefers_non_fk_of_composite() -> None:
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    lineitem = schema.table("lineitem")
    assert lineitem is not None
    assert choose_primary_key(lineitem) == ("linenumber", False)


def test_choose_primary_key_synthesizes_when_absent() -> None:
    table = extract_schema_from_ddl("CREATE TABLE t (a INT, b INT);").table("t")
    assert table is not None
    assert choose_primary_key(table) == ("a", True)


def test_projection_is_valid_by_construction() -> None:
    # project_to_mapping builds straight into SchemaMapping, so a bad projection
    # would raise pydantic ValidationError here rather than returning.
    result = project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres"))
    assert isinstance(result.mapping, SchemaMapping)
    assert {n.source_table for n in result.mapping.nodes} == {
        "region", "nation", "supplier", "customer", "part", "orders", "lineitem"
    }
    assert result.report.edge_tables == ["partsupp"]


def test_label_collision_is_disambiguated() -> None:
    # `order` and `orders` both singularize/pascal to `Order`; the mapping must
    # still be valid (unique labels), with the collision flagged.
    result = project_to_mapping(
        extract_schema_from_ddl(
            "CREATE TABLE order (id INT PRIMARY KEY); CREATE TABLE orders (id INT PRIMARY KEY);"
        )
    )
    labels = sorted(n.label for n in result.mapping.nodes)
    assert labels == ["Order", "Order2"]
    assert any("collision" in w.lower() for w in result.report.warnings)


def test_node_properties_exclude_fk_columns_but_keep_pk() -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres")).mapping
    supplier = next(n for n in mapping.nodes if n.source_table == "supplier")
    assert "nationkey" not in supplier.properties  # FK -> becomes an edge
    assert supplier.primary_key == "suppkey"
    assert "suppkey" in supplier.properties


def test_projection_duplicate_bare_name_is_valid_not_crash() -> None:
    # Two schema-qualified tables collapsing to the same bare name must not crash
    # the projection (it used to raise a duplicate-label ValidationError).
    result = project_to_mapping(
        extract_schema_from_ddl(
            "CREATE TABLE sales.orders (id INT PRIMARY KEY); CREATE TABLE archive.orders (id INT PRIMARY KEY);"
        )
    )
    assert isinstance(result.mapping, SchemaMapping)
    assert [n.label for n in result.mapping.nodes] == ["Order"]


def test_projection_junction_to_empty_table_drops_edge_not_crash() -> None:
    # A junction whose referenced table has no columns (dropped from nodes) must
    # not raise KeyError; the edge is dropped and the reason recorded.
    result = project_to_mapping(
        extract_schema_from_ddl(
            "CREATE TABLE a (LIKE x); CREATE TABLE b (id INT PRIMARY KEY); "
            "CREATE TABLE j (aid INT REFERENCES a(id), bid INT REFERENCES b(id), PRIMARY KEY (aid, bid));"
        )
    )
    assert isinstance(result.mapping, SchemaMapping)
    assert result.mapping.edges == []
    assert any("junction references a table that is not a node" in reason for _, reason in result.report.dropped_objects)


def test_projection_non_ascii_table_name_yields_non_blank_label() -> None:
    result = project_to_mapping(extract_schema_from_ddl('CREATE TABLE "用户" (id INT PRIMARY KEY);'))
    assert isinstance(result.mapping, SchemaMapping)
    assert all(n.label for n in result.mapping.nodes)  # no blank label (would fail NonBlankStr)


# ---------------------------------------------------------------------------
# Closeness to the hand-authored tpch.yaml
# ---------------------------------------------------------------------------


def test_generated_tpch_matches_shipped_join_semantics() -> None:
    generated = project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres"))
    shipped = SchemaMapping.from_yaml(_CONFIG / "tpch.yaml")
    junction_tables = set(generated.report.edge_tables)

    gen_nodes = {n.source_table for n in generated.mapping.nodes}
    ship_nodes = {n.source_table for n in shipped.nodes}
    assert gen_nodes == ship_nodes

    def direct_triples(mapping: SchemaMapping) -> set[tuple[str, str, str]]:
        return {
            (e.source_table, e.source_foreign_key, e.target_primary_key)
            for e in mapping.edges
            if e.source_table not in junction_tables and e.source_table != "partsupp"
        }

    # Direct foreign-key edges agree exactly on join semantics (type names and
    # direction are deliberately ignored - those are the LLM pass's concern).
    assert direct_triples(generated.mapping) == direct_triples(shipped)


def test_generated_tpch_junction_edge_matches_shipped() -> None:
    generated = project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres")).mapping
    supplies = next(e for e in generated.edges if e.source_table == "partsupp")
    assert {supplies.source_node, supplies.target_node} == {"Supplier", "Part"}
    assert set(supplies.properties) == {"availqty", "supplycost", "comment"}


def test_bundled_tpch_ddl_matches_shipped() -> None:
    # The bundled example (config/ddl/tpch.sql) is what the CLI and the web modal's
    # "Load example" feed in. It must parse with NO dialect (the modal's "Generic
    # SQL" default) and stay structurally faithful to the hand-authored tpch.yaml,
    # so the example can't silently drift from the mapping it is meant to reproduce.
    ddl_path = _CONFIG.parent / "ddl" / "tpch.sql"
    generated = project_to_mapping(extract_schema_from_ddl(ddl_path.read_text())).mapping  # dialect=None
    shipped = SchemaMapping.from_yaml(_CONFIG / "tpch.yaml")

    assert {n.source_table for n in generated.nodes} == {n.source_table for n in shipped.nodes}

    def direct_triples(mapping: SchemaMapping) -> set[tuple[str, str, str]]:
        return {
            (e.source_table, e.source_foreign_key, e.target_primary_key)
            for e in mapping.edges
            if e.source_table != "partsupp"
        }

    assert direct_triples(generated) == direct_triples(shipped)


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["tpch.yaml", "ldbc.yaml"])
def test_shipped_mapping_round_trips(name: str) -> None:
    mapping = SchemaMapping.from_yaml(_CONFIG / name)
    assert SchemaMapping.from_yaml_string(mapping_to_yaml(mapping)) == mapping


def test_generated_mapping_round_trips() -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres")).mapping
    assert SchemaMapping.from_yaml_string(mapping_to_yaml(mapping)) == mapping


def test_empty_edge_properties_are_omitted() -> None:
    mapping = project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres")).mapping
    yaml_text = mapping_to_yaml(mapping)
    # The BELONGS_TO-style direct edges carry no properties; partsupp does.
    assert "availqty" in yaml_text
    # A direct edge block should not emit an empty "properties: {}".
    assert "properties: {}" not in yaml_text


# ---------------------------------------------------------------------------
# LLM refinement + guardrail
# ---------------------------------------------------------------------------


class _FakeLLM:
    """A one-shot LLM double returning a canned reply (or raising)."""

    def __init__(self, reply: str | None = None, *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> ChatReply:  # noqa: ARG002
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        return ChatReply(text=self._reply, usage=TokenUsage())

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


def _tpch_skeleton() -> SchemaMapping:
    return project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres")).mapping


def test_refine_applies_valid_rename() -> None:
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    improved_yaml = mapping_to_yaml(skeleton).replace("HAS_REGION", "IN_REGION").replace("label: Lineitem", "label: LineItem")
    # keep edges that reference the renamed label consistent
    improved_yaml = improved_yaml.replace("source_node: Lineitem", "source_node: LineItem")
    outcome = refine_mapping(skeleton, schema, _FakeLLM(improved_yaml))
    assert outcome.accepted is True
    assert outcome.warnings == []
    assert "IN_REGION" in {e.type for e in outcome.mapping.edges}
    assert "LineItem" in {n.label for n in outcome.mapping.nodes}
    # the transcript carries the chat the modal renders
    roles = [m["role"] for m in outcome.messages]
    assert roles[:2] == ["system", "user"]
    assert "assistant" in roles


def test_refine_strips_code_fences() -> None:
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    fenced = "```yaml\n" + mapping_to_yaml(skeleton) + "\n```"
    outcome = refine_mapping(skeleton, schema, _FakeLLM(fenced))
    assert outcome.accepted is True
    assert outcome.warnings == []
    assert outcome.mapping == skeleton


def test_refine_rejects_hallucinated_column_and_falls_back() -> None:
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    bad = mapping_to_yaml(skeleton).replace("primary_key: regionkey", "primary_key: not_a_column")
    outcome = refine_mapping(skeleton, schema, _FakeLLM(bad))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("not_a_column" in w for w in outcome.warnings)
    # even a rejected attempt is shown in the transcript
    assert any(m["role"] == "assistant" for m in outcome.messages)


def test_refine_rejects_dropped_table_coverage_regression() -> None:
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    # Drop the Region node entirely but keep edges valid by also dropping its edge.
    smaller = SchemaMapping(
        nodes=[n for n in skeleton.nodes if n.source_table != "region"],
        edges=[e for e in skeleton.edges if e.target_node != "Region"],
    )
    outcome = refine_mapping(skeleton, schema, _FakeLLM(mapping_to_yaml(smaller)))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("region" in w.lower() for w in outcome.warnings)


def test_refine_falls_back_on_malformed_yaml() -> None:
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    outcome = refine_mapping(skeleton, schema, _FakeLLM("this: is: not: valid: mapping"))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert outcome.warnings  # explains the fallback


def test_refine_falls_back_when_llm_errors() -> None:
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    outcome = refine_mapping(skeleton, schema, _FakeLLM(error=RuntimeError("boom")))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("boom" in w for w in outcome.warnings)
    # transcript still records what we tried to send
    assert [m["role"] for m in outcome.messages] == ["system", "user"]


def test_refine_rejects_swapped_foreign_key_column() -> None:
    # The LLM repoints an FK to another column that *exists* on the table. The
    # existence-only guardrail accepted this; the preservation check must reject it.
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    swapped = mapping_to_yaml(skeleton).replace("source_foreign_key: regionkey", "source_foreign_key: name")
    outcome = refine_mapping(skeleton, schema, _FakeLLM(swapped))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("SQL side changed" in w for w in outcome.warnings)


def test_refine_rejects_swapped_property_column() -> None:
    # A node property value is repointed from one real column to another real one.
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    swapped = mapping_to_yaml(skeleton).replace("name: name", "name: comment")
    outcome = refine_mapping(skeleton, schema, _FakeLLM(swapped))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton


def test_refine_rejects_added_edge() -> None:
    # A spurious but identifier-valid relationship the LLM invents must be rejected
    # (every column exists, so only the preservation check catches it).
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    with_extra = SchemaMapping(
        nodes=list(skeleton.nodes),
        edges=[
            *skeleton.edges,
            EdgeMapping(
                type="BOGUS",
                source_node="Supplier",
                target_node="Supplier",
                source_table="supplier",
                source_foreign_key="suppkey",
                target_primary_key="suppkey",
            ),
        ],
    )
    outcome = refine_mapping(skeleton, schema, _FakeLLM(mapping_to_yaml(with_extra)))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton


def test_diff_mappings_detects_label_and_edge_renames() -> None:
    skeleton = _tpch_skeleton()
    renamed = (
        mapping_to_yaml(skeleton)
        .replace("label: Lineitem", "label: LineItem")
        .replace("source_node: Lineitem", "source_node: LineItem")
        .replace("HAS_REGION", "IN_REGION")
    )
    diff = diff_mappings(skeleton, SchemaMapping.from_yaml_string(renamed))
    assert not diff.is_empty()
    assert ("Lineitem", "LineItem") in {(r.before, r.after) for r in diff.label_renames}
    assert ("HAS_REGION", "IN_REGION") in {(r.before, r.after) for r in diff.edge_type_renames}


def test_diff_mappings_detects_property_rename() -> None:
    skeleton = _tpch_skeleton()
    # Rename Region's property key 'name' -> 'title'; the SQL column value stays 'name'.
    after = SchemaMapping(
        nodes=[
            NodeMapping(
                label=n.label,
                source_table=n.source_table,
                primary_key=n.primary_key,
                properties=(
                    {("title" if k == "name" else k): v for k, v in n.properties.items()}
                    if n.source_table == "region"
                    else n.properties
                ),
            )
            for n in skeleton.nodes
        ],
        edges=list(skeleton.edges),
    )
    diff = diff_mappings(skeleton, after)
    assert any(r.before == "name" and r.after == "title" for r in diff.property_renames)


def test_diff_mappings_empty_for_identical() -> None:
    skeleton = _tpch_skeleton()
    assert diff_mappings(skeleton, skeleton).is_empty()


def test_validate_against_schema_flags_unknown_table() -> None:
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    bogus = SchemaMapping(
        nodes=[{"label": "X", "source_table": "ghost", "properties": {"k": "k"}, "primary_key": "k"}],  # type: ignore[list-item]
        edges=[],
    )
    violations = validate_against_schema(bogus, schema)
    assert any("ghost" in v for v in violations)


# ---------------------------------------------------------------------------
# build_mapping facade
# ---------------------------------------------------------------------------


def test_build_mapping_deterministic() -> None:
    result = build_mapping(ddl=TPCH_DDL, dialect="postgres")
    assert result.refined is False
    assert result.warnings == []
    assert SchemaMapping.from_yaml_string(result.yaml) == result.mapping
    assert result.report.as_dict()["node_count"] == 7
    # No LLM ran: no conversation, no diff, and the "original" equals the result.
    assert result.conversation == []
    assert result.diff is None
    assert result.skeleton_yaml == result.yaml


def test_build_mapping_with_llm_refines() -> None:
    skeleton = _tpch_skeleton()
    improved = mapping_to_yaml(skeleton).replace("HAS_REGION", "IN_REGION")
    result = build_mapping(ddl=TPCH_DDL, dialect="postgres", llm=_FakeLLM(improved))
    assert result.refined is True
    assert "IN_REGION" in {e.type for e in result.mapping.edges}
    # The refinement is now transparent: original kept, chat captured, diff computed.
    assert result.skeleton_yaml == mapping_to_yaml(skeleton)
    assert result.skeleton_yaml != result.yaml
    assert result.conversation and result.conversation[0]["role"] == "system"
    assert result.diff is not None
    assert ("HAS_REGION", "IN_REGION") in {(r.before, r.after) for r in result.diff.edge_type_renames}


def test_report_as_dict_lists_junctions_and_warnings() -> None:
    report: CoverageReport = project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres")).report
    data = report.as_dict()
    assert data["edge_tables"] == ["partsupp"]
    assert isinstance(data["warnings"], list)


# ---------------------------------------------------------------------------
# CLI smoke (build-mapping subcommand)
# ---------------------------------------------------------------------------


def _load_cli() -> Any:
    spec = importlib.util.spec_from_file_location(
        "demo_cli", Path(__file__).resolve().parent.parent / "demo" / "cli.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_build_mapping_writes_loadable_yaml(tmp_path: Path) -> None:
    cli = _load_cli()
    ddl_path = tmp_path / "schema.sql"
    ddl_path.write_text(TPCH_DDL)
    out_path = tmp_path / "mapping.yaml"
    code = cli.main(["build-mapping", "--ddl", str(ddl_path), "--dialect", "postgres", "-o", str(out_path)])
    assert code == 0
    mapping = SchemaMapping.from_yaml(out_path)
    assert len(mapping.nodes) == 7


def test_cli_build_mapping_rejects_bad_ddl(tmp_path: Path) -> None:
    cli = _load_cli()
    ddl_path = tmp_path / "bad.sql"
    ddl_path.write_text("CREATE TABLE (((")
    with pytest.raises(SystemExit) as exc:
        cli.main(["build-mapping", "--ddl", str(ddl_path)])
    assert exc.value.code == 2


def test_cli_build_mapping_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    cli = _load_cli()
    ddl_path = tmp_path / "schema.sql"
    ddl_path.write_text(TPCH_DDL)
    out_path = tmp_path / "mapping.yaml"
    out_path.write_text("existing")
    with pytest.raises(SystemExit) as exc:
        cli.main(["build-mapping", "--ddl", str(ddl_path), "--dialect", "postgres", "-o", str(out_path)])
    assert exc.value.code == 2
    assert out_path.read_text() == "existing"  # left untouched
    # --force overwrites.
    code = cli.main(["build-mapping", "--ddl", str(ddl_path), "--dialect", "postgres", "-o", str(out_path), "--force"])
    assert code == 0
    assert SchemaMapping.from_yaml(out_path)  # now a real mapping


# ---------------------------------------------------------------------------
# Async streaming refine (drives the coroutines via asyncio.run)
# ---------------------------------------------------------------------------


class _FakeAsyncLLM:
    """An async LLM double that streams a canned reply in two chunks."""

    def __init__(self, reply: str | None = None, *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error

    async def chat(
        self,
        messages: list[dict[str, Any]],  # noqa: ARG002
        *,
        stream_to: Any = None,
        temperature: float | None = None,  # noqa: ARG002
    ) -> ChatReply:
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        if stream_to is not None:
            mid = len(self._reply) // 2
            stream_to(self._reply[:mid])
            stream_to(self._reply[mid:])
        return ChatReply(text=self._reply, usage=TokenUsage())

    async def close(self) -> None:  # pragma: no cover - nothing to release
        pass


def test_build_mapping_async_streams_and_matches_sync() -> None:
    skeleton = _tpch_skeleton()
    improved = mapping_to_yaml(skeleton).replace("HAS_REGION", "IN_REGION")
    snapshots: list[list[dict[str, str]]] = []
    result = asyncio.run(
        build_mapping_async(
            ddl=TPCH_DDL,
            dialect="postgres",
            llm=_FakeAsyncLLM(improved),
            on_conversation=snapshots.append,
        )
    )
    assert result.refined is True
    assert "IN_REGION" in {e.type for e in result.mapping.edges}
    assert result.diff is not None
    assert result.conversation and result.conversation[0]["role"] == "system"

    # The assistant turn streamed in: snapshots whose last message is the assistant
    # grow monotonically (partial chunks then the full reply).
    assistant_lens = [len(s[-1]["content"]) for s in snapshots if s and s[-1]["role"] == "assistant"]
    assert len(assistant_lens) >= 2
    assert assistant_lens == sorted(assistant_lens)

    # The async path produces the same result as the sync path.
    sync_result = build_mapping(ddl=TPCH_DDL, dialect="postgres", llm=_FakeLLM(improved))
    assert result.yaml == sync_result.yaml
    assert result.skeleton_yaml == sync_result.skeleton_yaml
    assert result.diff.as_dict() == sync_result.diff.as_dict()  # type: ignore[union-attr]


def test_build_mapping_async_deterministic_without_llm() -> None:
    result = asyncio.run(build_mapping_async(ddl=TPCH_DDL, dialect="postgres"))
    assert result.refined is False
    assert result.conversation == []
    assert result.diff is None
    assert result.skeleton_yaml == result.yaml


def test_refine_mapping_async_falls_back_when_llm_errors() -> None:
    skeleton = _tpch_skeleton()
    schema = extract_schema_from_ddl(TPCH_DDL, dialect="postgres")
    outcome = asyncio.run(refine_mapping_async(skeleton, schema, _FakeAsyncLLM(error=RuntimeError("boom"))))
    assert outcome.accepted is False
    assert outcome.mapping == skeleton
    assert any("boom" in w for w in outcome.warnings)
