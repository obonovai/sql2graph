SELECT s.name AS supplier_name, c.name AS customer_name, c.comment AS customer_comment
FROM supplier s
JOIN customer c ON c.nationkey = s.nationkey
WHERE s.comment LIKE '%special%' AND c.comment LIKE '%special%';
