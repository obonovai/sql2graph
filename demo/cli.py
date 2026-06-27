"""Parametrized demo CLI for the rows2graph framework.

This script is *outside* the published package by design: it is a reference
client that exercises the public library API. Every component is constructed
through the same factory functions a third-party user would call.

The output is rendered with `rich`: a settings header, the input SQL block,
the generated query block, and a result-summary panel. Pretty output goes to
stdout; logs go to stderr.

Invocation::

    uv run python demo/cli.py \\
        --sql "SELECT name FROM supplier WHERE suppkey = 1337" \\
        --mapping config/mappings/tpch.yaml \\
        --model   config/models/anthropic.yaml \\
        --target  cypher \\
        --validation syntax

Read SQL from stdin by passing ``--sql -``::

    cat my_query.sql | uv run python demo/cli.py --sql - --mapping ... --model ...

Server-side validation (catches label / collection / property hallucinations
in addition to syntactic errors) requires a running graph database and the
matching server config file::

    export NEO4J_PASSWORD=secret
    uv run python demo/cli.py \\
        --sql "..." \\
        --mapping config/mappings/tpch.yaml \\
        --model   config/models/anthropic.yaml \\
        --target  cypher \\
        --validation server \\
        --server  config/servers/neo4j.yaml

Managed validation provisions a throwaway database automatically — select
``--validation server`` and omit ``--server`` (requires a running Docker
daemon; the container starts on first validation and is removed at exit)::

    uv run python demo/cli.py --sql "..." --mapping config/mappings/tpch.yaml --model config/models/anthropic.yaml --target cypher --validation server

Exit codes:
    0 — translation succeeded (validator returned no errors).
    1 — translation reached ``--max-iterations`` without passing validation.
    2 — argument / config error before any LLM call.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import NoReturn

from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from rows2graph import (
    AnthropicConfig,
    ArangoDBConfig,
    AsyncSQLTranslator,
    GremlinConfig,
    Neo4jConfig,
    OllamaConfig,
    SchemaMapping,
    TranslationResult,
    load_model_config,
    load_server_config,
    make_async_llm,
    make_async_validator,
    make_target,
    resolve_validation_mode,
)

# Two consoles: pretty output (stdout) and logs/errors (stderr). Splitting
# the streams keeps shell pipelines well-behaved — the user can redirect
# stdout to capture the generated query block while stderr keeps the
# diagnostic stream visible on the terminal.
console = Console()
err_console = Console(stderr=True)

logger = logging.getLogger("demo.cli")


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser with grouped flags."""
    parser = argparse.ArgumentParser(
        prog="rows2graph-demo",
        description=(
            "Translate a SQL query into a graph database query (Cypher, AQL, or Gremlin) "
            "using the rows2graph framework."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    input_group = parser.add_argument_group("Input")
    input_group.add_argument(
        "--sql",
        required=True,
        help='SQL query to translate. Pass "-" to read from stdin.',
    )
    input_group.add_argument(
        "--mapping",
        required=True,
        type=Path,
        help="Path to a schema-mapping YAML (config/mappings/*.yaml).",
    )

    llm_group = parser.add_argument_group("LLM")
    llm_group.add_argument(
        "--model",
        required=True,
        type=Path,
        help="Path to a model-config YAML (config/models/*.yaml).",
    )

    target_group = parser.add_argument_group("Target language")
    target_group.add_argument(
        "--target",
        choices=("cypher", "aql", "gremlin"),
        default="cypher",
        help="Target graph query language (default: cypher).",
    )

    validation_group = parser.add_argument_group("Validation")
    validation_group.add_argument(
        "--validation",
        choices=("syntax", "server", "none"),
        default="syntax",
        help=(
            "Validation mode (default: syntax). With 'server', pass --server to use "
            "your own database, or omit it to auto-provision a throwaway one."
        ),
    )
    validation_group.add_argument(
        "--server",
        type=Path,
        default=None,
        help=(
            "Path to a server-config YAML (config/servers/*.yaml). Optional: with "
            "--validation=server and no --server, a throwaway database is provisioned "
            "automatically (requires Docker)."
        ),
    )
    validation_group.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum number of generate-validate-fix iterations (default: 3).",
    )

    logging_group = parser.add_argument_group("Logging")
    logging_group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable INFO-level logging on stderr (shows each loop iteration).",
    )

    return parser


# ---------------------------------------------------------------------------
# Loader helpers (delegate validation to library, surface errors via _die)
# ---------------------------------------------------------------------------


def _die(message: str, code: int = 2) -> NoReturn:
    """Print a red error message to stderr and exit."""
    err_console.print(f"[bold red]error:[/bold red] {message}")
    sys.exit(code)


