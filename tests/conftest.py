"""Root fixtures shared across the whole test tree.

Only path fixtures live here so every suite (unit / integration / eval) can
resolve repo-relative resources without brittle ``__file__`` arithmetic. These
are computed from this conftest's own fixed location, which stays put no matter
how deep a test module is nested.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the project root (the directory holding ``pyproject.toml``)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def examples_dir(repo_root: Path) -> Path:
    """The shipped ``examples/`` directory (mappings, ddl, sql)."""
    return repo_root / "examples"


@pytest.fixture(scope="session")
def mappings_dir(examples_dir: Path) -> Path:
    """The shipped ``examples/mappings/`` directory (tpch.yaml, ldbc.yaml, ...)."""
    return examples_dir / "mappings"
