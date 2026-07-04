# Normalized relational → LDBC graph: what converts, and what doesn't

This note compares two **fully normalized** relational schemas for the LDBC SNB
Interactive v1 dataset and explains why one converts to the proper LDBC property
graph and the other does not — using only the **deterministic** mapping builder
(`project_to_mapping`, no LLM).

- [`examples/ddl/ldbc.sql`](../examples/ddl/ldbc.sql) — converts to the **proper** LDBC graph.
- [`examples/ddl/ldbc_naive.sql`](../examples/ddl/ldbc_naive.sql) — the same schema, naively
  normalized, that converts **almost** correctly (one edge points the wrong way).

Reproduce either with:

```python
from sql2graph.mapping_builder.ddl import extract_schema_from_ddl
from sql2graph.mapping_builder.project import project_to_mapping

ddl = open("examples/ddl/ldbc.sql").read()
result = project_to_mapping(extract_schema_from_ddl(ddl, dialect="postgres"))
print(result.report.warnings)   # explains every non-default decision
```

## TL;DR

The two files are byte-identical except **one clause**: in `ldbc.sql`,
`post.forum_id` carries `ON DELETE CASCADE`; in `ldbc_naive.sql` it does not.
That single clause is the difference between:

| | `ldbc.sql` | `ldbc_naive.sql` |
|---|---|---|
| Nodes | 8 | 8 |
| Edges | 23 | 23 |
| `Person.email` / `Person.language` list properties | ✅ | ✅ |
| Forum–Post containment | `Forum → Post` ✅ | `Post → Forum` ❌ |

22 of 23 edges are identical. The sole divergence is the **direction** of the
containment edge (`CONTAINER_OF`).

## Why direction is the hard part

The builder's default rule for a foreign key is **`FK-holder → referenced`**: a row
that holds the FK points at the row it references. That is correct for 22 of LDBC's
23 relationships (a post points at its creator, a comment at the post it replies to,
a tag at its class, …).

But a **1:N foreign key is direction-ambiguous**. `post.forum_id` supports *both*
readings equally:

- `Post -[:BELONGS_TO]-> Forum` (child → parent, the default), and
- `Forum -[:CONTAINS]-> Post` (parent → child, what LDBC calls `CONTAINER_OF`).

They are the *same join* (`post.forum_id = forum.id`); only the verb and the arrow
differ. So the direction is **semantic, not structural** — you cannot recover it from
the shape of the FK alone. Concretely, `post.forum_id` and `post.creator_person_id`
are structurally identical (both `NOT NULL` 1:N FKs on `post` to a surrogate-key
parent), yet LDBC wants them in **opposite** directions. No purely structural rule can
tell them apart. This is exactly why the builder normally leaves edge direction (and
type names) to its optional LLM refinement pass.

## The two composition signals the builder now reads

A *properly, expressively* normalized schema can encode the missing intent —
**composition** (the parent owns the child) versus **association** (the child merely
references an independent entity). The deterministic builder now recognizes two
standard structural markers of composition and, for those, emits **`parent → child`**
(see `is_composition_fk` in `src/sql2graph/mapping_builder/project.py`):

1. **`ON DELETE CASCADE`** — deleting the parent deletes the child, so the parent owns
   the child's lifecycle. This is how `ldbc.sql` marks `Forum CONTAINER_OF Post`:
   ```sql
   forum_id BIGINT NOT NULL REFERENCES forum(id) ON DELETE CASCADE   -- Forum owns its Posts
   ```
   Association FKs stay plain and keep the default direction:
   ```sql
   creator_person_id BIGINT NOT NULL REFERENCES person(id)           -- Post references a Person
   ```

2. **Identifying relationship (weak entity)** — the FK is *part of the child's primary
   key*, so the child cannot exist without the parent (e.g.
   `lineitem(order_id, line_no, PRIMARY KEY(order_id, line_no))`). LDBC's surrogate-key
   schema does not use this form, but TPC-H's `lineitem` does, so its
   `Order → LineItem` edge now comes out as composition automatically.

