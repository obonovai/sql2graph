"""Offline tests for the shared metric-frame helpers (harness.frames).

These lock the scaffolding the result notebooks (02-05) build on -- the per-record metric
loop, model ordering, feature explosion, behavioural-column derivation, the gold-vs-self
identity guard, and CSV save -- without needing any LLM or database.
"""

from __future__ import annotations

import pandas as pd
import pytest
from harness import frames


def _records() -> list[dict]:
    base = {"dataset": "ldbc", "target": "cypher", "difficulty": "easy", "expected_query": "MATCH (n) RETURN n"}
    return [
        {**base, "model": "qwen3-coder:30b", "query_id": "q1", "generated_query": "MATCH (n) RETURN n"},
        {**base, "model": "claude-opus-4-8", "query_id": "q2", "difficulty": "hard", "generated_query": None},
    ]


def test_order_frame_models_is_canonical_not_alphabetical() -> None:
    df = pd.DataFrame({"model": ["claude-opus-4-8", "llama3.2:latest", "qwen3-coder:30b"]})
    cats = list(frames.order_frame_models(df)["model"].cat.categories)
    assert cats == ["llama3.2:latest", "qwen3-coder:30b", "claude-opus-4-8"]


def test_compute_metrics_frame_fills_missing_and_keeps_order() -> None:
    df = frames.compute_metrics_frame(_records(), lambda *_: {"score": 1.0}, {"score": 0.0})
    assert list(df["score"]) == [1.0, 0.0]  # q2 has no generated query -> missing fill
    assert list(df["query_id"]) == ["q1", "q2"]  # input order preserved
    assert bool(df["thinking_used"].iloc[0]) is False  # extra field defaulted


def test_compute_metrics_frame_extra_fields() -> None:
    recs = [{**_records()[0], "validation_passed": True}]
    df = frames.compute_metrics_frame(recs, lambda *_: {"score": 1.0}, {"score": 0.0}, extra=("validation_passed",))
    assert bool(df["validation_passed"].iloc[0]) is True


def test_by_feature_groups_by_target_feature_and_feature_only() -> None:
    df = pd.DataFrame({"target": ["cypher", "cypher"], "query_id": ["q1", "q2"], "val": [1.0, 0.0]})
    feats = {"q1": ["join"], "q2": ["join", "union"]}
    g = frames.by_feature(df, ["val"], feats)
    assert round(g.loc[("cypher", "join"), "val"], 3) == 0.5  # mean of q1, q2
    assert g.loc[("cypher", "union"), "val"] == 0.0
    gf = frames.by_feature(df, ["val"], feats, group=("feature",))
    assert round(gf.loc["join", "val"], 3) == 0.5 and list(gf.index.names) == ["feature"]


def test_add_behavioural_columns_derives_pass_and_cost() -> None:
    df = pd.DataFrame(
        [
            {
                "model": "qwen3-coder:30b",
                "provider": "ollama",
                "validation_passed": True,
                "iterations_used": 1,
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        ]
    )
    out = frames.add_behavioural_columns(df)
    assert bool(out["pass_at_1"].iloc[0]) is True
    assert int(out["billed_input_tokens"].iloc[0]) == 100
    assert out["cost_usd"].iloc[0] == 0.0  # ollama is free


def test_identity_check_passes_trivially_and_raises_on_drift() -> None:
    frames.identity_check([("ok", lambda *_: True, True)])
    with pytest.raises(AssertionError):
        frames.identity_check([("bad", lambda *_: 0.5, 0.0)])


def test_query_results_and_summary_by_model() -> None:
    df = pd.DataFrame(
        {
            "target": ["cypher", "cypher"],
            "model": ["qwen3-coder:30b", "qwen3-coder:30b"],
            "query_id": ["q2", "q1"],
            "difficulty": ["easy", "hard"],
            "score": [0.4, 0.6],
        }
    )
    df = frames.order_frame_models(df)
    assert round(frames.summary_by_model(df, "cypher", ["score"]).loc["qwen3-coder:30b", "score"], 3) == 0.5
    assert list(frames.query_results(df, "cypher", "qwen3-coder:30b", ["score"])["query_id"]) == ["q1", "q2"]
    assert frames.summary_by_model(df, "aql", ["score"]) is None  # empty cell


def test_save_metrics_csv_drops_columns(tmp_path) -> None:
    df = pd.DataFrame({"model": ["qwen3-coder:30b"], "x": [1.0], "drop_me": [9]})
    n = frames.save_metrics_csv(df, tmp_path / "m.csv", drop=("drop_me",))
    got = pd.read_csv(tmp_path / "m.csv")
    assert n == 1 and "drop_me" not in got.columns and "x" in got.columns
