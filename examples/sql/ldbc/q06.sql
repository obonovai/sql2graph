SELECT p2.id, p2.first_name, p2.last_name, k.creation_date AS friendship_date
FROM person p1
JOIN knows k ON k.person_id = p1.id
JOIN person p2 ON p2.id = k.friend_id
WHERE p1.id = 933;
