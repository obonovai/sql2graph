"""Translation run: drive the translator over a RunConfig and record results.

This is the only part of the harness that calls the LLM. :func:`translate_one` is
the single-query primitive: it runs one SQL query for a
:class:`~harness.config.RunConfig` through :class:`~sql2graph.SQLTranslator` and
returns one :class:`AttemptRecord` (as a dict). Notebook 01 builds the resumable
run loop on top of it -- writing ``records_<dataset>_<target>_<model>.json``
incrementally so a crash mid-run preserves prior work -- so that flow reads in the
notebook. Token counts come straight from ``result.token_usage`` (the library
reports them first-class; no log scraping). Stratification keys (dataset / target /
model / provider) live on every record so the metric notebooks can glob all record
files and slice the matrix.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from sql2graph import (
    AnthropicConfig,
    OllamaConfig,
    PreflightAction,
    SQLTranslator,
    make_llm,
    make_target,
    make_validator,
)

from .config import RunConfig, default_validation_mode, records_filename


@dataclass
class AttemptRecord:
    # --- stratification keys (the matrix) ---
    dataset: str
    query_id: str
    target: str
    model: str
    provider: str
    difficulty: str
    sql_features: list[str]
    # --- inputs ---
    sql: str
    expected_query: str
    # --- TranslationResult fields ---
    generated_query: str | None
    validation_passed: bool
    validation_errors: list[str]
    iterations_used: int
    status: str
    duration_seconds: float
    unmapped_tables: list[str]
    unmapped_columns: list[str]
    # --- token usage (from result.token_usage) ---
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    total_tokens: int
    # True if the model produced an extended-thinking block on any iteration of this
    # translation (exact, from the Anthropic response). Always False for Ollama.
    thinking_used: bool
    # --- harness error (e.g. backend unreachable), distinct from validation ---
    error: str | None


def make_llm_for(rc: RunConfig):
    """Build an LLM client for ``rc`` (constructs the config in-code, no YAML)."""
    if rc.provider == "ollama":
        # host omitted -> the ollama SDK resolves $OLLAMA_HOST (its default when unset).
        return make_llm(
            OllamaConfig(
                model=rc.model,
                num_ctx=rc.num_ctx,
                temperature=rc.temperature,
            )
        )
    if rc.provider == "anthropic":
        # api_key omitted -> SDK reads $ANTHROPIC_API_KEY. thinking/effort default to
        # off/None so the plain opus rows are unchanged; the thinking variant sets them
        # (adaptive + xhigh) plus a larger max_output_tokens (thinking spends the same
        # output budget). The real model id (rc.model) is what hits the API -- rc.label
        # is only a stratification key for records/metrics.
        return make_llm(
            AnthropicConfig(
                model=rc.model,
                temperature=rc.temperature,
                max_output_tokens=rc.max_output_tokens,
                thinking=rc.thinking,
                effort=rc.effort,
            )
        )
    raise ValueError(f"Unknown provider: {rc.provider!r}")


@contextmanager
def make_translator_for(rc: RunConfig, mapping) -> Iterator[SQLTranslator]:
    """A fresh translator for one work item (chat-history isolation).

    Resolves the validation mode per target so an AQL run never requests the
    nonexistent AQL syntax validator. Pre-flight is set to WARN (not REJECT) so a
    query that names an unmapped column is still attempted and its warning
    recorded, rather than aborting before the LLM call.
    """
    llm = make_llm_for(rc)
    target = make_target(rc.target)
    mode = default_validation_mode(rc.target, rc.validation_mode)
    validator = make_validator(rc.target, mode, server_config=rc.server_config)
    with SQLTranslator(
        mapping,
        llm,
        target,
        validator,
        max_iterations=rc.max_iterations,
        unmapped_tables_action=PreflightAction.WARN,
        unmapped_columns_action=PreflightAction.WARN,
    ) as translator:
        yield translator


def records_path(rc: RunConfig) -> Path:
    """The per-cell records file for ``rc`` (``records_<dataset>_<target>_<model>.json``)."""
    return rc.records_dir / records_filename(rc)


def write_records(path: Path, records: list[dict]) -> None:
    """Write ``records`` to ``path`` as indented JSON (called after every item, crash-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, default=str))


def translate_one(rc: RunConfig, item, mapping) -> dict:
    """Translate one work item and return its :class:`AttemptRecord` as a dict.

    The single-query primitive: it calls the LLM once (a fresh translator per item, for
    chat-history isolation), captures the exact ``token_usage``, and never touches disk.
    Notebook 01's resume loop builds on it, so the "run the matrix" flow is visible in the
    notebook while the per-query mechanics live here.
    """
    error_message: str | None = None
    result = None
    try:
        with make_translator_for(rc, mapping) as translator:
            result = translator.translate(item.sql)
    except Exception as exc:  # backend unreachable, model missing, etc.
        error_message = f"{type(exc).__name__}: {exc}"

    usage = result.token_usage if result else None
    record = AttemptRecord(
        dataset=rc.dataset,
        query_id=item.query_id,
        target=rc.target,
        # Stratify on the label when set (e.g. the thinking variant) so it reads
        # as its own model across every metric notebook; falls back to rc.model.
        model=rc.label or rc.model,
        provider=rc.provider,
        difficulty=item.difficulty,
        sql_features=item.sql_features,
        sql=item.sql,
        expected_query=item.expected_query,
        generated_query=result.generated_query if result else None,
        validation_passed=bool(result and result.validation_passed),
        validation_errors=result.validation_errors if result else [],
        iterations_used=result.iterations_used if result else 0,
        status=result.status if result else "error",
        duration_seconds=result.duration_seconds if result else 0.0,
        unmapped_tables=result.unmapped_tables if result else [],
        unmapped_columns=result.unmapped_columns if result else [],
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        cache_read_tokens=usage.cache_read_tokens if usage else 0,
        cache_creation_tokens=usage.cache_creation_tokens if usage else 0,
        total_tokens=usage.total_tokens if usage else 0,
        thinking_used=usage.thinking_used if usage else False,
        error=error_message,
    )
    return asdict(record)
