"""Reading translation records back for the metric notebooks.

:mod:`harness.runner` writes one ``records_<dataset>_<target>_<model>.json``
per matrix cell; these helpers glob and concatenate them (optionally filtered
by stratification key) so notebooks 02-06 all load records the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import RECORDS_GLOB


def load_records(
    outputs_dir: Path,
    dataset: str | None = None,
    target: str | None = None,
    model: str | None = None,
) -> list[dict]:
    """Concatenate every records file under ``outputs_dir``, optionally filtered."""
    records: list[dict] = []
    for path in sorted(outputs_dir.glob(RECORDS_GLOB)):
        records.extend(json.loads(path.read_text()))
    if dataset is not None:
        records = [r for r in records if r.get("dataset") == dataset]
    if target is not None:
        records = [r for r in records if r.get("target") == target]
    if model is not None:
        records = [r for r in records if r.get("model") == model]
    return records


def records_frame(outputs_dir: Path, **filt):
    """Load records (optionally filtered) into a pandas DataFrame."""
    import pandas as pd

    return pd.DataFrame(load_records(outputs_dir, **filt))
