"""Reusable evaluation harness for rows2graph SQL -> graph translation.

The notebooks under ``eval/notebooks/`` import from here. The run logic lives in
:mod:`harness.runner`, the matrix/config in :mod:`harness.config`, gold-dataset
loading in :mod:`harness.datasets`, and the string canonicalisation /
structural-component / distance primitives (shared by notebooks 03 and 04) in
:mod:`harness.canonical`, :mod:`harness.components`, and :mod:`harness.distances`.

Import pattern from a notebook (after the repo-root walk)::

    import sys
    sys.path.insert(0, str(REPO_ROOT / "eval"))
    from harness import RUN_MATRIX, run_translation, load_records
    from harness.canonical import canonicalize, exact_match
"""

from __future__ import annotations

from .config import (
    DEFAULT_VALIDATION_MODE,
    GOLD_DIR,
    MAPPINGS_DIR,
    OUTPUTS_DIR,
    REPO_ROOT,
    REPORTS_DIR,
    RUN_MATRIX,
    Provider,
    RunConfig,
    Target,
    ValidationMode,
    default_validation_mode,
    model_slug,
    records_filename,
)
from .datasets import (
    GoldQuery,
    WorkItem,
    build_work_items,
    expected_key,
    load_dataset,
    mapping_for,
)
from .pricing import (
    billed_input_tokens,
    rate_for,
    usd_cost,
)
from .runner import (
    AttemptRecord,
    load_records,
    make_llm_for,
    make_translator_for,
    records_frame,
    run_translation,
)

__all__ = [
    # config
    "REPO_ROOT",
    "GOLD_DIR",
    "MAPPINGS_DIR",
    "OUTPUTS_DIR",
    "REPORTS_DIR",
    "RUN_MATRIX",
    "RunConfig",
    "Provider",
    "Target",
    "ValidationMode",
    "DEFAULT_VALIDATION_MODE",
    "default_validation_mode",
    "model_slug",
    "records_filename",
    # datasets
    "GoldQuery",
    "WorkItem",
    "load_dataset",
    "mapping_for",
    "build_work_items",
    "expected_key",
    # pricing
    "usd_cost",
    "billed_input_tokens",
    "rate_for",
    # runner
    "AttemptRecord",
    "run_translation",
    "make_llm_for",
    "make_translator_for",
    "load_records",
    "records_frame",
]
