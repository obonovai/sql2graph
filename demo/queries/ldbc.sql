-- Sample LDBC SNB Interactive (v1) SQL queries to translate into Cypher (or AQL)
-- using the rows2graph demo.
--
-- These queries are written against the LDBC SNB Interactive PostgreSQL schema
-- (https://github.com/ldbc/ldbc_snb_interactive_v1_impls/blob/main/postgres/ddl/schema.sql)
-- and exercise the mappings declared in ldbc.yaml. They are loosely inspired
-- by the LDBC SNB Interactive Short (IS) and Complex (IC) read queries — see
-- the official spec for the full benchmark workload.


-- =============================================================================
-- SELECTION QUERIES
-- =============================================================================

-- Q1 — Point lookup by primary key (Person profile, cf. LDBC IS-1).
-- Expected Cypher: MATCH (p:Person {id: 933}) RETURN ...
SELECT p_personid, p_firstname, p_lastname, p_birthday, p_creationdate
FROM person
WHERE p_personid = 933;

-- Q2 — Range query on a timestamp column.
-- Expected Cypher: MATCH (po:Post) WHERE po.creationDate >= datetime('2010-06-01T00:00:00') ...
SELECT m_messageid, m_creationdate, m_content
FROM post
WHERE m_creationdate >= '2010-06-01' AND m_creationdate < '2010-07-01';

-- Q3 — Lookup persons by first name.
SELECT p_personid, p_firstname, p_lastname
FROM person
WHERE p_firstname = 'Mahinda';


-- =============================================================================
-- AGGREGATION QUERIES
-- =============================================================================

-- Q4 — Top forums by member count (junction-table aggregation).
-- Cypher traversal: (f:Forum)-[:HAS_MEMBER]->(:Person)
SELECT f.f_forumid, f.f_title, COUNT(*) AS member_count
FROM forum f
JOIN forum_person fp ON fp.fp_forumid = f.f_forumid
GROUP BY f.f_forumid, f.f_title
ORDER BY member_count DESC
LIMIT 10;

-- Q5 — Most-used tags across all posts and comments.
-- Cypher traversal: (m)-[:HAS_TAG]->(t:Tag) for both Post and Comment subtypes.
SELECT t.t_tagid, t.t_name, COUNT(*) AS usage_count
FROM tag t
JOIN message_tag mt ON mt.mt_tagid = t.t_tagid
GROUP BY t.t_tagid, t.t_name
ORDER BY usage_count DESC
LIMIT 20;


-- =============================================================================
-- JOIN / TRAVERSAL QUERIES
-- =============================================================================

-- Q6 — Two-hop traversal: friends of a given person (cf. LDBC IS-3).
-- Cypher: (p1:Person {id: 933})-[:KNOWS]->(p2:Person)
SELECT p2.p_personid, p2.p_firstname, p2.p_lastname, k.k_creationdate AS friendship_date
FROM person p1
JOIN knows k ON k.k_person1id = p1.p_personid
JOIN person p2 ON p2.p_personid = k.k_person2id
WHERE p1.p_personid = 933;

-- Q7 — Posts with their creator and tags (3-way join).
-- Cypher: (p:Person)<-[:HAS_CREATOR]-(po:Post)-[:HAS_TAG]->(t:Tag)
SELECT po.m_messageid, po.m_content, p.p_firstname, p.p_lastname, t.t_name
FROM post po
JOIN person p ON p.p_personid = po.m_creatorid
JOIN message_tag mt ON mt.mt_messageid = po.m_messageid
JOIN tag t ON t.t_tagid = mt.mt_tagid
WHERE po.m_creationdate >= '2010-06-01' AND po.m_creationdate < '2010-06-02';

-- Q8 — Friends-of-friends who share a tag interest (4-way join, cf. IC-10).
SELECT DISTINCT p3.p_personid, p3.p_firstname, p3.p_lastname, t.t_name AS shared_tag
FROM person p1
JOIN knows k1 ON k1.k_person1id = p1.p_personid
JOIN knows k2 ON k2.k_person1id = k1.k_person2id
JOIN person p3 ON p3.p_personid = k2.k_person2id
JOIN person_tag pt1 ON pt1.pt_personid = p1.p_personid
JOIN person_tag pt3 ON pt3.pt_personid = p3.p_personid AND pt3.pt_tagid = pt1.pt_tagid
JOIN tag t ON t.t_tagid = pt1.pt_tagid
WHERE p1.p_personid = 933 AND p3.p_personid <> p1.p_personid;

-- Q9 — Polymorphic LIKES across Post target (cf. IC-7).
-- The likes.l_messageid is polymorphic; JOINing to `post` selects the Post
-- variant of the Person-LIKES-Post edge mapping.
SELECT DISTINCT liker.p_personid, liker.p_firstname, liker.p_lastname,
                po.m_messageid, po.m_content, l.l_creationdate AS liked_at
FROM person liker
JOIN likes l ON l.l_personid = liker.p_personid
JOIN post po ON po.m_messageid = l.l_messageid
JOIN person creator ON creator.p_personid = po.m_creatorid
WHERE creator.p_personid = 933
ORDER BY liked_at DESC
LIMIT 20;

-- Q10 — REPLY_OF chain: comments replying to a specific post (cf. IS-7).
-- Cypher: (c:Comment)-[:REPLY_OF]->(:Post {id: 1099511627776})
SELECT c.m_messageid, c.m_content, c.m_creationdate, p.p_firstname, p.p_lastname
FROM comment c
JOIN person p ON p.p_personid = c.m_creatorid
WHERE c.m_replyof_post = 1099511627776
ORDER BY c.m_creationdate DESC;

-- Q11 — Optional traversal (LEFT JOIN): forums and their member count
--       including empty forums.
-- Expected Cypher: MATCH (f:Forum) OPTIONAL MATCH (f)-[:HAS_MEMBER]->(p:Person) ...
SELECT f.f_forumid, f.f_title, COUNT(fp.fp_personid) AS member_count
FROM forum f
LEFT JOIN forum_person fp ON fp.fp_forumid = f.f_forumid
GROUP BY f.f_forumid, f.f_title
ORDER BY member_count DESC;

-- Q12 — HAVING: persons with more than 5 friends.
SELECT p.p_personid, p.p_firstname, p.p_lastname, COUNT(*) AS friend_count
FROM person p
JOIN knows k ON k.k_person1id = p.p_personid
GROUP BY p.p_personid, p.p_firstname, p.p_lastname
HAVING COUNT(*) > 5
ORDER BY friend_count DESC;


-- =============================================================================
-- SET QUERIES
-- =============================================================================

-- Q13 — UNION: persons who either created OR liked any post in a forum.
SELECT DISTINCT p.p_personid, p.p_firstname, p.p_lastname
FROM person p
JOIN post po ON po.m_creatorid = p.p_personid
WHERE po.m_ps_forumid = 549755813888
UNION
SELECT DISTINCT p.p_personid, p.p_firstname, p.p_lastname
FROM person p
JOIN likes l ON l.l_personid = p.p_personid
JOIN post po ON po.m_messageid = l.l_messageid
WHERE po.m_ps_forumid = 549755813888;

-- Q14 — DIFFERENCE / NOT EXISTS: persons with no friends.
-- Graph equivalent: MATCH (p:Person) WHERE NOT (p)-[:KNOWS]->(:Person) RETURN ...
SELECT p.p_personid, p.p_firstname, p.p_lastname
FROM person p
WHERE NOT EXISTS (
    SELECT 1 FROM knows k WHERE k.k_person1id = p.p_personid
);
