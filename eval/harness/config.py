"""Run configuration and the evaluation matrix.

A :class:`RunConfig` is one cell of the evaluation matrix: a (dataset, target
language, model) triple plus the knobs needed to drive the translator for it.
:data:`RUN_MATRIX` is the list of cells a run executes; extending the evaluation
(another model, another target language, another dataset) is appending rows here,
not editing notebooks.

The single rule the rest of the harness leans on is
:func:`default_validation_mode`: all three targets have an in-process, offline
ANTLR grammar validator ("syntax") -- Cypher and Gremlin from the engines' own
grammars, AQL from a hand-port of ArangoDB's Flex+Bison grammar (best-effort).
Each therefore defaults to "syntax"; pass an override for "server"/"managed"
(which validate against a real, or throwaway, server).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Provider = Literal["ollama", "anthropic"]
Target = Literal["cypher", "aql", "gremlin"]
ValidationMode = Literal["none", "syntax", "server", "managed"]


def _repo_root() -> Path:
    """Walk up from this file to the directory holding ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    # Fallback: harness/ -> eval/ -> repo root
    return here.parents[2]


REPO_ROOT = _repo_root()
EVAL_DIR = REPO_ROOT / "eval"
GOLD_DIR = EVAL_DIR / "gold"
MAPPINGS_DIR = REPO_ROOT / "examples" / "mappings"
OUTPUTS_DIR = EVAL_DIR / "outputs"
RECORDS_DIR = OUTPUTS_DIR / "records"
METRICS_DIR = OUTPUTS_DIR / "metrics"
CACHE_DIR = OUTPUTS_DIR / "cache"
REPORTS_DIR = EVAL_DIR / "reports"
FIGURES_DIR = REPORTS_DIR / "figures"
FINAL_REPORT_MD = REPORTS_DIR / "final.md"

# The filename contract between the notebooks: 01 writes records (see
# records_filename below), 02-05 write one metrics CSV each, 06 joins them.
# Single-sourced here so producer and consumer can never drift.
RECORDS_GLOB = "records_*.json"
METRICS_BEHAVIOURAL_CSV = METRICS_DIR / "metrics_behavioural.csv"
METRICS_STRUCTURAL_CSV = METRICS_DIR / "metrics_structural.csv"
METRICS_DISTANCE_CSV = METRICS_DIR / "metrics_distance.csv"
METRICS_EXECUTION_CSV = METRICS_DIR / "metrics_execution.csv"
EXECUTION_CACHE_PATH = CACHE_DIR / "execution_rows_cache.json"

# Target -> deployment-free default validation mode. All three targets have an
# in-process grammar-based syntax validator (AQL via a hand-port of ArangoDB's
# grammar), so all default to "syntax"; pass an override for server/managed.
DEFAULT_VALIDATION_MODE: dict[Target, ValidationMode] = {
    "cypher": "syntax",
    "gremlin": "syntax",
    "aql": "syntax",
}


def default_validation_mode(target: Target, override: ValidationMode | None = None) -> ValidationMode:
    """The validation mode to use for ``target``, honouring an explicit override."""
    if override is not None:
        return override
    return DEFAULT_VALIDATION_MODE[target]


@dataclass(frozen=True)
class RunConfig:
    """One evaluation-matrix cell: a (dataset, target, model) triple + knobs."""

    dataset: str = "ldbc"
    target: Target = "cypher"
    model: str = "qwen3-coder:30b"
    provider: Provider = "ollama"
    # None -> default_validation_mode(target). Set explicitly to force, e.g.,
    # "server"/"managed" for a Cypher run that should catch schema hallucinations.
    validation_mode: ValidationMode | None = None
    max_iterations: int = 3
    temperature: float = 0.0
    # Anthropic reasoning knobs (ignored for provider="ollama"). thinking="adaptive"
    # turns on extended thinking; effort maps to output_config.effort (Opus 4.7/4.8
    # accept low|medium|high|xhigh|max; None = the API default "high"). label is the
    # stratification id used for the records filename and record["model"], so a
    # thinking variant of a model gets its own record file rather than colliding with
    # -- and resume-skipping -- the plain run. max_output_tokens is raised for the
    # thinking variant, since extended thinking spends the same output budget.
    label: str | None = None
    thinking: Literal["off", "adaptive"] = "off"
    effort: str | None = None
    max_output_tokens: int = 4096
    # Ollama-specific knob (ignored for provider="anthropic"). The Ollama endpoint
    # is not configured here: the SDK resolves $OLLAMA_HOST (its default when unset).
    num_ctx: int = 16384
    records_dir: Path = field(default=RECORDS_DIR)
    # Restrict the run to these query ids (smoke test); None = the whole dataset.
    subset: tuple[str, ...] | None = None
    resume: bool = True
    # A Neo4jConfig/ArangoDBConfig/GremlinConfig when validation_mode == "server".
    server_config: object | None = None


