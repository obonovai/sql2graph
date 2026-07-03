"""Gold-dataset loading and work-item construction.

A gold dataset (``eval/gold/<name>.yaml``) is a list of queries, each
carrying the source SQL plus one expected query per target language
(``expected_cypher``, ``expected_aql``, ...). :func:`build_work_items` flattens a
:class:`~harness.config.RunConfig` into one :class:`WorkItem` per gold query
for that config's single target, skipping queries that lack the target's gold
column (so a Cypher run is unaffected by a query that only has ``expected_aql``).

The relational->graph :class:`~rows2graph.SchemaMapping` the translator reasons
over lives separately under ``examples/mappings/<name>.yaml`` and is loaded by
:func:`mapping_for`.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from rows2graph import SchemaMapping

from .config import GOLD_DIR, MAPPINGS_DIR, RunConfig, Target

_TARGETS: tuple[Target, ...] = ("cypher", "aql", "gremlin")


@dataclass
class GoldQuery:
    id: str
    sql: str
    difficulty: str
    sql_features: list[str]
    description: str
    # target -> expected gold query string, for whichever targets the YAML defines.
    expected: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkItem:
    dataset: str
    query_id: str
    difficulty: str
    sql_features: list[str]
    sql: str
    target: str
    expected_query: str
    mapping_path: Path


def expected_key(target: str) -> str:
    return f"expected_{target}"


def load_dataset(dataset: str) -> list[GoldQuery]:
    """Load ``eval/gold/<dataset>.yaml`` into :class:`GoldQuery` objects."""
    path = GOLD_DIR / f"{dataset}.yaml"
    data = yaml.safe_load(path.read_text())
    queries: list[GoldQuery] = []
    for q in data["queries"]:
        expected = {
            t: q[expected_key(t)]
            for t in _TARGETS
            if q.get(expected_key(t))
        }
        queries.append(
            GoldQuery(
                id=q["id"],
                sql=q["sql"],
                difficulty=q["difficulty"],
                sql_features=q.get("sql_features", []),
                description=q.get("description", ""),
                expected=expected,
            )
        )
    return queries


@functools.cache
def mapping_for(dataset: str) -> SchemaMapping:
    """Load (and cache) the relational->graph mapping for ``dataset``."""
    return SchemaMapping.from_yaml(MAPPINGS_DIR / f"{dataset}.yaml")


def mapping_path_for(dataset: str) -> Path:
    return MAPPINGS_DIR / f"{dataset}.yaml"


def build_work_items(rc: RunConfig) -> list[WorkItem]:
    """One :class:`WorkItem` per gold query for ``rc.target``.

    Honours ``rc.subset`` and silently skips queries with no gold column for the
    target (so adding AQL gold later does not perturb the Cypher run, and a
    Cypher-only dataset entry is skipped on an AQL run).
    """
    items: list[WorkItem] = []
    mapping_path = mapping_path_for(rc.dataset)
    for q in load_dataset(rc.dataset):
        if rc.subset is not None and q.id not in rc.subset:
            continue
        expected = q.expected.get(rc.target)
        if not expected:
            continue
        items.append(
            WorkItem(
                dataset=rc.dataset,
                query_id=q.id,
                difficulty=q.difficulty,
                sql_features=q.sql_features,
                sql=q.sql,
                target=rc.target,
                expected_query=expected,
                mapping_path=mapping_path,
            )
        )
    return items
