"""The documentation must not rot: every source citation and relative link holds.

Runs ``scripts/check_doc_refs.py`` (line citations, symbol citations, relative
markdown links including ``#fragment`` anchors) as part of the default suite,
so ``uv run pytest`` is the single gate that catches a doc pointing at code or
pages that no longer exist. Offline and fast.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_doc_refs_hold(repo_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "check_doc_refs.py")],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"broken doc references:\n{result.stdout}{result.stderr}"