Both signals are **opt-in**: a plain FK (no cascade, not in the PK) is treated as an
association and keeps the default `child → parent` direction, so no existing schema's
edges change unless it declares one of these markers. Every reversal is recorded in the
`CoverageReport` (e.g. *"Edge 'HAS_POST' Forum->Post directed parent->child
(composition: ON DELETE CASCADE)"*), so the decision is always auditable.

> Edge **type names** from the deterministic builder are mechanical (`HAS_POST`,
> `HAS_REGION`, …) and are not meaningful — the hand-authored
> [`examples/mappings/ldbc.yaml`](../examples/mappings/ldbc.yaml) carries the real LDBC
> names (`CONTAINER_OF`, `HAS_CREATOR`, …). Only the **structure** (which tables are
> nodes/edges, the join columns, and the direction) is what this note is about.

## The 23 LDBC edges

| # | Relationship | Source of the edge | How the direction is decided |
|---|---|---|---|
| 1 | `Place → Place` (part of) | `place.partof_id` | default child→parent ✅ |
| 2 | `TagClass → TagClass` (subclass of) | `tag_class.subclass_of_id` | default ✅ |
| 3 | `Tag → TagClass` (has type) | `tag.tag_class_id` | default ✅ |
| 4 | `Organisation → Place` | `organisation.place_id` | default ✅ |
| 5 | `Person → Place` | `person.place_id` | default ✅ |
| 6 | `Forum → Person` (moderator) | `forum.moderator_person_id` | default ✅ |
| 7 | `Post → Person` (creator) | `post.creator_person_id` | default ✅ |
| 8 | `Post → Place` | `post.place_id` | default ✅ |
| 9 | `Comment → Person` (creator) | `comment.creator_person_id` | default ✅ |
| 10 | `Comment → Place` | `comment.place_id` | default ✅ |
| 11 | `Comment → Post` (reply of) | `comment.reply_of_post_id` | default ✅ |
| 12 | `Comment → Comment` (reply of) | `comment.reply_of_comment_id` | default ✅ |
| 13–22 | `KNOWS`, `HAS_INTEREST`, `STUDY_AT`, `WORK_AT`, `HAS_MEMBER`, `HAS_TAG` ×3, `LIKES` ×2 | junction tables (`knows`, `has_interest`, …) | junction FK order ✅ |
| **23** | **`Forum → Post` (contains)** | **`post.forum_id`** | **composition — needs `ON DELETE CASCADE`** ⚠ |

Only row 23 depends on a composition signal. In `ldbc_naive.sql` it comes out reversed
(`Post → Forum`); everything else is identical.

## Companion issue: multi-valued attributes

A separate relational→graph gap, already handled: LDBC's `Person.email` and
`Person.speaks` (language) are **multi-valued**. Normalizing them produces value-list
child tables:

```sql
CREATE TABLE person_email  (person_id BIGINT REFERENCES person(id), email    TEXT, PRIMARY KEY (person_id, email));
CREATE TABLE person_speaks (person_id BIGINT REFERENCES person(id), language TEXT, PRIMARY KEY (person_id, language));
```

The mapping format's scalar `properties` (one graph property ← one column of the
node's own table) cannot express these, and a naive builder would turn each table into
a spurious `PersonEmail` / `PersonSpeak` node. The builder now detects a **value-list
table** (exactly one FK to a parent, every column part of the PK, referenced by
nothing — see `is_multivalue_property_table`) and folds it into a **list property** on
the parent node instead. Both `ldbc.sql` and `ldbc_naive.sql` convert these correctly;
the list-property support is independent of the direction question.

Together, list-property support + composition detection let a *properly, expressively*
normalized schema convert straight to the proper LDBC graph — no LLM, no denormalization.

## Caveats

- **The stock LDBC published schema encodes neither signal.** Its foreign keys are
  plain and its keys are surrogates, so out of the box it behaves like
  `ldbc_naive.sql` — the containment edge comes out reversed. To convert it
  deterministically you must add the honest `ON DELETE CASCADE` (a forum *does* own its
  posts) or model the weak-entity keys, or fix that one edge by hand / with the LLM pass.
- **Naming is out of scope.** The deterministic builder's labels/edge-types are
  mechanical; direction and structure are what these DDLs pin down.
- **graphonauts2's Postgres load is intentionally left in its natural form**
  (`post.forum_id` as a plain FK) to keep the benchmark's hand-written SQL and timings
  unchanged. Adding `ON DELETE CASCADE` there would make that schema convert too, at no
  cost to the stored data.
