SELECT c.custkey, c.name
FROM customer c
WHERE NOT EXISTS (
    SELECT 1 FROM orders o WHERE o.custkey = c.custkey
);
