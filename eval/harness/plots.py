"""Shared matplotlib figures for the evaluation result notebooks.

Pure ``(df, ...) -> Figure`` helpers so each notebook cell is one or two lines and
the awkward parts live in one place:

* **Compare all models.** Every chart groups by ``model`` (not ``target``), so the
  four-model run is compared side by side rather than collapsed together.
* **All queries.** ``query_model_heatmap`` renders the full query x model grid, the
  per-query view the tables never showed.
* **Missing is not zero.** A model that has only run some notebooks (one-model-at-a-time,
  or execution metrics skipped) has NaN for the metrics it lacks. Heatmaps mask NaN to
  grey, grouped bars leave a gap, boxplots drop it - a blank cell never reads as a real 0.

matplotlib only (no seaborn/plotly), matching the curated ``eval`` extra. Axes are always
derived from the data (``sorted(unique)``), never hardcoded, so 1-4 models and any id set
render correctly. Colours are assigned per model by sorted position, so a model keeps the
same colour across every figure in a notebook.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import order_models

# Difficulty axis order (the gold sets use exactly these three buckets).
DIFF_ORDER = ["easy", "medium", "hard"]

# The 8 per-clause structural F1 columns, in a stable display order.
COMPONENT_F1_COLS = [
    "f1_node_labels",
    "f1_edge_types",
    "f1_directions",
    "f1_where",
    "f1_return",
    "f1_order",
    "f1_limit",
    "f1_aggregations",
]

# Discrete pass/fail palette (fail red, pass green); NaN cells are painted grey.
_PASS_CMAP = matplotlib.colors.ListedColormap(["#d62728", "#2ca02c"]).with_extremes(bad="lightgrey")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def model_axis(df: pd.DataFrame) -> list[str]:
    """Model names present in ``df``, in the canonical :func:`~harness.config.order_models`
    order (llama, qwen, gemma, opus, opus-thinking), never alphabetical."""
    if "model" not in df.columns:
        return []
    return order_models(df["model"].dropna().unique().tolist())


def query_axis(df: pd.DataFrame) -> list[str]:
    """Sorted unique query ids present in ``df`` (never hardcoded)."""
    if "query_id" not in df.columns:
        return []
    return sorted(df["query_id"].dropna().unique().tolist())


def model_colors(models: list[str]) -> dict[str, tuple]:
    """Stable ``tab10`` colour per model, keyed by name (same order -> same colour)."""
    cmap = matplotlib.colormaps["tab10"]
    return {m: cmap(i % 10) for i, m in enumerate(models)}


def _masked_cmap(name: str = "viridis", bad: str = "lightgrey"):
    """A continuous colormap that renders masked/NaN values as ``bad`` (grey)."""
    return matplotlib.colormaps[name].with_extremes(bad=bad)


def _numeric(series: pd.Series) -> pd.Series:
    """Coerce to float (bools -> 1.0/0.0, anything non-numeric -> NaN)."""
    return pd.to_numeric(series, errors="coerce")


def _has_data(df: pd.DataFrame, cols) -> bool:
    cols = [c for c in cols if c in df.columns]
    return bool(cols) and bool(df[cols].apply(_numeric).notna().to_numpy().any())


def _save(fig, path: Path | str | None):
    """Persist ``fig`` (dpi 120, tight) when a path is given; return it for display."""
    if path is not None:
        fig.savefig(str(path), dpi=120, bbox_inches="tight")
    return fig


def show(path: Path | str | None):
    """Display a saved figure in a notebook output cell, backend-independent.

    Renders the on-disk PNG (via ``IPython.display.Image``) so the figure appears in
    the output whether the kernel uses the inline or the Agg backend, and whether the
    notebook is run interactively or headless via ``nbconvert --execute``. If the figure
    was skipped (no data, so nothing was written), prints a short note instead.
    """
    p = Path(path) if path is not None else None
    if p is not None and p.exists():
        from IPython.display import Image, display

        display(Image(filename=str(p)))
    else:
        print(f"(no figure written: {p.name if p else 'figure'})")


def _no_data(title: str):
    """A small placeholder figure (NOT written to disk) when a metric is absent.

    Returned so a notebook cell still shows *something*; because it is not saved, the
    guarded figure list in ``06_report`` simply omits the missing chart.
    """
    fig, ax = plt.subplots(figsize=(5, 1.6))
    ax.text(0.5, 0.5, f"{title}\n(no data yet)", ha="center", va="center", fontsize=11, color="dimgray")
    ax.axis("off")
    return fig


def _per_model_means(df: pd.DataFrame, cols: list[str], models: list[str]) -> pd.DataFrame:
    """Per-model mean of each column (numeric-coerced), indexed by ``models``."""
    numeric = df.assign(**{c: _numeric(df[c]) for c in cols})
    return numeric.groupby("model")[cols].mean().reindex(models)


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def headline_bars(df: pd.DataFrame, path: Path | str | None = None, title: str = "Per-model headline metrics"):
    """Grouped bars of the headline "higher = better" metrics, per model.

    x-axis groups are the metrics; bars within a group are the models. Any metric whose
    column is absent or all-NaN (e.g. execution metrics before notebook 05 has run) is
    dropped, so the chart adapts to a partial run.
    """
    models = model_axis(df)
    if not models:
        return _no_data("Per-model headline")

    # (source column, display label). ``structural_similarity`` is derived as
    # 1 - normalized_ted so that, like the others, higher is better.
    spec = [
        ("validation_passed", "validation\npass rate"),
        ("pass_at_1", "pass@1"),
        ("component_f1_overall", "component\nF1"),
        ("normalized_ted", "structural\nsimilarity"),
        ("execution_accuracy", "execution\naccuracy"),
        ("result_f1", "result F1"),
    ]
    data: dict[str, pd.Series] = {}
    for col, label in spec:
        if col not in df.columns or not _numeric(df[col]).notna().any():
            continue
        mean = _numeric(df[col]).groupby(df["model"]).mean().reindex(models)
        data[label] = (1 - mean) if col == "normalized_ted" else mean
    if not data:
        return _no_data("Per-model headline")

    hb = pd.DataFrame(data).T  # index=metric, columns=model
    colors = [model_colors(models)[m] for m in hb.columns]
    ax = hb.plot(kind="bar", figsize=(max(8, 1.6 * len(hb.index) + 2), 4.6), ylim=(0, 1.05), color=colors, width=0.82)
    ax.set_ylabel("score (higher = better)")
    ax.set_title(title)
    ax.set_xticklabels(hb.index, rotation=0)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(title="model", bbox_to_anchor=(1.01, 1.0), loc="upper left", fontsize=8)
    return _save(ax.figure, path)


def per_model_bars(
    df: pd.DataFrame,
    metrics: list[str],
    path: Path | str | None = None,
    *,
    title: str = "",
    ylabel: str = "value",
    ylim: tuple[float, float] | None = (0.0, 1.05),
    labels: dict[str, str] | None = None,
):
    """Grouped bars of the given rate-like ``metrics`` (each a per-model mean).

    Used by the behavioural / structural / execution notebooks for their own small
    per-model comparisons. Columns absent or all-NaN are skipped.
    """
    present = [m for m in metrics if m in df.columns and _numeric(df[m]).notna().any()]
    if not present:
        return _no_data(title or "per-model bars")
    models = model_axis(df)
    means = _per_model_means(df, present, models)
    means = means.rename(columns=(labels or {}))
    hb = means.T  # index=metric, columns=model
    colors = [model_colors(models)[m] for m in hb.columns]
    ax = hb.plot(kind="bar", figsize=(max(7, 1.5 * len(hb.index) + 2), 4.2), color=colors, width=0.82)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_xticklabels(hb.index, rotation=0)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(title="model", bbox_to_anchor=(1.01, 1.0), loc="upper left", fontsize=8)
    return _save(ax.figure, path)


def query_model_heatmap(
    df: pd.DataFrame,
    value: str,
    path: Path | str | None = None,
    *,
    discrete: bool = False,
    cmap_name: str = "viridis",
    models: list[str] | None = None,
    queries: list[str] | None = None,
    title: str = "",
    cbar_label: str = "",
):
    """Query (rows) x model (columns) heatmap of ``value`` - the per-query view.

    ``discrete=True`` uses a red/green pass/fail palette (values thresholded at 0.5);
    otherwise a continuous colormap (``cmap_name``, default viridis) in [0, 1] - pass
    ``"viridis_r"`` for a distance where lower is better so bright reads as good. Missing
    (model, query) pairs are masked to grey and annotated ``n/a``, so a model that has not
    produced this metric is visibly blank rather than a fake 0.
    """
    if value not in df.columns or not _numeric(df[value]).notna().any():
        return _no_data(title or value)
    models = models or model_axis(df)
    queries = queries or query_axis(df)

    tmp = df.assign(__val=_numeric(df[value]))
    piv = tmp.pivot_table(index="query_id", columns="model", values="__val", aggfunc="mean")
    piv = piv.reindex(index=queries, columns=models)
    raw = piv.to_numpy(dtype=float)
    arr = np.ma.masked_invalid(raw)

    fig, ax = plt.subplots(figsize=(1.15 * len(models) + 2.5, 0.42 * len(queries) + 1.8))
    cmap = _PASS_CMAP if discrete else _masked_cmap(cmap_name)
    im = ax.imshow(arr, cmap=cmap, vmin=0, vmax=1, aspect="auto")

    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(models, rotation=30, ha="right")
    ax.set_yticks(range(len(queries)))
    ax.set_yticklabels(queries)
    ax.set_title(title or value)

    for i in range(len(queries)):
        for j in range(len(models)):
            v = raw[i, j]
            if np.isnan(v):
                ax.text(j, i, "n/a", ha="center", va="center", fontsize=7, color="dimgray")
            elif discrete:
                ax.text(j, i, "✓" if v >= 0.5 else "✗", ha="center", va="center", fontsize=9, color="white")
            else:
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7, color=("white" if v < 0.6 else "black"))

    if discrete:
        handles = [
            matplotlib.patches.Patch(color="#2ca02c", label="pass"),
            matplotlib.patches.Patch(color="#d62728", label="fail"),
            matplotlib.patches.Patch(color="lightgrey", label="n/a"),
        ]
        ax.legend(handles=handles, bbox_to_anchor=(1.01, 1.0), loc="upper left", fontsize=8)
    else:
        fig.colorbar(im, ax=ax, shrink=0.85, label=cbar_label or value)
    fig.tight_layout()
    return _save(fig, path)


def component_f1_by_model(df: pd.DataFrame, comp_cols: list[str] | None = None, path: Path | str | None = None,
                          title: str = "Component F1 by model"):
    """Model (rows) x per-clause-F1 (columns) heatmap - where each model is weak."""
    comp_cols = comp_cols or COMPONENT_F1_COLS
    present = [c for c in comp_cols if c in df.columns]
    if not _has_data(df, present):
        return _no_data("Component F1 per model")
    models = model_axis(df)
    heat = _per_model_means(df, present, models)
    raw = heat.to_numpy(dtype=float)
    arr = np.ma.masked_invalid(raw)

    fig, ax = plt.subplots(figsize=(1.0 * len(present) + 2.5, 0.6 * len(models) + 2))
    im = ax.imshow(arr, cmap=_masked_cmap("viridis"), vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(present)))
    ax.set_xticklabels([c.replace("f1_", "") for c in present], rotation=30, ha="right")
    ax.set_yticks(range(len(models)))
    ax.set_yticklabels(models)
    ax.set_title(title)
    for i in range(len(models)):
        for j in range(len(present)):
            v = raw[i, j]
            txt = "n/a" if np.isnan(v) else f"{v:.2f}"
            color = "dimgray" if np.isnan(v) else ("white" if v < 0.6 else "black")
            ax.text(j, i, txt, ha="center", va="center", fontsize=7, color=color)
    fig.colorbar(im, ax=ax, shrink=0.85, label="F1")
    fig.tight_layout()
    return _save(fig, path)


def distance_boxplots(
    df: pd.DataFrame,
    path: Path | str | None = None,
    *,
    title: str = "Edit distance by model (validated only, lower = better)",
):
    """Per-model distribution (box per model) of the three edit distances (lower better).

    Restricted to validated translations. One subplot per distance metric; a model with
    no points for a metric is simply absent from that subplot.
    """
    cols = [c for c in ["levenshtein", "jaccard", "normalized_ted"] if c in df.columns]
    if "validation_passed" in df.columns:
        sub = df[_numeric(df["validation_passed"]) >= 0.5]
    else:
        sub = df
    if not cols or sub.empty or not _has_data(sub, cols):
        return _no_data("Distance by model")

    models = model_axis(sub)
    fig, axes = plt.subplots(1, len(cols), figsize=(4 * len(cols), 4), squeeze=False)
    for k, metric in enumerate(cols):
        ax = axes[0][k]
        present_models = [m for m in models if _numeric(sub.loc[sub["model"] == m, metric]).notna().any()]
        data = [_numeric(sub.loc[sub["model"] == m, metric]).dropna().to_numpy() for m in present_models]
        if data:
            ax.boxplot(data)
            ax.set_xticks(range(1, len(present_models) + 1))
            ax.set_xticklabels(present_models, rotation=30, ha="right")
        ax.set_title(metric)
        ax.set_ylim(-0.02, 1.02)
        ax.grid(axis="y", alpha=0.3)
    fig.suptitle(title)
    fig.tight_layout()
    return _save(fig, path)


def cost_latency(df: pd.DataFrame, path: Path | str | None = None):
    """Operational comparison per model: mean duration, total USD cost, mean iterations."""
    metrics = [
        ("duration_seconds", "mean duration (s)", "mean"),
        ("cost_usd", "total cost (USD)", "sum"),
        ("iterations_used", "mean iterations", "mean"),
    ]
    present = [(c, lab, agg) for c, lab, agg in metrics if c in df.columns and _numeric(df[c]).notna().any()]
    if not present:
        return _no_data("Cost & latency")
    models = model_axis(df)
    colors = model_colors(models)
    fig, axes = plt.subplots(1, len(present), figsize=(4 * len(present), 4), squeeze=False)
    for k, (col, label, agg) in enumerate(present):
        ax = axes[0][k]
        grouped = _numeric(df[col]).groupby(df["model"])
        series = (grouped.mean() if agg == "mean" else grouped.sum()).reindex(models)
        ax.bar(range(len(models)), series.to_numpy(dtype=float), color=[colors[m] for m in models])
        ax.set_xticks(range(len(models)))
        ax.set_xticklabels(models, rotation=30, ha="right")
        ax.set_title(label)
        ax.grid(axis="y", alpha=0.3)
    fig.suptitle("Cost & latency per model")
    fig.tight_layout()
    return _save(fig, path)


def passrate_by_difficulty(df: pd.DataFrame, value: str = "pass_at_1", path: Path | str | None = None):
    """Grouped bars of ``value`` per difficulty (x) and model (series): does a model degrade on hard queries?"""
    if value not in df.columns or "difficulty" not in df.columns or not _numeric(df[value]).notna().any():
        return _no_data(f"{value} by difficulty")
    models = model_axis(df)
    tmp = df.assign(__val=_numeric(df[value]))
    piv = tmp.pivot_table(index="difficulty", columns="model", values="__val", aggfunc="mean")
    piv = piv.reindex(index=[d for d in DIFF_ORDER if d in piv.index], columns=models)
    colors = [model_colors(models)[m] for m in piv.columns]
    ax = piv.plot(kind="bar", figsize=(8, 4.5), ylim=(0, 1.05), color=colors, width=0.82)
    ax.set_ylabel(value)
    ax.set_xlabel("difficulty")
    ax.set_title(f"{value} by difficulty")
    ax.set_xticklabels(piv.index, rotation=0)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(title="model", bbox_to_anchor=(1.01, 1.0), loc="upper left", fontsize=8)
    return _save(ax.figure, path)