def _read_sql(sql_arg: str) -> str:
    """Return the SQL string — read stdin if --sql is the literal ``-``."""
    if sql_arg == "-":
        return sys.stdin.read()
    return sql_arg


def _load_server_config_or_die(
    args: argparse.Namespace,
) -> Neo4jConfig | ArangoDBConfig | GremlinConfig | None:
    """Load a server config if --validation=server, validating cross-flag invariants."""
    if args.validation != "server":
        return None
    if args.server is None:
        # No config provided: managed mode provisions its own database.
        return None
    server_config = load_server_config(args.server)
    if args.target == "cypher" and not isinstance(server_config, Neo4jConfig):
        _die(
            f"--target=cypher requires a Neo4j server config; "
            f"got type={type(server_config).__name__} from {args.server}"
        )
    if args.target == "aql" and not isinstance(server_config, ArangoDBConfig):
        _die(
            f"--target=aql requires an ArangoDB server config; "
            f"got type={type(server_config).__name__} from {args.server}"
        )
    if args.target == "gremlin" and not isinstance(server_config, GremlinConfig):
        _die(
            f"--target=gremlin requires a Gremlin server config; "
            f"got type={type(server_config).__name__} from {args.server}"
        )
    return server_config


def _load_model_config_or_die(
    args: argparse.Namespace,
) -> OllamaConfig | AnthropicConfig:
    """Load a model config, surfacing missing env vars as a clean error."""
    try:
        return load_model_config(args.model)
    except KeyError as e:
        _die(str(e))


# ---------------------------------------------------------------------------
# Rich output
# ---------------------------------------------------------------------------


# Pygments lexer name per target language. Pygments ships a `cypher` lexer
# but neither AQL nor Gremlin. AQL falls back to plain `text`; Gremlin
# uses the `groovy` lexer since Gremlin-Groovy is its host language and
# the visual match is closer than plain text.
_PYGMENTS_LEXER: dict[str, str] = {
    "cypher": "cypher",
    "aql": "text",
    "gremlin": "groovy",
}


def _syntax(code: str, language: str) -> Syntax:
    """Wrap ``code`` in a Rich ``Syntax`` block with terminal-friendly theme."""
    return Syntax(
        code.strip(),
        language,
        theme="ansi_dark",
        word_wrap=True,
        background_color="default",
    )


