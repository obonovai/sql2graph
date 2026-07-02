"""Fixtures for the schema-mapping builder tests.

The fake LLM clients are exposed as factory-fixtures returning the double
*class* (``oneshot_llm(reply)`` / ``oneshot_llm(error=exc)``). ``tpch_ddl`` is a
value fixture carrying the shared TPC-H DDL string; ``tpch_skeleton`` is a
factory that projects it into a :class:`SchemaMapping`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from rows2graph import SchemaMapping
from rows2graph.mapping_builder.ddl import extract_schema_from_ddl
from rows2graph.mapping_builder.project import project_to_mapping
from tests.unit._doubles import OneShotAsyncLLM, OneShotLLM

# A graphonauts-style TPC-H schema whose column names match the shipped
# examples/mappings/tpch.yaml (bare keys: regionkey, partkey, ...), so the
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


@pytest.fixture
def oneshot_llm() -> type[OneShotLLM]:
    """Factory for the sync one-shot LLM double: ``oneshot_llm(reply)`` / ``oneshot_llm(error=exc)``."""
    return OneShotLLM


@pytest.fixture
def oneshot_async_llm() -> type[OneShotAsyncLLM]:
    """Factory for the async one-shot LLM double: ``oneshot_async_llm(reply)``."""
    return OneShotAsyncLLM


@pytest.fixture
def tpch_ddl() -> str:
    """The shared TPC-H DDL string used across the builder tests."""
    return TPCH_DDL


@pytest.fixture
def tpch_skeleton() -> Callable[[], SchemaMapping]:
    """Factory that projects :data:`TPCH_DDL` into a skeleton :class:`SchemaMapping`."""

    def _build() -> SchemaMapping:
        return project_to_mapping(extract_schema_from_ddl(TPCH_DDL, dialect="postgres")).mapping

    return _build
