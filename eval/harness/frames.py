"""Shared DataFrame/metric helpers for the result notebooks (02-05).

The metric notebooks all follow the same shape: load the per-attempt records, turn them
into a per-record metrics DataFrame, then slice it (per query, per model, per SQL feature)
and write one CSV. That scaffolding used to be copy-pasted into every notebook; it lives here
once so the notebooks keep only their visible per-model / per-figure cells and a thin wrapper
that binds this notebook's DataFrame and metric-column set.

Nothing here computes a metric itself -- the metric functions stay in :mod:`harness.canonical`,
:mod:`harness.components`, :mod:`harness.distances`, and :mod:`harness.pricing`. These helpers
only build, order, slice, and persist the frames those functions feed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pandas as pd

from .config import DIFF_ORDER, GOLD_DIR, order_models
from .datasets import load_dataset
from .pricing import billed_input_tokens, usd_cost

# The stratification keys every records file carries; the join key across every metrics CSV.
STRAT_KEYS = ["dataset", "target", "model", "query_id", "difficulty"]


def order_frame_models(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``df`` with its ``model`` column as an ordered categorical in canonical order.

    Replaces the ``pd.Categorical(order_models(...))`` step the notebooks repeated, so every
    view sorts models the same way (llama, qwen, gemma, opus, opus-thinking) rather than
    alphabetically. A no-op copy when there is no ``model`` column.
    """
    if "model" not in df.columns:
        return df
    df = df.copy()
    df["model"] = pd.Categorical(df["model"], order_models(df["model"].unique()), ordered=True)
    return df


def feature_map(dataset: str = "ldbc") -> dict[str, list[str]]:
    """``{query_id: sql_features}`` for a gold dataset (the notebooks' ``FEATURES`` map)."""
    return {q.id: q.sql_features for q in load_dataset(dataset)}


def compute_metrics_frame(
    records: list[dict],
    metric_fn: Callable[[str, str, str], Mapping[str, float]],
    missing: Mapping[str, float],
    *,
    extra: tuple[str, ...] = ("thinking_used",),
) -> pd.DataFrame:
    """Per-record metric loop shared by notebooks 03/04 (structural, distance).

    ``metric_fn(translated, expected, target)`` returns the metric columns for one record;
    ``missing`` is the fill used when a record has no generated query (0.0 for the
    higher-is-better structural metrics, 1.0 for the distances). ``extra`` names record fields
    to carry onto each row (defaulting to ``thinking_used``; notebook 04 also keeps
    ``validation_passed`` for its validated-only boxplots). Returns the frame with the
    stratification keys, the ``extra`` fields, and the metric columns, models canonically ordered.
    """
    rows: list[dict] = []
    for r in records:
        base = {k: r[k] for k in STRAT_KEYS}
        for name in extra:
            base[name] = r.get(name, False)
        translated = r.get("generated_query")
        metrics = dict(missing) if not translated else dict(metric_fn(translated, r["expected_query"], r["target"]))
        rows.append({**base, **metrics})
    return order_frame_models(pd.DataFrame(rows))


