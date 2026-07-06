"""The generate-validate-fix orchestration loop (architecture Layer 3).

This package holds the framework's core feedback loop and its direct support
modules: the sync/async translators, the loop's internal state and public
result, the typed iteration events, the input-side pre-flight gate, and the
per-iteration prompt assembly.

It is deliberately kept as a *thin* package: this ``__init__`` intentionally
re-exports nothing, so importing a single submodule (e.g.
``sql2graph.engine.events``, which :mod:`sql2graph.mapping_builder` depends on)
does not eagerly pull the translator's heavier dependency graph
(``validators`` / ``llm``) through the package initializer. The public API is
re-exported from the top-level :mod:`sql2graph` package instead.
"""
