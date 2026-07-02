"""Make the evaluation harness importable for the eval tests.

``eval_harness`` lives under ``evaluation/`` (added to ``sys.path`` by the
notebooks). Mirror that here - computed from this conftest's own stable location
rather than the test file's - so the pricing tests run under the default
``testpaths=["tests"]`` config without hitting any LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVALUATION = Path(__file__).resolve().parents[2] / "evaluation"
if str(_EVALUATION) not in sys.path:
    sys.path.insert(0, str(_EVALUATION))
