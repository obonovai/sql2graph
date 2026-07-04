-- LDBC SNB Interactive v1 -- NAIVELY normalized (counter-example to ldbc.sql).
--
-- This schema is textbook-normalized (3NF) and byte-identical to `ldbc.sql`
-- EXCEPT for one thing: `post.forum_id` is a plain foreign key with no
-- `ON DELETE CASCADE`. Because a lone 1:N foreign key is direction-ambiguous,
-- the deterministic builder falls back to its default direction (FK-holder ->
-- referenced) and emits the forum-post edge as `Post -> Forum` -- the reverse of
-- LDBC's `Forum CONTAINER_OF Post`.
--
-- So this "perfectly normal" normalized schema converts *almost* perfectly: all
-- 8 nodes, all list properties, and 22 of 23 edges are correct; only the
-- containment edge's DIRECTION is wrong, because plain normalization records the
-- relationship but not whether it is composition (ownership) or association.
-- `ldbc.sql` fixes it with a single `ON DELETE CASCADE`. See
-- `docs/LDBC_NORMALIZATION.md` for the full comparison.

-- --- Static reference data: places, tag hierarchy, organisations ------------

CREATE TABLE place (
    id        BIGINT PRIMARY KEY,
    name      TEXT NOT NULL,
    url       TEXT,
    type      TEXT NOT NULL,                          -- City | Country | Continent
    partof_id BIGINT REFERENCES place(id)             -- City->Country, Country->Continent
);

CREATE TABLE tag_class (
    id              BIGINT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT,
    subclass_of_id  BIGINT REFERENCES tag_class(id)
);

CREATE TABLE tag (
    id            BIGINT PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT,
    tag_class_id  BIGINT REFERENCES tag_class(id)
);

CREATE TABLE organisation (
    id        BIGINT PRIMARY KEY,
    type      TEXT NOT NULL,                          -- University | Company
    name      TEXT NOT NULL,
    url       TEXT,
    place_id  BIGINT REFERENCES place(id)
);

-- --- Person and its multi-valued attributes ---------------------------------

CREATE TABLE person (
    id            BIGINT PRIMARY KEY,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    gender        TEXT,
    birthday      DATE,
    creation_date TIMESTAMP WITH TIME ZONE NOT NULL,
    location_ip   TEXT,
    browser_used  TEXT,
    place_id      BIGINT REFERENCES place(id)         -- IS_LOCATED_IN city
);

-- Value-list child tables -> Person.email / Person.language list properties
-- (these DO convert correctly here -- the list-property support is independent).
CREATE TABLE person_email (
    person_id BIGINT NOT NULL REFERENCES person(id),
    email     TEXT   NOT NULL,
    PRIMARY KEY (person_id, email)
);

CREATE TABLE person_speaks (
    person_id BIGINT NOT NULL REFERENCES person(id),
    language  TEXT   NOT NULL,
    PRIMARY KEY (person_id, language)
);

-- --- Person-Person and Person-Tag/Organisation edges ------------------------

CREATE TABLE knows (
    person_id     BIGINT NOT NULL REFERENCES person(id),
    friend_id     BIGINT NOT NULL REFERENCES person(id),
    creation_date TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (person_id, friend_id)
);

CREATE TABLE has_interest (
    person_id BIGINT NOT NULL REFERENCES person(id),
    tag_id    BIGINT NOT NULL REFERENCES tag(id),
    PRIMARY KEY (person_id, tag_id)
);

CREATE TABLE study_at (
    person_id       BIGINT NOT NULL REFERENCES person(id),
    organisation_id BIGINT NOT NULL REFERENCES organisation(id),
    class_year      INTEGER,
    PRIMARY KEY (person_id, organisation_id)
);

CREATE TABLE work_at (
    person_id       BIGINT NOT NULL REFERENCES person(id),
    organisation_id BIGINT NOT NULL REFERENCES organisation(id),
    work_from       INTEGER,
    PRIMARY KEY (person_id, organisation_id)
);

-- --- Forum and forum edges --------------------------------------------------

CREATE TABLE forum (
    id                  BIGINT PRIMARY KEY,
    title               TEXT NOT NULL,
    creation_date       TIMESTAMP WITH TIME ZONE NOT NULL,
    moderator_person_id BIGINT REFERENCES person(id)
);

CREATE TABLE forum_has_member (
    forum_id  BIGINT NOT NULL REFERENCES forum(id),
    person_id BIGINT NOT NULL REFERENCES person(id),
    join_date TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (forum_id, person_id)
);

CREATE TABLE forum_has_tag (
    forum_id BIGINT NOT NULL REFERENCES forum(id),
    tag_id   BIGINT NOT NULL REFERENCES tag(id),
    PRIMARY KEY (forum_id, tag_id)
);

-- --- Post and post edges ----------------------------------------------------

CREATE TABLE post (
    id                BIGINT PRIMARY KEY,
    image_file        TEXT,
    creation_date     TIMESTAMP WITH TIME ZONE NOT NULL,
    location_ip       TEXT,
    browser_used      TEXT,
    language          TEXT,
    content           TEXT,
    length            INTEGER,
    creator_person_id BIGINT NOT NULL REFERENCES person(id),
    forum_id          BIGINT NOT NULL REFERENCES forum(id),   -- plain FK: builder emits Post -> Forum (WRONG)
    place_id          BIGINT NOT NULL REFERENCES place(id)
);

CREATE TABLE post_has_tag (
    post_id BIGINT NOT NULL REFERENCES post(id),
    tag_id  BIGINT NOT NULL REFERENCES tag(id),
    PRIMARY KEY (post_id, tag_id)
);

CREATE TABLE likes_post (
    person_id     BIGINT NOT NULL REFERENCES person(id),
    post_id       BIGINT NOT NULL REFERENCES post(id),
    creation_date TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (person_id, post_id)
);

-- --- Comment and comment edges ----------------------------------------------

CREATE TABLE comment (
    id                  BIGINT PRIMARY KEY,
    creation_date       TIMESTAMP WITH TIME ZONE NOT NULL,
    location_ip         TEXT,
    browser_used        TEXT,
    content             TEXT,
    length              INTEGER,
    creator_person_id   BIGINT NOT NULL REFERENCES person(id),
    place_id            BIGINT NOT NULL REFERENCES place(id),
    reply_of_post_id    BIGINT REFERENCES post(id),
    reply_of_comment_id BIGINT REFERENCES comment(id)
);

CREATE TABLE comment_has_tag (
    comment_id BIGINT NOT NULL REFERENCES comment(id),
    tag_id     BIGINT NOT NULL REFERENCES tag(id),
    PRIMARY KEY (comment_id, tag_id)
);

CREATE TABLE likes_comment (
    person_id     BIGINT NOT NULL REFERENCES person(id),
    comment_id    BIGINT NOT NULL REFERENCES comment(id),
    creation_date TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (person_id, comment_id)
);
