"""Final-report assembly for notebook 06.

Notebook 06 joins the four per-record metric CSVs, renders per-target figures, and writes
``reports/final.md`` -- one dedicated section per target (headline + stratified tables +
figure embeds + a manual error-taxonomy template), targets never combined. The join and the
markdown-building that used to sprawl across two dense notebook cells live here, so the
notebook keeps only its per-target display/figure cells and a thin call to
:func:`build_final_report`.

Every function is pure ``(frame, ...) -> frame | str`` (no disk writes, no ``datetime.now``),
so the report is reproducible: the notebook passes the timestamp in and writes the returned
string itself. Table rendering uses ``tabulate`` (GitHub-flavoured markdown), matching the
curated ``eval`` extra.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from tabulate import tabulate

from .config import COMPONENT_F1_COLS, DIFF_ORDER, order_models
from .frames import STRAT_KEYS, by_feature

# Figure (filename suffix, heading) pairs, in report order. A target section embeds each figure
# that exists on disk for that target's prefix, so a partial run (e.g. execution figures absent)
# simply omits the missing ones.
REPORT_FIGURES: list[tuple[str, str]] = [
    ("model_headline", "Per-model headline metrics"),
    ("query_model_pass", "Pass / fail by query x model"),
    ("query_model_f1", "Component F1 by query x model"),
    ("query_model_exec", "Execution accuracy by query x model"),
    ("component_f1", "Component F1 per model"),
    ("query_model_ted", "Normalised TED by query x model (lower is better)"),
    ("cost_latency", "Cost and latency per model"),
    ("passrate_by_difficulty", "Pass@1 by difficulty"),
]

# Reader-facing target names. Targets are reported one section each, never combined.
TARGET_LABELS: dict[str, str] = {
    "cypher": "SQL -> Cypher",
    "aql": "SQL -> AQL",
    "gremlin": "SQL -> Gremlin",
}

# Headline "higher = better" columns (plus normalized_ted, a distance shown for context); the
# execution pair is appended only when notebook 05 has produced execution metrics.
_PRIMARY_BASE = ["validation_passed", "pass_at_1", "component_f1_overall", "normalized_ted"]
_PRIMARY_EXEC = ["execution_accuracy", "result_f1"]


def primary_cols(has_exec: bool) -> list[str]:
    """The stratified-table metric columns; the execution pair only when execution metrics exist."""
    return _PRIMARY_BASE + (_PRIMARY_EXEC if has_exec else [])


def _model_axis(sub: pd.DataFrame) -> list[str]:
    """Models present in ``sub`` in canonical order (the report's row/order axis)."""
    return order_models(sub["model"].dropna().unique().tolist())


def join_metric_csvs(
    behavioural: Path,
    structural: Path,
    distance: Path,
    execution: Path | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Outer-join the per-record metric CSVs on the stratification keys.

    The behavioural / structural / distance CSVs are always present (DB-free); the execution
    CSV is left-joined when it exists (only its new columns, to avoid clashing on shared keys).
    Returns ``(df, has_exec)`` where ``has_exec`` says whether execution accuracy is available.
    """
    beh = pd.read_csv(behavioural)
    stru = pd.read_csv(structural)
    dist = pd.read_csv(distance)
    execm = pd.read_csv(execution) if execution is not None and execution.exists() else None

    df = beh.merge(stru, on=STRAT_KEYS, how="outer").merge(dist, on=STRAT_KEYS, how="outer")
    if execm is not None:
        exec_key = [k for k in STRAT_KEYS if k in execm.columns]
        exec_cols = exec_key + [c for c in execm.columns if c not in df.columns]
        df = df.merge(execm[exec_cols], on=exec_key, how="left")
    has_exec = execm is not None and "execution_accuracy" in df.columns
    return df, has_exec


def md_table(d: pd.DataFrame, floatfmt: str = ".3f") -> str:
    """Render a frame as a GitHub-flavoured markdown table (index becomes the first column)."""
    return tabulate(d.reset_index(), headers="keys", tablefmt="github", floatfmt=floatfmt, showindex=False)


def headline(sub: pd.DataFrame, has_exec: bool) -> pd.DataFrame:
    """Per-model headline metrics for one target (validation pass rate, pass@1, F1, TED, +exec)."""
    h = pd.DataFrame(index=pd.Index(_model_axis(sub), name="model"))
    g = sub.groupby("model", observed=True)
    h["validation_pass_rate"] = g["validation_passed"].mean()
    h["pass@1"] = g["pass_at_1"].mean()
    h["component_f1"] = g["component_f1_overall"].mean()
    h["normalized_ted"] = g["normalized_ted"].mean()
    if has_exec:
        h["execution_accuracy"] = g["execution_accuracy"].mean()
        h["result_f1"] = g["result_f1"].mean()
    return h


def by_difficulty(sub: pd.DataFrame, has_exec: bool) -> pd.DataFrame:
    """Mean of the primary metrics per difficulty (easy -> medium -> hard) for one target."""
    s = sub.copy()
    s["difficulty"] = pd.Categorical(s["difficulty"], DIFF_ORDER, ordered=True)
    return s.groupby("difficulty", observed=True)[primary_cols(has_exec)].mean()


def cost_latency(sub: pd.DataFrame) -> pd.DataFrame | None:
    """Per-model mean duration, total USD cost, mean iterations; ``None`` if cost columns absent."""
    if "duration_seconds" not in sub.columns:
        return None
    g = sub.groupby("model", observed=True)
    return pd.DataFrame(
        {
            "mean_duration_s": g["duration_seconds"].mean(),
            "total_cost_usd": g["cost_usd"].sum(),
            "mean_iterations": g["iterations_used"].mean(),
        }
    ).reindex(_model_axis(sub))


def failures(sub: pd.DataFrame, has_exec: bool) -> pd.DataFrame:
    """The per-target failure list for manual error-taxonomy annotation.

    A record fails if it did not validate or (when execution ran) did not reproduce the oracle
    rows. Empty ``category`` / ``notes`` columns are added for hand annotation.
    """
    mask = ~sub["validation_passed"].astype(bool)
    if has_exec:
        mask = mask | (sub["execution_accuracy"].fillna(1.0) < 1.0)
    fcols = ["model", "query_id", "difficulty", "validation_passed", "component_f1_overall", "normalized_ted"]
    f = sub[mask][fcols].copy()
    f["category"] = ""
    f["notes"] = ""
    return f.sort_values(["query_id", "model"]).reset_index(drop=True)


def target_section(
    sub: pd.DataFrame,
    prefix: str,
    label: str,
    *,
    has_exec: bool,
    features: dict[str, list[str]],
    figures_dir: Path,
) -> str:
    """Build one target's full markdown section (headline, stratified tables, figures, failures)."""
    out = [f"\n## {label}\n"]
    out.append(f"Translations: **{len(sub)}** ({int(sub['validation_passed'].sum())} validated)\n")
    out.append("\n### Headline (per model)\n")
    out.append(md_table(headline(sub, has_exec)) + "\n")
    out.append("\n### Stratified by difficulty\n")
    out.append(md_table(by_difficulty(sub, has_exec)) + "\n")
    out.append("\n### Stratified by SQL feature\n")
    out.append(md_table(by_feature(sub, primary_cols(has_exec), features, group=("feature",))) + "\n")
    out.append("\n### Component F1 breakdown (per model)\n")
    out.append(md_table(sub.groupby("model", observed=True)[COMPONENT_F1_COLS].mean().reindex(_model_axis(sub))) + "\n")
    cl = cost_latency(sub)
    if cl is not None:
        out.append("\n### Cost and latency (per model)\n")
        out.append(md_table(cl) + "\n")
    out.append("\n### Figures\n")
    out.extend(
        f"![{label}: {heading}](figures/{prefix}_{suffix}.png)\n"
        for suffix, heading in REPORT_FIGURES
        if (figures_dir / f"{prefix}_{suffix}.png").exists()
    )
    out.append("\n### Error taxonomy (fill in manually)\n")
    out.append(
        "Categories: schema_error, hallucination, direction_error, predicate_error, "
        "projection_error, aggregation_error, join_to_path_error, other.\n\n"
    )
    out.append(md_table(failures(sub, has_exec), floatfmt=".2f") + "\n")
    return "\n".join(out)


# Execution-metric caveats appended after the per-target sections when execution ran.
_EXEC_CAVEATS = [
    "\n## Execution-metric caveats\n",
    "Oracle = gold SQL on Postgres vs generated query on the graph DB (multiset compare).\n\n",
    "- Query timeout: each generated query runs under a **180s per-query ceiling** "
    "(`EVAL_QUERY_TIMEOUT`); a query killed at the ceiling scores 0 even if it would return "
    "the correct rows given more time. `translated_runtime_s` keeps per-query speed visible.\n",
    "- Date reconciliation: Postgres timestamps, Neo4j native temporals, and "
    "ArangoDB/Gremlin ISO-8601 strings are canonicalised to epoch-millis on all sides.\n",
    "- AQL/Gremlin empty text: absent optional text is reconciled to Postgres NULL.\n",
    "- Vacuous matches: when both stores return 0 rows, execution_accuracy is 1.0 even if the "
    "generated query has a latent bug.\n",
]
_OUT_OF_SCOPE = [
    "\n## Out of scope (this pass)\n",
    "- Execution-based metrics (not yet run; need the graphonauts databases, see notebook 05).\n",
]


def build_final_report(
    df: pd.DataFrame,
    *,
    models: list[str],
    targets: list[str],
    has_exec: bool,
    features: dict[str, list[str]],
    figures_dir: Path,
    generated_at: str,
    target_labels: dict[str, str] | None = None,
) -> str:
    """Assemble the whole ``final.md`` markdown string (header + per-target sections + caveats).

    ``generated_at`` is passed in (not read from the clock) so the build is reproducible. The
    notebook writes the returned string to :data:`~harness.config.FINAL_REPORT_MD`.
    """
    labels = target_labels or TARGET_LABELS
    total_records = len(df)
    validated = int(df["validation_passed"].sum())
    total_in = int(df["billed_input_tokens"].sum()) if "billed_input_tokens" in df.columns else 0
    total_out = int(df["output_tokens"].sum()) if "output_tokens" in df.columns else 0
    total_cost = float(df["cost_usd"].sum()) if "cost_usd" in df.columns else 0.0

    parts = ["# sql2graph evaluation report\n"]
    parts.append(f"Generated: {generated_at}\n")
    parts.append(f"Models under evaluation: **{', '.join(models)}**\n")
    parts.append(f"Targets: **{', '.join(labels.get(t, t) for t in targets)}**\n")
    parts.append(f"Total translations: **{total_records}** ({validated} validated)\n")
    parts.append(
        f"Total tokens: **{total_in:,}** input / **{total_out:,}** output, approx **${total_cost:,.2f}** USD\n"
    )
    parts.append("\nResults are reported per target below; targets are never combined in one table or figure.\n")
    parts.extend(
        target_section(
            df[df["target"] == t], t, labels.get(t, t), has_exec=has_exec, features=features, figures_dir=figures_dir
        )
        for t in targets
    )
    parts.extend(_EXEC_CAVEATS if has_exec else _OUT_OF_SCOPE)
    return "\n".join(parts)
