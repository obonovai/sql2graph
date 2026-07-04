"""Environment-variable interpolation for YAML config files.

YAML loaders for model and server configs route the parsed data through
:func:`interpolate_env` before handing it to Pydantic. Strings of the form
``${VAR}`` are substituted with ``os.environ["VAR"]``; missing variables
raise :class:`KeyError` so the failure is loud and immediate rather than a
later authentication error against a real service.

Schema-mapping YAML does not pass through this step: mappings hold no
secrets, and disallowing interpolation there enforces the invariant that
mappings are deployment-invariant.
"""

from __future__ import annotations

import os
import re
from typing import Any

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def interpolate_env(data: Any) -> Any:
    """Walk a parsed-YAML structure, replacing ``${VAR}`` tokens in strings.

    Raises ``KeyError`` if a referenced variable is not present in
    ``os.environ``: fail-fast at load time beats failing later with an
    opaque "authentication failed" from a graph database.
    """
    if isinstance(data, dict):
        return {k: interpolate_env(v) for k, v in data.items()}
    if isinstance(data, list):
        return [interpolate_env(v) for v in data]
    if isinstance(data, str):

        def _repl(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in os.environ:
                raise KeyError(f"Environment variable '${{{var}}}' is referenced in config but not set")
            return os.environ[var]

        return _ENV_RE.sub(_repl, data)
    return data
