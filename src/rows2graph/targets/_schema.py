"""Shared structure for target-language prompt sections.

Every target's system-prompt section has the same shape: an always-on base
block followed by per-:class:`~rows2graph.sql_features.SqlFeature` rule chunks
gated on the features detected in the input SQL. Historically each target spelled
that out as a free-form ``_BASE_RULES`` string plus a
``dict[SqlFeature, str]``, and the three drifted apart: Gremlin lacked the
"Data model" / "Core syntax" headers and the ``BAD -> GOOD`` anti-pattern format
the other two used, and ``TEMPORAL`` silently existed in Cypher alone because a
missing dict key was indistinguishable from a deliberate omission.

This module makes the schema explicit and uniform:

* :class:`BaseRules` is the always-on block. Its five named sections
  (output mandate, data model, core syntax, anti-patterns, worked examples)
  render in a fixed order with fixed headers, so a target *cannot* quietly skip
  one; the shape is the same for Cypher, AQL, and Gremlin.
* :class:`FeatureRule` is one operation-specific chunk; its ``body`` stays
  free-text (preserving the exact wording empirically tuned per language) but it
  slots into a ``dict[SqlFeature, FeatureRule]`` that a parity test asserts is
  *total* over :class:`~rows2graph.sql_features.SqlFeature`.
* :func:`compose_section` and :func:`extract_query` hold the assembly and
  extraction logic that all three targets previously duplicated verbatim.

The shared SQL example constants (:data:`EX_POINT_LOOKUP_SQL`,
:data:`EX_JOIN_FILTER_SQL`) are the inputs every target's base block shows
translated, so the worked examples are directly comparable across languages.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from rows2graph.sql_features import SqlFeature

# Shared canonical SQL inputs. Every target's base block renders its own
# translation of these same queries, so the examples line up across languages.
EX_POINT_LOOKUP_SQL = "SELECT id, first_name FROM person WHERE id = 933"
EX_JOIN_FILTER_SQL = (
    "SELECT s.name, n.name AS nation FROM supplier s JOIN nation n ON n.nationkey = s.nationkey WHERE s.acctbal > 5000"
)
EX_GROUPED_COUNT_SQL = "SELECT brand, COUNT(*) AS c FROM part GROUP BY brand"

# Match a fenced code block with ANY (or no) info-string after the opening
# ```. The info-string is whatever follows the ``` up to the newline (`aql`,
# `arangodb`, `cypher`, `sql`, or empty), so a model that mislabels the fence
# (e.g. ```arangodb for AQL) still has its body extracted cleanly instead of
# leaking the closing ``` into the query. The first group is the fence body.
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# A closing fence left dangling at the END of a keyword-sliced query, stripped
# on the fallback paths so a stray ``` never reaches the validator.
_TRAILING_FENCE_RE = re.compile(r"\n?[ \t]*```[ \t]*$")


@dataclass(frozen=True)
class Example:
    """One worked ``SQL -> target`` translation shown in the prompt."""

    sql: str
    query: str
    label: str = ""

    def render(self) -> str:
        head = f"- {self.label} (`{self.sql}`):" if self.label else f"- `{self.sql}`:"
        body = "\n".join(f"    {line}" for line in self.query.splitlines())
        return f"{head}\n{body}"


@dataclass(frozen=True)
class AntiPattern:
    """A "what not to do" entry, optionally with a ``BAD -> GOOD`` rewrite.

    ``good is None`` is a pure prohibition ("never do X"); otherwise the entry
    states the fix inline and may carry verbatim ``bad_example`` / ``good_example``
    code lines rendered as aligned ``BAD:`` / ``GOOD:`` blocks.
    """

    bad: str
    good: str | None = None
    bad_example: str | None = None
    good_example: str | None = None

    def render(self) -> str:
        head = f"- {self.bad}" if self.good is None else f"- {self.bad} -> {self.good}"
        lines = [head]
        if self.bad_example is not None:
            lines.append(f"    BAD:  {self.bad_example}")
        if self.good_example is not None:
            lines.append(f"    GOOD: {self.good_example}")
        return "\n".join(lines)


@dataclass(frozen=True)
class BaseRules:
    """The always-on block, identical in shape for every target.

    ``data_model`` and ``core_syntax`` items are full pre-formatted bullet
    strings (each starting with ``- ``, possibly multi-line) so a target can keep
    its language-tuned indentation verbatim; :meth:`render` only supplies the
    fixed section headers and ordering.
    """

    language: str
    output_mandate: str
    data_model: list[str]
    core_syntax: list[str]
    anti_patterns: list[AntiPattern]
    examples: list[Example]

    def render(self) -> str:
        parts: list[str] = [
            self.output_mandate,
            "",
            "Data model:",
            *self.data_model,
            "",
            "Core syntax:",
            *self.core_syntax,
            "",
            f"These are NOT valid {self.language} - never generate them:",
            *(ap.render() for ap in self.anti_patterns),
            "",
            "Examples:",
            *(ex.render() for ex in self.examples),
        ]
        return "\n".join(parts)


@dataclass(frozen=True)
class FeatureRule:
    """One per-feature rule chunk. ``body`` is free-text, language-tuned prose."""

    body: str
    example: Example | None = field(default=None)

    def render(self) -> str:
        if self.example is None:
            return self.body
        return f"{self.body}\n{self.example.render()}"


def compose_section(
    base: BaseRules,
    feature_rules: Mapping[SqlFeature, FeatureRule],
    features: frozenset[SqlFeature],
) -> str:
    """Assemble a target's prompt section: base block + gated feature chunks.

    Chunks are appended in :class:`~rows2graph.sql_features.SqlFeature`
    declaration order for a stable layout across translations. The ``f in
    feature_rules`` guard keeps assembly fail-soft if a chunk is ever missing;
    the parity test (every target's ``feature_rules`` is total over
    ``SqlFeature``) is what turns such a gap into a loud test failure instead.
    """
    chunks = [base.render()]
    chunks += [feature_rules[f].render() for f in SqlFeature if f in features and f in feature_rules]
    return "\n\n".join(chunks)


def extract_query(start_re: re.Pattern[str], llm_response: str) -> str:
    """Pull a query out of (possibly noisy) LLM output.

    Resolution order, shared by every target: (1) any fenced code block
    (:data:`FENCE_RE` accepts any info-string, so a mislabeled fence such as
    ```` ```arangodb ```` is still parsed); (2) the first line that starts with a
    target entry keyword (the per-target ``start_re``); (3) the whole response,
    stripped. On the two fallback paths a dangling closing ```` ``` ```` is
    removed, so a fence the model opened but mistagged never leaks its delimiter
    into the query.
    """
    match = FENCE_RE.search(llm_response)
    if match:
        return match.group(1).strip()

    match = start_re.search(llm_response)
    if match:
        return _TRAILING_FENCE_RE.sub("", llm_response[match.start() :]).strip()

    return _TRAILING_FENCE_RE.sub("", llm_response).strip()