def _print_settings(
    *,
    mapping_path: Path,
    mapping: SchemaMapping,
    model_path: Path,
    model_config: OllamaConfig | AnthropicConfig,
    target: str,
    validation: str,
    server_path: Path | None,
    server_config: Neo4jConfig | ArangoDBConfig | GremlinConfig | None,
    max_iterations: int,
) -> None:
    """Top-of-output header: every parameter that shapes the translation."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", justify="right")
    table.add_column()

    table.add_row(
        "Mapping:",
        f"{mapping_path}  [dim]({len(mapping.nodes)} nodes, {len(mapping.edges)} edges)[/dim]",
    )
    table.add_row(
        "Model:",
        f"[cyan]{model_config.provider}[/cyan] / {model_config.model}  [dim]({model_path})[/dim]",
    )
    table.add_row("Target:", f"[cyan]{target}[/cyan]")
    table.add_row(
        "Validation:",
        f"[cyan]{validation}[/cyan]  [dim](max_iterations={max_iterations})[/dim]",
    )
    if server_path is not None and server_config is not None:
        table.add_row(
            "Server:",
            f"[cyan]{server_config.type}[/cyan]  [dim]({server_path})[/dim]",
        )

    console.print(
        Panel(
            table,
            title="[bold]rows2graph demo[/bold]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def _print_input_sql(sql: str) -> None:
    console.print(
        Panel(
            _syntax(sql, "sql"),
            title="[bold]Input SQL[/bold]",
            title_align="left",
            border_style="blue",
            padding=(1, 2),
        )
    )


# Per-role styling for the conversation transcript.
_ROLE_STYLE: dict[str, str] = {
    "system": "dim",
    "user": "bold cyan",
    "assistant": "bold green",
}


def _conversation_panel(messages: list[dict[str, str]]) -> Panel:
    """Build a panel of the system↔model exchange for the live display.

    Shows each turn so the reader can follow the generate-validate-fix loop: the
    model's attempts stream in live and each fix request carries the validation
    errors. The system prompt (schema + rules) is long and static, so it is
    shown as a one-line summary to keep the live view focused on the exchange.
    """
    body = Text()
    if not messages:
        body.append("(waiting for the model…)", style="dim")
    for index, message in enumerate(messages):
        role = message.get("role", "?")
        content = message.get("content", "")
        if index:
            body.append("\n\n")
        body.append(f"▸ {role}\n", style=_ROLE_STYLE.get(role, "bold"))
        if role == "system":
            body.append(f"({len(content)} chars — schema + translation rules)", style="dim")
        else:
            body.append(content)
    return Panel(
        body,
        title="[bold]Conversation (system ↔ model)[/bold]",
        title_align="left",
        border_style="magenta",
        padding=(1, 2),
    )


def _print_generated(query: str | None, target_language: str) -> None:
    renderable: str | Syntax
    if not query:
        renderable = "[dim italic](no query produced)[/dim italic]"
    else:
        lexer = _PYGMENTS_LEXER.get(target_language, "text")
        renderable = _syntax(query, lexer)

    console.print(
        Panel(
            renderable,
            title=f"[bold]Generated {target_language}[/bold]",
            title_align="left",
            border_style="green",
            padding=(1, 2),
        )
    )


def _print_result(result: TranslationResult) -> None:
    """Bottom-of-output panel: status, iterations, validation, duration, tokens."""
    if result.validation_passed:
        status_cell = "[bold green]✓ success[/bold green]"
        border = "green"
    else:
        status_cell = f"[bold red]✗ {result.status}[/bold red]"
        border = "red"

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", justify="right")
    table.add_column()
    table.add_row("Status:", status_cell)
    table.add_row("Iterations:", str(result.iterations_used))
    table.add_row(
        "Validated:",
        "[green]yes[/green]" if result.validation_passed else "[red]no[/red]",
    )
    table.add_row("Duration:", f"{result.duration_seconds:.2f}s")
    usage = result.token_usage
    total_input = usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens
    table.add_row(
        "Tokens:",
        f"{usage.total_tokens:,}  [dim](in {total_input:,} / out {usage.output_tokens:,})[/dim]",
    )
    if result.unmapped_tables:
        table.add_row("Unmapped:", f"[red]{', '.join(result.unmapped_tables)}[/red]")
    if result.validation_errors:
        bullets = "\n".join(f"• {e}" for e in result.validation_errors)
        table.add_row("Errors:", f"[red]{bullets}[/red]")

    console.print(
        Panel(
            table,
            title="[bold]Result[/bold]",
            title_align="left",
            border_style=border,
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _run_live(translator: AsyncSQLTranslator, sql_query: str) -> TranslationResult:
    """Drive the async translator, streaming the conversation into a live panel.

    The Live region updates in place as the model "types" and each turn is added;
    on exit it leaves the final conversation rendered between the Input SQL and
    Generated panels.
    """
    async with translator:
        with Live(
            _conversation_panel([]),
            console=console,
            refresh_per_second=12,
            vertical_overflow="visible",
        ) as live:
            return await translator.translate(
                sql_query,
                on_conversation=lambda snapshot: live.update(_conversation_panel(snapshot)),
            )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Default is WARNING so the per-iteration INFO logs from the translator
    # do not duplicate what the result panel already shows. `-v` opens them
    # up for users who want to watch the loop progress.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=err_console, show_path=False, rich_tracebacks=True, markup=False)],
    )
    # Silence the graph-DB drivers' own console logging. The validators already
    # capture DB errors and feed them to the LLM (and into the Result panel), so
    # the drivers' redundant warning/error lines are pure noise in the demo.
    for _noisy_logger in ("neo4j", "gremlinpython"):
        logging.getLogger(_noisy_logger).setLevel(logging.CRITICAL)

    sql_query = _read_sql(args.sql)

    mapping = SchemaMapping.from_yaml(args.mapping)
    model_config = _load_model_config_or_die(args)
    server_config = _load_server_config_or_die(args)
    validation_mode = resolve_validation_mode(args.validation, server_config=server_config)

    _print_settings(
        mapping_path=args.mapping,
        mapping=mapping,
        model_path=args.model,
        model_config=model_config,
        target=args.target,
        validation=validation_mode,
        server_path=args.server,
        server_config=server_config,
        max_iterations=args.max_iterations,
    )
    _print_input_sql(sql_query)

    llm = make_async_llm(model_config)
    target = make_target(args.target)
    validator = make_async_validator(args.target, validation_mode, server_config=server_config)
    translator = AsyncSQLTranslator(
        schema_mapping=mapping,
        llm=llm,
        target=target,
        validator=validator,
        max_iterations=args.max_iterations,
    )

    try:
        result = asyncio.run(_run_live(translator, sql_query))
    except RuntimeError as e:
        # e.g. managed mode could not reach the Docker daemon.
        _die(str(e))

    _print_generated(result.generated_query, result.target_language)
    _print_result(result)

    return 0 if result.validation_passed else 1


if __name__ == "__main__":
    sys.exit(main())
