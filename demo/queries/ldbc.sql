-- Sample LDBC SNB Interactive (v1) SQL queries to translate into Cypher (or AQL)
-- using the rows2graph demo.
--
-- These queries target the LDBC SNB Interactive PostgreSQL schema as
-- implemented by graphonauts2 (`graphonauts2/src/graphonauts/adapters/postgres/
-- _shared/schema.py`): clean snake_case columns, no `p_*`/`f_*`/`m_*`
-- prefixes. They are loosely inspired by the LDBC SNB Interactive Short (IS)
-- and Complex (IC) read queries. See the official spec for the full
-- benchmark workload.


-- =============================================================================
-- SELECTION QUERIES
-- =============================================================================

-- Q1: Point lookup by primary key (Person profile, cf. LDBC IS-1).
-- Expected Cypher: MATCH (p:Person {id: 933}) RETURN ...
SELECT id, first_name, last_name, birthday, creation_date
FROM person
WHERE id = 933;

-- Q2: Range query on a timestamp column.
-- Expected Cypher: MATCH (po:Post) WHERE po.creationDate >= datetime('2010-06-01T00:00:00') ...
SELECT id, creation_date, content
FROM post
WHERE creation_date >= '2010-06-01' AND creation_date < '2010-07-01';

-- Q3: Lookup persons by first name.
SELECT id, first_name, last_name
FROM person
WHERE first_name = 'Mahinda';


-- =============================================================================
-- AGGREGATION QUERIES
-- =============================================================================

-- Q4: Top forums by member count (junction-table aggregation).
-- Cypher traversal: (f:Forum)-[:HAS_MEMBER]->(:Person)
SELECT f.id, f.title, COUNT(*) AS member_count
FROM forum f
JOIN forum_has_member fhm ON fhm.forum_id = f.id
GROUP BY f.id, f.title
ORDER BY member_count DESC
LIMIT 10;

-- Q5: Most-used tags across all posts and comments.
-- Cypher traversal: (m)-[:HAS_TAG]->(t:Tag) for both Post and Comment subtypes.
SELECT t.id, t.name, COUNT(*) AS usage_count
FROM tag t
JOIN (
  SELECT tag_id FROM post_has_tag
  UNION ALL
  SELECT tag_id FROM comment_has_tag
) mt ON mt.tag_id = t.id
GROUP BY t.id, t.name
ORDER BY usage_count DESC
LIMIT 20;


-- =============================================================================
-- JOIN / TRAVERSAL QUERIES
-- =============================================================================

-- Q6, two-hop traversal: friends of a given person (cf. LDBC IS-3).
-- Cypher: (p1:Person {id: 933})-[:KNOWS]->(p2:Person)
SELECT p2.id, p2.first_name, p2.last_name, k.creation_date AS friendship_date
FROM person p1
JOIN knows k ON k.person_id = p1.id
JOIN person p2 ON p2.id = k.friend_id
WHERE p1.id = 933;

-- Q7: Posts with their creator and tags (3-way join).
-- Cypher: (p:Person)<-[:HAS_CREATOR]-(po:Post)-[:HAS_TAG]->(t:Tag)
SELECT po.id, po.content, p.first_name, p.last_name, t.name
FROM post po
JOIN person p ON p.id = po.creator_person_id
JOIN post_has_tag pht ON pht.post_id = po.id
JOIN tag t ON t.id = pht.tag_id
WHERE po.creation_date >= '2010-06-01' AND po.creation_date < '2010-06-02';

-- Q8: Friends-of-friends who share a tag interest (4-way join, cf. IC-10).
SELECT DISTINCT p3.id, p3.first_name, p3.last_name, t.name AS shared_tag
FROM person p1
JOIN knows k1 ON k1.person_id = p1.id
JOIN knows k2 ON k2.person_id = k1.friend_id
JOIN person p3 ON p3.id = k2.friend_id
JOIN has_interest hi1 ON hi1.person_id = p1.id
JOIN has_interest hi3 ON hi3.person_id = p3.id AND hi3.tag_id = hi1.tag_id
JOIN tag t ON t.id = hi1.tag_id
WHERE p1.id = 933 AND p3.id <> p1.id;

-- Q9: Polymorphic LIKES across Post target (cf. IC-7).
-- `likes_post` and `likes_comment` are separate junction tables in this
-- schema; this query selects the Post variant.
SELECT DISTINCT liker.id AS liker_id, liker.first_name, liker.last_name,
                po.id AS post_id, po.content, l.creation_date AS liked_at
FROM person liker
JOIN likes_post l ON l.person_id = liker.id
JOIN post po ON po.id = l.post_id
JOIN person creator ON creator.id = po.creator_person_id
WHERE creator.id = 933
ORDER BY liked_at DESC
LIMIT 20;

-- Q10, REPLY_OF chain: comments replying to a specific post (cf. IS-7).
-- Cypher: (c:Comment)-[:REPLY_OF]->(:Post {id: 1099511627776})
SELECT c.id AS comment_id, c.content, c.creation_date, p.first_name, p.last_name
FROM comment c
JOIN person p ON p.id = c.creator_person_id
WHERE c.reply_of_post_id = 1099511627776
ORDER BY c.creation_date DESC;

-- Q11, optional traversal (LEFT JOIN): forums and their member count
--       including empty forums.
-- Expected Cypher: MATCH (f:Forum) OPTIONAL MATCH (f)-[:HAS_MEMBER]->(p:Person) ...
SELECT f.id, f.title, COUNT(fhm.person_id) AS member_count
FROM forum f
LEFT JOIN forum_has_member fhm ON fhm.forum_id = f.id
GROUP BY f.id, f.title
ORDER BY member_count DESC;

-- Q12, HAVING: persons with more than 5 friends.
SELECT p.id, p.first_name, p.last_name, COUNT(*) AS friend_count
FROM person p
JOIN knows k ON k.person_id = p.id
GROUP BY p.id, p.first_name, p.last_name
HAVING COUNT(*) > 5
ORDER BY friend_count DESC;


-- =============================================================================
-- SET QUERIES
-- =============================================================================

-- Q13, UNION: persons who either created OR liked any post in a forum.
SELECT DISTINCT p.id, p.first_name, p.last_name
FROM person p
JOIN post po ON po.creator_person_id = p.id
WHERE po.forum_id = 549755813888
UNION
SELECT DISTINCT p.id, p.first_name, p.last_name
FROM person p
JOIN likes_post l ON l.person_id = p.id
JOIN post po ON po.id = l.post_id
WHERE po.forum_id = 549755813888;

-- Q14, DIFFERENCE / NOT EXISTS: persons with no friends.
-- Graph equivalent: MATCH (p:Person) WHERE NOT (p)-[:KNOWS]->(:Person) RETURN ...
SELECT p.id, p.first_name, p.last_name
FROM person p
WHERE NOT EXISTS (
    SELECT 1 FROM knows k WHERE k.person_id = p.id
);