def model_slug(model: str) -> str:
    """Filename-safe model id, e.g. ``qwen3-coder:30b`` -> ``qwen3-coder_30b``."""
    return model.replace(":", "_").replace("/", "_")


def records_filename(rc: RunConfig) -> str:
    """Per-cell records file, e.g. ``records_ldbc_cypher_qwen3-coder_30b.json``.

    Keyed on ``rc.label or rc.model`` so a variant of a model (e.g. the same model
    run with thinking on) writes to its own file instead of overwriting/resuming
    the plain run's records.
    """
    return f"records_{rc.dataset}_{rc.target}_{model_slug(rc.label or rc.model)}.json"


# Stratification labels for reasoning variants (adaptive thinking + effort). A row with
# label="claude-opus-4-8-thinking" still calls the real claude-opus-4-8 on the API, but
# records/metrics/plots stratify on the label, so the thinking run is its own series. The
# notebooks import this to keep the thinking variant *out* of the shared 4-model comparison
# views and show it in a dedicated section instead. Extend it if you add more variants.
THINKING_LABELS: tuple[str, ...] = ("claude-opus-4-8-thinking",)


# The evaluation matrix: LDBC x {cypher, aql, gremlin} x 4 models. Extend by
# appending rows, e.g. a server-validated cell:
#   RunConfig(dataset="ldbc", target="aql", model="qwen3-coder:30b",
#             validation_mode="server", server_config=ArangoDBConfig(...)),
RUN_MATRIX: list[RunConfig] = [
    RunConfig(dataset="ldbc", target="cypher", model="llama3.2:latest", provider="ollama"),
    RunConfig(dataset="ldbc", target="cypher", model="qwen3-coder:30b", provider="ollama"),
    RunConfig(dataset="ldbc", target="cypher", model="gemma4:26b", provider="ollama"),
    RunConfig(dataset="ldbc", target="cypher", model="claude-opus-4-8", provider="anthropic"),
    # AQL rows -- default validation_mode="syntax" (offline grammar; no server needed at
    # generation time). Execution accuracy runs against the mapping-aligned ArangoDB in 05.
    RunConfig(dataset="ldbc", target="aql", model="llama3.2:latest", provider="ollama"),
    RunConfig(dataset="ldbc", target="aql", model="qwen3-coder:30b", provider="ollama"),
    RunConfig(dataset="ldbc", target="aql", model="gemma4:26b", provider="ollama"),
    RunConfig(dataset="ldbc", target="aql", model="claude-opus-4-8", provider="anthropic"),
    # Gremlin rows -- default validation_mode="syntax" (offline TinkerPop grammar).
    # Execution accuracy runs against graphonauts's in-memory TinkerGraph in 05
    # (bring it up with Neo4j/ArangoDB stopped; reload after container restarts).
    RunConfig(dataset="ldbc", target="gremlin", model="llama3.2:latest", provider="ollama"),
    RunConfig(dataset="ldbc", target="gremlin", model="qwen3-coder:30b", provider="ollama"),
    RunConfig(dataset="ldbc", target="gremlin", model="gemma4:26b", provider="ollama"),
    RunConfig(dataset="ldbc", target="gremlin", model="claude-opus-4-8", provider="anthropic"),
    # 5th option: the same claude-opus-4-8, but with adaptive (extended) thinking on
    # and effort="xhigh" -- a fair comparison against the reasoning Ollama models. The
    # `label` stratifies it separately from the plain opus-4-8 rows above (own records
    # file, own series in every metric/plot); the real model sent to the API is still
    # claude-opus-4-8. max_output_tokens is raised so thinking has room to breathe.
    RunConfig(dataset="ldbc", target="cypher", model="claude-opus-4-8", provider="anthropic",
              label="claude-opus-4-8-thinking", thinking="adaptive", effort="xhigh",
              max_output_tokens=16000),
    RunConfig(dataset="ldbc", target="aql", model="claude-opus-4-8", provider="anthropic",
              label="claude-opus-4-8-thinking", thinking="adaptive", effort="xhigh",
              max_output_tokens=16000),
    RunConfig(dataset="ldbc", target="gremlin", model="claude-opus-4-8", provider="anthropic",
              label="claude-opus-4-8-thinking", thinking="adaptive", effort="xhigh",
              max_output_tokens=16000),
]
