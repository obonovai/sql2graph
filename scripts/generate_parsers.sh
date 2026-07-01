#!/usr/bin/env bash
#
# Regenerate the ANTLR Python parsers from the vendored grammars.
#
# Dev-time only: requires Java (any JDK 11+) and the ANTLR 4.13.x "complete" jar.
# End users and CI need NEITHER: the generated parsers are committed under
# src/rows2graph/validators/_grammar/generated/, and only the pure-Python
# `antlr4-python3-runtime` is required at runtime.
#
# Usage:
#   scripts/generate_parsers.sh
#   ANTLR_JAR=/path/to/antlr-4.13.2-complete.jar JAVA=/path/to/java scripts/generate_parsers.sh
#
# Resolution order for the ANTLR tool:
#   1. $ANTLR_JAR via $JAVA (or `java` on PATH)
#   2. the local Maven cache (~/.m2/.../antlr4-<version>-complete.jar)
#   3. an `antlr4` command on PATH (e.g. from `antlr4-tools`)
#
# The tool version MUST match the antlr4-python3-runtime pin in pyproject.toml.
set -euo pipefail

ANTLR_VERSION="${ANTLR_VERSION:-4.13.2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRAMMARS="$HERE/src/rows2graph/validators/grammars"
OUT="$HERE/src/rows2graph/validators/_grammar/generated"

JAVA_BIN="${JAVA:-java}"
ANTLR_JAR="${ANTLR_JAR:-$HOME/.m2/repository/org/antlr/antlr4/$ANTLR_VERSION/antlr4-$ANTLR_VERSION-complete.jar}"

run_antlr() {
  if [[ -f "$ANTLR_JAR" ]] && command -v "$JAVA_BIN" >/dev/null 2>&1; then
    "$JAVA_BIN" -jar "$ANTLR_JAR" "$@"
  elif command -v antlr4 >/dev/null 2>&1; then
    antlr4 "$@"
  else
    echo "error: no ANTLR tool found." >&2
    echo "  Set ANTLR_JAR=/path/to/antlr-$ANTLR_VERSION-complete.jar (and JAVA=/path/to/java)," >&2
    echo "  or install antlr4-tools so an 'antlr4' command is on PATH." >&2
    exit 1
  fi
}

# Generate from inside the grammars dir with bare filenames so the generated
# files' "# Generated from <path>" header stays relative (no absolute local path).
echo "Generating Cypher parser (Cypher25)..."
rm -rf "$OUT/cypher"; mkdir -p "$OUT/cypher"
( cd "$GRAMMARS" && run_antlr -Dlanguage=Python3 -no-listener -no-visitor -lib . -o "$OUT/cypher" \
  Cypher25Lexer.g4 Cypher25Parser.g4 )

echo "Generating Gremlin parser..."
rm -rf "$OUT/gremlin"; mkdir -p "$OUT/gremlin"
( cd "$GRAMMARS" && run_antlr -Dlanguage=Python3 -no-listener -no-visitor -o "$OUT/gremlin" \
  Gremlin.g4 )

echo "Generating AQL parser..."
rm -rf "$OUT/aql"; mkdir -p "$OUT/aql"
( cd "$GRAMMARS" && run_antlr -Dlanguage=Python3 -no-listener -no-visitor -lib . -o "$OUT/aql" \
  AQLLexer.g4 AQLParser.g4 )

# Keep only the runtime-required .py files; drop ANTLR dev artifacts.
find "$OUT" -type f \( -name '*.interp' -o -name '*.tokens' \) -delete

# Python 3.12 compatibility shim. The grammars carry a few Java-typed rule
# arguments/returns (e.g. `parameter[String paramType]`); the ANTLR Python
# target emits them as real annotations (`paramType:String`), which Python
# evaluates at definition time on 3.12 -> NameError. `from __future__ import
# annotations` (PEP 563) makes every annotation a lazy string, so the undefined
# Java types are never evaluated. Prepended as the first line of each parser.
while IFS= read -r -d '' f; do
  tmp="$(mktemp)"
  printf 'from __future__ import annotations\n' >"$tmp"
  cat "$f" >>"$tmp"
  mv "$tmp" "$f"
done < <(find "$OUT" -type f -name '*.py' ! -name '__init__.py' -print0)

# Restore package markers.
printf '' > "$OUT/__init__.py"
printf '' > "$OUT/cypher/__init__.py"
printf '' > "$OUT/gremlin/__init__.py"
printf '' > "$OUT/aql/__init__.py"

echo "Done. Generated parsers under $OUT"
