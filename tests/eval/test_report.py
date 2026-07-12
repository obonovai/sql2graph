"""Offline tests for the final-report assembly (harness.report).

These lock the CSV join and the markdown-building notebook 06 delegates to, without needing
any metric CSV on disk or a database. The byte-for-byte fidelity against the committed
``reports/final.md`` is checked by re-running notebook 06; here we cover the structure.
"""

from __future__ import annotations

import pandas as pd
from harness import report

_F1_COLS = {
    c: 0.8
    for c in [
        "f1_node_labels",
        "f1_edge_types",
        "f1_directions",
        "f1_where",
        "f1_return",
        "f1_order",
        "f1_limit",
        "f1_aggregations",
    ]
}


def _joined() -> pd.DataFrame:
    common = {"dataset": "ldbc", "target": "cypher", "model": "qwen3-coder:30b", **_F1_COLS}
    return pd.DataFrame(
        [
            {
                **common,
                "query_id": "q1",
                "difficulty": "easy",
                "validation_passed": True,
                "pass_at_1": True,
                "component_f1_overall": 0.9,
                "normalized_ted": 0.1,
                "duration_seconds": 1.0,
                "cost_usd": 0.0,
                "iterations_used": 1,
                "billed_input_tokens": 100,
                "output_tokens": 10,
            },
            {
                **common,
                "query_id": "q2",
                "difficulty": "hard",
                "validation_passed": False,
                "pass_at_1": False,
                "component_f1_overall": 0.2,
                "normalized_ted": 0.7,
                "duration_seconds": 2.0,
                "cost_usd": 0.0,
                "iterations_used": 3,
                "billed_input_tokens": 200,
                "output_tokens": 20,
            },
        ]
    )


def test_primary_cols_appends_execution_only_when_present() -> None:
    assert report.primary_cols(False) == ["validation_passed", "pass_at_1", "component_f1_overall", "normalized_ted"]
    assert report.primary_cols(True)[-2:] == ["execution_accuracy", "result_f1"]


def test_headline_and_failures() -> None:
    df = _joined()
    h = report.headline(df, has_exec=False)
    assert round(h.loc["qwen3-coder:30b", "pass@1"], 3) == 0.5
    assert "execution_accuracy" not in h.columns
    f = report.failures(df, has_exec=False)
    assert list(f["query_id"]) == ["q2"]  # only the record that did not validate
    assert {"category", "notes"} <= set(f.columns)


def test_join_metric_csvs_without_execution(tmp_path) -> None:
    df = _joined()
    key = ["dataset", "target", "model", "query_id", "difficulty"]
    df[
        key
        + [
            "validation_passed",
            "pass_at_1",
            "billed_input_tokens",
            "output_tokens",
            "cost_usd",
            "duration_seconds",
            "iterations_used",
        ]
    ].to_csv(tmp_path / "b.csv", index=False)
    df[key + ["component_f1_overall"]].to_csv(tmp_path / "s.csv", index=False)
    df[key + ["normalized_ted"]].to_csv(tmp_path / "d.csv", index=False)
    joined, has_exec = report.join_metric_csvs(
        tmp_path / "b.csv", tmp_path / "s.csv", tmp_path / "d.csv", tmp_path / "no.csv"
    )
    assert has_exec is False
    assert len(joined) == 2 and "component_f1_overall" in joined.columns and "normalized_ted" in joined.columns


def test_target_section_embeds_only_existing_figures(tmp_path) -> None:
    df = _joined()
    (tmp_path / "cypher_model_headline.png").write_bytes(b"x")
    sec = report.target_section(
        df, "cypher", "SQL -> Cypher", has_exec=False, features={"q1": ["join"], "q2": ["union"]}, figures_dir=tmp_path
    )
    assert "figures/cypher_model_headline.png" in sec
    assert "figures/cypher_query_model_pass.png" not in sec  # never written


def test_build_final_report_structure(tmp_path) -> None:
    md = report.build_final_report(
        _joined(),
        models=["qwen3-coder:30b"],
        targets=["cypher"],
        has_exec=False,
        features={"q1": ["join"], "q2": ["union"]},
        figures_dir=tmp_path,
        generated_at="2020-01-01T00:00:00",
    )
    assert md.startswith("# sql2graph evaluation report")
    assert "Generated: 2020-01-01T00:00:00" in md
    assert "## SQL -> Cypher" in md
    assert "## Out of scope (this pass)" in md  # execution absent
