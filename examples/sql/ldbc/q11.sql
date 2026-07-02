SELECT p.id, p.first_name, p.last_name, COUNT(*) AS friend_count
FROM person p
JOIN knows k ON k.person_id = p.id
GROUP BY p.id, p.first_name, p.last_name
HAVING COUNT(*) > 5
ORDER BY friend_count DESC;