def add_behavioural_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Derive the notebook-02 behavioural/cost columns on a raw records frame.

    Adds ``pass_at_1`` (validated on the first iteration, no repair), ``billed_input_tokens``
    (uncached input + both Anthropic cache buckets), ``cost_usd`` (per-record, cache-aware),
    and a ``thinking_used`` fallback, then orders the model column. The token/cost maths stay
    in :mod:`harness.pricing`; this only applies them across the frame.
    """
    df = df.copy()
    df["pass_at_1"] = df["validation_passed"] & (df["iterations_used"] == 1)
    df["billed_input_tokens"] = billed_input_tokens(
        df["input_tokens"], df["cache_read_tokens"], df["cache_creation_tokens"]
    )
    df["cost_usd"] = df.apply(
        lambda r: usd_cost(
            r["provider"],
            str(r["model"]),
            r["input_tokens"],
            r["output_tokens"],
            r["cache_read_tokens"],
            r["cache_creation_tokens"],
        ),
        axis=1,
    )
    if "thinking_used" not in df.columns:
        df["thinking_used"] = False
    return order_frame_models(df)


def query_results(df: pd.DataFrame, target: str, model: str, metric_cols: list[str]) -> pd.DataFrame | None:
    """Per-query metrics for one ``(target, model)`` cell, sorted by query id.

    Returns ``None`` (after a short note) when that cell has no records, so a partial run still
    renders. The thinking variant additionally shows its ``thinking_used`` flag.
    """
    sub = df[(df["target"] == target) & (df["model"] == model)]
    if not len(sub):
        print(f"No records for {target}/{model}.")
        return None
    cols = ["query_id", "difficulty"] + metric_cols + (["thinking_used"] if "thinking" in str(model) else [])
    return sub[cols].sort_values("query_id").reset_index(drop=True)


def summary_by_model(df: pd.DataFrame, target: str, metric_cols: list[str]) -> pd.DataFrame | None:
    """Mean of ``metric_cols`` for one target, by model (canonical order); ``None`` if empty."""
    sub = df[df["target"] == target]
    if not len(sub):
        print(f"No records for {target}.")
        return None
    return sub.groupby("model", observed=True)[metric_cols].mean()


def by_feature(
    df: pd.DataFrame,
    cols: list[str],
    features: Mapping[str, list[str]],
    group: tuple[str, ...] = ("target", "feature"),
) -> pd.DataFrame:
    """Mean of ``cols`` per ``group`` (default ``(target, feature)``); explodes the feature list.

    ``group=("feature",)`` is used inside a single-target report section (where ``target`` is
    constant). Columns absent from ``df`` are skipped, so the same call works on a partial join;
    this is the one ``by_feature`` shared by the metric notebooks and the report.
    """
    f = df.copy()
    f["feature"] = f["query_id"].map(features)
    f = f.explode("feature").dropna(subset=["feature"])
    cols = [c for c in cols if c in f.columns]
    return f.groupby(list(group), observed=True)[cols].mean()


def run_summary(
    df: pd.DataFrame,
    metric_cols: list[str],
    feature_cols: list[str],
    features: Mapping[str, list[str]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """The run-level ``(by target x model, by target x difficulty, by target x feature)`` summary.

    Returns the three aggregated frames; the notebook displays each under its own heading. The
    difficulty axis is ordered easy -> medium -> hard, never alphabetical.
    """
    by_model = df.groupby(["target", "model"], observed=True)[metric_cols].mean()
    d = df.copy()
    d["difficulty"] = pd.Categorical(d["difficulty"], DIFF_ORDER, ordered=True)
    by_diff = d.groupby(["target", "difficulty"], observed=True)[metric_cols].mean()
    by_feat = by_feature(df, feature_cols, features)
    return by_model, by_diff, by_feat


def identity_check(
    checks: list[tuple[str, Callable[[str, str, str], object], object]], gold_dir: Path = GOLD_DIR
) -> pd.DataFrame:
    """Gold-vs-self sanity test the metric notebooks open with (03/04).

    Each ``(column, fn, ideal)`` scores every gold query against itself: ``fn(ref, ref, target)``
    must equal ``ideal`` (Exact Match ``True``, Component F1 ``1.0``, any distance ``0.0``), or
    canonicalisation / a metric has silently drifted. Prints the pass/fail summary, raises
    ``AssertionError`` (listing the offenders) on any failure, and returns the per-case frame.
    """
    rows: list[dict] = []
    for path in sorted(gold_dir.glob("*.yaml")):
        for q in load_dataset(path.stem):
            for target, ref in q.expected.items():
                row = {"dataset": path.stem, "query_id": q.id, "target": target}
                for col, fn, _ideal in checks:
                    row[col] = fn(ref, ref, target)
                rows.append(row)
    df = pd.DataFrame(rows)
    failed = pd.Series(False, index=df.index)
    for col, _fn, ideal in checks:
        if isinstance(ideal, bool):
            failed = failed | (df[col] != ideal)
        else:
            failed = failed | ((df[col].astype(float) - float(ideal)).abs() > 1e-9)
    failures = df[failed]
    print(f"Identity test: {len(df)} cases; failures: {len(failures)}")
    if len(failures):
        print(failures.to_string(index=False))
        raise AssertionError("Identity sanity test failed: canonicalisation or a metric has drifted.")
    print("PASS")
    return df


def save_metrics_csv(df: pd.DataFrame, path: Path, *, drop: tuple[str, ...] = ()) -> int:
    """Write a per-record metrics frame to ``path`` (model stringified for stable output).

    ``drop`` names figure-only columns to leave out of the CSV (e.g. notebook 04's
    ``validation_passed`` flag). Returns the row count written.
    """
    out = df.drop(columns=list(drop)) if drop else df.copy()
    out["model"] = out["model"].astype(str)
    out.to_csv(path, index=False)
    return len(out)
