"""Translation run: drive the translator over a RunConfig and record results.

This is the only part of the harness that calls the LLM. :func:`run_translation`
builds the work list for one :class:`~harness.config.RunConfig`, runs each
SQL query through :class:`~rows2graph.SQLTranslator`, and writes one
:class:`AttemptRecord` per query to ``records_<dataset>_<target>_<model>.json``,
incrementally so a crash mid-run preserves prior work. Token counts come straight
from ``result.token_usage`` (the library reports them first-class; no log
scraping). Stratification keys (dataset / target / model / provider) live on every
record so the metric notebooks can glob all record files and slice the matrix.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from rows2graph import (
    AnthropicConfig,
    OllamaConfig,
    PreflightAction,
    SQLTranslator,
    make_llm,
    make_target,
    make_validator,
)

from .config import RunConfig, default_validation_mode, records_filename
from .datasets import build_work_items, mapping_for


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
    # --- harness error (e.g. backend unreachable), distinct from validation ---
    error: str | None


def make_llm_for(rc: RunConfig):
    """Build an LLM client for ``rc`` (constructs the config in-code, no YAML)."""
    if rc.provider == "ollama":
        return make_llm(
            OllamaConfig(
                model=rc.model,
                host=rc.host,
                num_ctx=rc.num_ctx,
                temperature=rc.temperature,
            )
        )
    if rc.provider == "anthropic":
        # api_key omitted -> SDK reads $ANTHROPIC_API_KEY.
        return make_llm(AnthropicConfig(model=rc.model, temperature=rc.temperature))
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


def _records_path(rc: RunConfig) -> Path:
    return rc.outputs_dir / records_filename(rc)


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps(records, indent=2, default=str))


def run_translation(rc: RunConfig) -> list[dict]:
    """Run every work item for ``rc`` and return all records for this cell.

    Resumes from ``records_<...>.json`` when ``rc.resume`` is set: query ids
    already on disk are skipped (the filename already pins dataset/target/model,
    so query_id is a sufficient key within the file).
    """
    rc.outputs_dir.mkdir(parents=True, exist_ok=True)
    path = _records_path(rc)

    existing: list[dict] = []
    done: set[str] = set()
    if rc.resume and path.exists():
        existing = json.loads(path.read_text())
        done = {r["query_id"] for r in existing}
        print(f"Resume: {len(existing)} record(s) on disk for {path.name}; {len(done)} query id(s) done.")

    work = [w for w in build_work_items(rc) if w.query_id not in done]
    print(f"{rc.dataset}/{rc.target}/{rc.model}: {len(work)} item(s) to translate.")

    mapping = mapping_for(rc.dataset)
    records: list[dict] = list(existing)

    for idx, item in enumerate(work, start=1):
        print(f"[{idx:3d}/{len(work)}] {item.query_id} -> {rc.target}", end=" ", flush=True)
        error_message: str | None = None
        result = None
        try:
            with make_translator_for(rc, mapping) as translator:
                result = translator.translate(item.sql)
        except Exception as exc:  # backend unreachable, model missing, etc.
            error_message = f"{type(exc).__name__}: {exc}"
            print(f"ERROR ({error_message})")

        usage = result.token_usage if result else None
        record = AttemptRecord(
            dataset=rc.dataset,
            query_id=item.query_id,
            target=rc.target,
            model=rc.model,
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
            error=error_message,
        )
        records.append(asdict(record))
        _write_records(path, records)

        if error_message is None:
            marker = "ok" if record.validation_passed else "x "
            # Billed input = uncached input + both Anthropic cache buckets, so
            # the log matches the reports (and platform.claude.com "tokens in").
            billed_in = record.input_tokens + record.cache_read_tokens + record.cache_creation_tokens
            print(
                f"{marker} iters={record.iterations_used} "
                f"tokens=({billed_in:>6},{record.output_tokens:>4}) "
                f"{record.duration_seconds:5.1f}s status={record.status}"
            )

    print(f"Done: {len(records)} record(s) in {path}")
    return records


def load_records(
    outputs_dir: Path,
    dataset: str | None = None,
    target: str | None = None,
    model: str | None = None,
) -> list[dict]:
    """Concatenate every ``records_*.json`` under ``outputs_dir``, optionally filtered."""
    records: list[dict] = []
    for path in sorted(outputs_dir.glob("records_*.json")):
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
