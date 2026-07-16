#!/usr/bin/env python3
"""Validate the documentation against the actual source tree.

Three checks, all offline and fast:

1. Line citations, written ``path/to/file.py:LINE`` (or ``:LINE-LINE``): cheap
   to write but they rot when code moves, so any citation whose line runs past
   the target file's end fails.
2. Symbol citations, written ``path/to/file.py::symbol``: rot-proof, failing
   only if the named ``def`` / ``class`` / module-level assignment disappears.
   Prefer this form for high-traffic references.
3. Relative markdown links, written ``[text](target)`` or
   ``[text](target#fragment)``: the target file (or directory) must exist, and
   a fragment must match a real heading of the target under GitHub's heading
   slugification.

A citation whose file cannot be resolved (a path with a directory component
that matches nothing) is also a failure. Bare ambiguous basenames matching
several files are skipped with a note rather than failed, since they cannot be
disambiguated. Run this in the default test suite (tests/unit/test_doc_refs.py)
or directly:

    uv run python scripts/check_doc_refs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
DOC_FILES = [
    *sorted((REPO / "docs").rglob("*.md")),
    REPO / "README.md",
    REPO / "config" / "README.md",
    REPO / "examples" / "README.md",
    REPO / "tests" / "README.md",
    REPO / "eval" / "README.md",
    REPO / "eval" / "METRICS.md",
    REPO / "src" / "sql2graph" / "validators" / "_grammar" / "sources" / "README.md",
]

# ``foo/bar.py:12`` or ``foo/bar.py:12-20`` (not preceded by a word/path char).
LINE_REF = re.compile(r"(?<![\w./])((?:\.\./)?[\w./-]*?[\w-]+\.py):(\d+)(?:-(\d+))?")
# ``foo/bar.py::SYMBOL``.
SYMBOL_REF = re.compile(r"(?<![\w./])((?:\.\./)?[\w./-]*?[\w-]+\.py)::(\w+)")
# ``[text](target)``; target must not contain whitespace or a closing paren.
MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
INLINE_CODE = re.compile(r"`[^`]*`")
FENCE = re.compile(r"^\s*(```|~~~)")


def resolve(path_str: str) -> tuple[Path | None, str]:
    """Resolve a cited path to a source file. Returns (path, status)."""
    rel = path_str.lstrip("./")
    candidates = (
        REPO / path_str,
        REPO / rel,
        SRC / "sql2graph" / rel,
        REPO / "eval" / rel,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate, "ok"
    tail = [m for m in SRC.rglob(Path(rel).name) if str(m).replace("\\", "/").endswith(rel)]
    if len(tail) == 1:
        return tail[0], "ok"
    if len(tail) > 1:
        return None, "ambiguous"
    has_dir = "/" in rel
    matches = list(SRC.rglob(Path(rel).name))
    if not has_dir and len(matches) > 1:
        return None, "ambiguous"
    return None, "missing"


def symbol_defined(path: Path, symbol: str) -> bool:
    pattern = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(symbol)}\b|^\s*{re.escape(symbol)}\s*[:=]")
    return any(pattern.search(line) for line in path.read_text().splitlines())


def github_slug(heading: str, seen: dict[str, int]) -> str:
    """Slugify a heading the way GitHub's renderer does.

    Lowercase; drop everything that is not a word character, space, or hyphen;
    turn spaces into hyphens; suffix ``-N`` on repeats within one file.
    """
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
    n = seen.get(slug, 0)
    seen[slug] = n + 1
    return slug if n == 0 else f"{slug}-{n}"


def heading_slugs(path: Path) -> set[str]:
    """All heading anchors GitHub would generate for a markdown file."""
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text().splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            slugs.add(github_slug(re.sub(r"[`*]", "", m.group(1)), seen))
    return slugs


def check_links(doc: Path, errors: list[str]) -> None:
    """Validate every relative markdown link (and fragment) in one file."""
    in_fence = False
    for lineno, text in enumerate(doc.read_text().splitlines(), start=1):
        if FENCE.match(text):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in MD_LINK.finditer(INLINE_CODE.sub("", text)):
            raw = m.group(1)
            if raw.startswith(("http://", "https://", "mailto:")):
                continue
            target_part, _, fragment = raw.partition("#")
            if target_part:
                target = (doc.parent / target_part).resolve()
                if not target.exists():
                    errors.append(f"{doc.relative_to(REPO)}:{lineno}: broken link '{raw}'")
                    continue
            else:
                target = doc  # a same-page '#fragment' link
            if fragment and target.suffix == ".md":
                if fragment not in heading_slugs(target):
                    errors.append(
                        f"{doc.relative_to(REPO)}:{lineno}: link '{raw}' names no heading in {target.relative_to(REPO)}"
                    )


def check_citations(doc: Path, errors: list[str]) -> int:
    """Validate line and symbol citations in one file. Returns skipped count."""
    skipped = 0
    for lineno, text in enumerate(doc.read_text().splitlines(), start=1):
        for m in LINE_REF.finditer(text):
            path_str, start, end = m.group(1), int(m.group(2)), m.group(3)
            target, status = resolve(path_str)
            if status == "ambiguous":
                skipped += 1
                continue
            if target is None:
                errors.append(f"{doc.relative_to(REPO)}:{lineno}: unresolved file '{path_str}'")
                continue
            n_lines = len(target.read_text().splitlines())
            hi = int(end) if end else start
            if hi > n_lines:
                errors.append(
                    f"{doc.relative_to(REPO)}:{lineno}: '{path_str}:{m.group(2)}"
                    f"{'-' + end if end else ''}' past EOF ({target.relative_to(REPO)} has {n_lines} lines)"
                )
        for m in SYMBOL_REF.finditer(text):
            path_str, symbol = m.group(1), m.group(2)
            target, status = resolve(path_str)
            if status == "ambiguous":
                skipped += 1
                continue
            if target is None:
                errors.append(f"{doc.relative_to(REPO)}:{lineno}: unresolved file '{path_str}'")
            elif not symbol_defined(target, symbol):
                errors.append(
                    f"{doc.relative_to(REPO)}:{lineno}: symbol '{symbol}' not defined in {target.relative_to(REPO)}"
                )
    return skipped


def main() -> int:
    errors: list[str] = []
    skipped = 0
    for doc in DOC_FILES:
        if not doc.is_file():
            errors.append(f"doc file listed in DOC_FILES is missing: {doc.relative_to(REPO)}")
            continue
        skipped += check_citations(doc, errors)
        check_links(doc, errors)

    for e in errors:
        print(e)
    note = f" ({skipped} ambiguous bare-basename refs skipped)" if skipped else ""
    print(f"\n{'FAIL' if errors else 'OK'}: {len(errors)} broken doc reference(s){note}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
