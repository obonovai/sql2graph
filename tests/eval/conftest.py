"""Make the evaluation harness importable for the eval tests.

``harness`` lives under ``eval/`` (added to ``sys.path`` by the notebooks).
Mirror that here - computed from this conftest's own stable location rather
than the test file's - so these tests run under the default
``testpaths=["tests"]`` config without hitting any LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))
