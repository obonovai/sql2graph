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
    EVAL_DIR,
    EXECUTION_CACHE_PATH,
    FIGURES_DIR,
    FINAL_REPORT_MD,
    GOLD_DIR,
    MAPPINGS_DIR,
    METRICS_BEHAVIOURAL_CSV,
    METRICS_DISTANCE_CSV,
    METRICS_EXECUTION_CSV,
    METRICS_STRUCTURAL_CSV,
    OUTPUTS_DIR,
    RECORDS_GLOB,
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
from .records import (
    load_records,
    records_frame,
)
from .runner import (
    AttemptRecord,
    make_llm_for,
    make_translator_for,
    run_translation,
)

__all__ = [
    # config: paths
    "REPO_ROOT",
    "EVAL_DIR",
    "GOLD_DIR",
    "MAPPINGS_DIR",
    "OUTPUTS_DIR",
    "REPORTS_DIR",
    "FIGURES_DIR",
    "FINAL_REPORT_MD",
    # config: the notebook filename contract
    "RECORDS_GLOB",
    "METRICS_BEHAVIOURAL_CSV",
    "METRICS_STRUCTURAL_CSV",
    "METRICS_DISTANCE_CSV",
    "METRICS_EXECUTION_CSV",
    "EXECUTION_CACHE_PATH",
    # config: the matrix
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
    # records
    "load_records",
    "records_frame",
    # runner
    "AttemptRecord",
    "run_translation",
    "make_llm_for",
    "make_translator_for",
]
