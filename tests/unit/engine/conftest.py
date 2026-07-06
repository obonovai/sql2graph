"""Translator-only fixtures.

The scripted fake-LLM factory fixtures (``scripted_llm`` / ``scripted_async_llm``)
live in ``tests/unit/conftest.py`` so they are visible across the whole unit
tree. This conftest adds only ``spy_analyze_sql``, which is specific to the
translator dialect-forwarding tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture
def spy_analyze_sql(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], list[str | None]]:
    """Install a spy over ``analyze_sql`` on the given module, returning the recorded dialects.

    Delegates to the real implementation and returns the accumulating list, so a
    test can assert the translator forwarded its constructor ``dialect`` into the
    single pre-flight parse without depending on any sqlglot-version dialect quirk.
    Patches the name as looked up on the *consumer* module (the translator), which
    is why the module to patch is passed in rather than patching ``sql_features``.
    """

    def _install(module: Any) -> list[str | None]:
        seen: list[str | None] = []
        real = module.analyze_sql

        def spy(sql_query: str, *, dialect: str | None = None) -> Any:
            seen.append(dialect)
            return real(sql_query, dialect=dialect)

        monkeypatch.setattr(module, "analyze_sql", spy)
        return seen

    return _install
