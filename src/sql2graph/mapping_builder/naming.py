"""Turn relational identifiers into graph names: node labels and edge types.

These heuristics are deliberately *structural* and dependency-free (no
inflection library - see the package rationale). They produce correct,
deterministic, if sometimes clunky, names: ``region`` becomes ``Region``,
``orders`` becomes ``Order``, a foreign key ``regionkey -> region`` becomes
``HAS_REGION``. Polishing these into idiomatic, readable graph names is the job
of the LLM refinement pass, which may rename labels and edge types freely; the
structural names are the always-available baseline it starts from.
"""

from __future__ import annotations

import re

from sql2graph.mapping_builder.relational import ForeignKey

# Irregular plurals worth special-casing; everything else falls to the rules in
# :func:`_singularize`. ``data``/``news``-style words are handled by the
# stop-set and suffix guards rather than listed here.
_IRREGULAR_PLURALS: dict[str, str] = {
    "people": "person",
    "children": "child",
    "men": "man",
    "women": "woman",
    "feet": "foot",
    "teeth": "tooth",
    "geese": "goose",
    "mice": "mouse",
    "indices": "index",
    "matrices": "matrix",
    "vertices": "vertex",
}

# Words that end in ``s`` (or ``ies``) but are already singular: stripping them
# would mangle the name. Checked before the suffix rules.
_NON_PLURAL: frozenset[str] = frozenset({"series", "species", "news", "data", "media"})

# Split points for PascalCase: underscores, spaces, other non-alphanumerics, and
# lowercase→uppercase camelCase boundaries.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_FK_SUFFIXES_UNDERSCORE = ("_id", "_fk", "_key")
_FK_SUFFIXES_BARE = ("id", "key")


def table_to_label(table_name: str) -> str:
    """Map a table name to a graph node label (singularized PascalCase).

    Only the final ``_``-delimited token is singularized, so compound names keep
    their structure: ``line_items`` -> ``LineItem``, ``orders`` -> ``Order``,
    ``region`` -> ``Region``. A name with no underscore is left for the LLM to
    split (``lineitem`` -> ``Lineitem``).
    """
    return _pascal_case(_singularize_last_token(table_name))


def edge_type_for_fk(fk: ForeignKey, *, target_label: str) -> str:
    """Derive a structural relationship type from a foreign key.

    The FK column usually carries the semantics, so it drives the name with its
    key suffix stripped (``moderator_person_id`` -> ``MODERATOR_PERSON``). When
    the column adds nothing beyond the target (``regionkey`` referencing
    ``region``), fall back to ``HAS_<TARGET>`` (-> ``HAS_REGION``).
    """
    base = _strip_fk_suffix(fk.columns[0])
    if not base or base.casefold() in {fk.ref_table.casefold(), "id", "key", "fk"}:
        return f"HAS_{_screaming(target_label)}"
    return _screaming(base)


def junction_to_edge_type(junction_table: str) -> str:
    """Derive a structural relationship type from a junction table name.

    ``knows`` -> ``KNOWS``, ``study_at`` -> ``STUDY_AT``,
    ``forum_has_member`` -> ``FORUM_HAS_MEMBER`` (the LLM trims these).
    """
    return _screaming(junction_table)


def _singularize_last_token(name: str) -> str:
    """Singularize only the last ``_``-delimited token, preserving the rest."""
    parts = name.split("_")
    parts[-1] = _singularize(parts[-1])
    return "_".join(parts)


def _singularize(token: str) -> str:
    """Best-effort singular of one word token (rule-ordered, dependency-free)."""
    low = token.casefold()
    if low in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[low]
    if low in _NON_PLURAL:
        return token
    if low.endswith(("ss", "us", "is")):  # address, status, analysis, bus
        return token
    if low.endswith("ies") and len(low) > 3:  # categories -> category
        return token[:-3] + "y"
    if low.endswith(("ses", "xes", "zes", "ches", "shes")):  # boxes -> box
        return token[:-2]
    if low.endswith("s") and not low.endswith("ss"):  # orders -> order
        return token[:-1]
    return token


def _pascal_case(identifier: str) -> str:
    """Join an identifier's tokens into PascalCase (``line_item`` -> ``LineItem``).

    Falls back to the original identifier when every character is non-ASCII or a
    separator (e.g. a Cyrillic/CJK table name, or ``"_"``), so the result is never
    empty - an empty label would fail the ``NonBlankStr`` model constraint. This
    mirrors the ``or identifier.upper()`` guard in :func:`_screaming`.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", identifier)
    tokens = [t for t in _NON_ALNUM.split(spaced) if t]
    return "".join(t[:1].upper() + t[1:].lower() for t in tokens) or identifier


def _screaming(identifier: str) -> str:
    """Convert an identifier to ``SCREAMING_SNAKE_CASE`` (``LineItem`` -> ``LINE_ITEM``)."""
    spaced = _CAMEL_BOUNDARY.sub(" ", identifier)
    tokens = [t for t in _NON_ALNUM.split(spaced) if t]
    return "_".join(t.upper() for t in tokens) or identifier.upper()


def _strip_fk_suffix(column: str) -> str:
    """Strip a trailing key suffix (``_id``/``_fk``/``_key``/``id``/``key``)."""
    low = column.casefold()
    for suffix in _FK_SUFFIXES_UNDERSCORE:
        if low.endswith(suffix) and len(low) > len(suffix):
            return column[: len(column) - len(suffix)]
    for suffix in _FK_SUFFIXES_BARE:
        if low.endswith(suffix) and len(low) > len(suffix):
            return column[: len(column) - len(suffix)]
    return column
