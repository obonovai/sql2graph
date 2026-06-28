-- Sample TPC-H SQL queries to translate into Cypher (or AQL) using
-- the rows2graph demo.
--
-- These mirror the benchmark queries from the graphonauts project
-- (/src/graphonauts/postgres_db/queries.py). Use them to smoke-test
-- the demo against the tpch.yaml schema mapping.
--
-- Run one query at a time:
--
--   uv run python demo/cli.py \
--       --sql "$(sed -n '/^-- Q1 /,/^;/p' demo/queries/tpch.sql | grep -v '^--' | tr -d '\n')" \
--       --mapping config/mappings/tpch.yaml \
--       --model   config/models/ollama.yaml \
--       --target  cypher \
--       --validation syntax \
--       -v


-- =============================================================================
-- SELECTION QUERIES
-- =============================================================================

-- Q1: Point lookup on non-indexed column.
-- Expected Cypher: MATCH (s:Supplier {name: 'Supplier#000000666'}) RETURN ...
SELECT suppkey, name, address, phone
FROM supplier
WHERE name = 'Supplier#000000666';

-- Q2: Range query on a date column.
-- Expected Cypher: MATCH (li:LineItem) WHERE li.shipdate >= date('1995-03-01') ...
SELECT orderkey, linenumber, shipdate, extendedprice
FROM lineitem
WHERE shipdate >= '1995-03-01' AND shipdate <= '1995-03-31';

-- Q3: Point lookup on primary key.
SELECT suppkey, name, address, phone
FROM supplier
WHERE suppkey = 1337;


-- =============================================================================
-- AGGREGATION QUERIES
-- =============================================================================

-- Q4: COUNT grouped by a property.
-- Expected Cypher: MATCH (p:Part) RETURN p.brand AS brand, count(p) AS product_count ORDER BY product_count DESC
SELECT brand, COUNT(*) AS product_count
FROM part
GROUP BY brand
ORDER BY product_count DESC;

-- Q5: MAX grouped by a property.
SELECT brand, MAX(retailprice) AS max_price
FROM part
GROUP BY brand
ORDER BY max_price DESC;


-- =============================================================================
-- JOIN / TRAVERSAL QUERIES
-- =============================================================================

-- Q6: Join through a shared Nation, with string filter.
-- Cypher traversal: (s:Supplier)-[:LOCATED_IN]->(n:Nation)<-[:LOCATED_IN]-(c:Customer)
SELECT s.name AS supplier_name, c.name AS customer_name, c.comment AS customer_comment
FROM supplier s
JOIN customer c ON c.nationkey = s.nationkey
WHERE s.comment LIKE '%special%' AND c.comment LIKE '%special%';

-- Q7: Products with their orders (Part -> LineItem -> Order path).
SELECT p.name AS part_name, o.orderdate AS order_date, o.totalprice AS order_totalprice
FROM part p
JOIN lineitem li ON li.partkey = p.partkey
JOIN orders o ON o.orderkey = li.orderkey;

-- Q8: Complex 5-way join across customer, order, lineitem, part, supplier, nation.
SELECT c.custkey, c.name AS customer_name, n.name AS customer_nation,
       o.orderkey, o.orderdate, o.totalprice,
       li.linenumber, li.quantity, li.extendedprice,
       p.partkey, p.name AS part_name, p.brand,
       s.suppkey, s.name AS supplier_name
FROM customer c
JOIN orders o ON o.custkey = c.custkey
JOIN lineitem li ON li.orderkey = o.orderkey
JOIN part p ON p.partkey = li.partkey
JOIN supplier s ON s.suppkey = li.suppkey
JOIN nation n ON n.nationkey = c.nationkey;

-- Q9: Customers with more than one order (HAVING clause).
SELECT c.custkey, c.name AS customer_name, c.mktsegment,
       n.name AS nation_name, COUNT(o.orderkey) AS order_count
FROM customer c
JOIN orders o ON o.custkey = c.custkey
JOIN nation n ON n.nationkey = c.nationkey
GROUP BY c.custkey, c.name, c.mktsegment, n.name
HAVING COUNT(o.orderkey) > 1;

-- Q10, optional traversal (LEFT JOIN): suppliers and count of parts they supply.
-- Expected Cypher: MATCH (s:Supplier) OPTIONAL MATCH (s)-[:SUPPLIES]->(p:Part) RETURN ...
SELECT s.suppkey, s.name AS supplier_name, COUNT(ps.partkey) AS supplied_part_count
FROM supplier s
LEFT JOIN partsupp ps ON ps.suppkey = s.suppkey
GROUP BY s.suppkey, s.name;


-- =============================================================================
-- SET QUERIES
-- =============================================================================

-- Q11, UNION: phone numbers of all suppliers and customers.
SELECT name, phone FROM supplier
UNION
SELECT name, phone FROM customer;

-- Q12, DIFFERENCE: customers with no orders.
-- Graph equivalent uses: MATCH (c:Customer) WHERE NOT (c)-[:PLACED]->(:Order)
SELECT c.custkey, c.name
FROM customer c
WHERE NOT EXISTS (
    SELECT 1 FROM orders o WHERE o.custkey = c.custkey
);


-- =============================================================================
-- MODIFICATION / SHAPING QUERIES
-- =============================================================================

-- Q13: Sorting.
SELECT partkey, name, brand, retailprice
FROM part
ORDER BY brand, retailprice DESC;

-- Q14: DISTINCT across a join.
SELECT DISTINCT p.brand, n.name AS supplier_nation
FROM part p
JOIN partsupp ps ON ps.partkey = p.partkey
JOIN supplier s ON s.suppkey = ps.suppkey
JOIN nation n ON n.nationkey = s.nationkey;
