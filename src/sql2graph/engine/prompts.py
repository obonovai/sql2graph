"""Prompt assembly for the generate-validate-fix loop.

Three distinct prompts shape every translation:

1. **System prompt** (:func:`build_system_prompt`): establishes the LLM's
   role, embeds the user-provided schema mapping as a structured
   human-readable block, enumerates target-language-specific translation
   rules, and constrains output format ("only valid query code, no markdown,
   no commentary").
2. **Generate prompt** (:func:`build_generate_prompt`): the user-turn that
   initiates a translation. Deliberately short: the schema and rules already
   live in the system prompt.
3. **Fix prompt** (:func:`build_fix_prompt`): user-turn appended on each
   validate-fail iteration. Includes the failing query and the validator's
   error list; instructs the model to fix *only* those errors.

Keeping the three prompts as separate function calls (each producing a plain
``str``) lets the loop accumulate them as chat messages without any framework
machinery. The LLM sees the entire history on each call, which is the
property the feedback loop relies on for iterative refinement.
"""

from __future__ import annotations

import re

from sql2graph.mapping import SchemaMapping, SemanticType
from sql2graph.sql_features import SqlFeature
from sql2graph.targets import TargetLanguage

# Generic rule one-liners that previously sat in a fixed block. Each is now
# gated on a detected SQL feature so the prompt does not mention, e.g.,
# JOINs on a single-table query. Schema/label/output-format rules stay
# always-on (see ``_ALWAYS_ON_RULES`` below) because they govern the LLM's
# output regardless of input shape.
_GENERIC_FEATURE_RULES: dict[SqlFeature, str] = {
    SqlFeature.JOIN: "- Map SQL JOINs to relationship traversals.",
    SqlFeature.AGGREGATION: "- Map SQL aggregations (GROUP BY, COUNT, SUM, etc.) to {language} equivalents.",
}


def _type_suffix(semantic: SemanticType | None) -> str:
    """Render a property's declared type as `` (datetime)``, or empty when untyped.

    The empty case keeps the schema block byte-for-byte identical to before the
    type feature, so untyped mappings (and their prompt snapshots) are unchanged.
    """
    return f" ({semantic.value})" if semantic is not None else ""


def _key_str(columns: list[str]) -> str:
    """Render key columns for the prompt: one backticked column, or a parenthesised
    comma-separated tuple of backticked columns for a composite key.

    A single-column key reproduces the previous byte-for-byte output, so untyped
    single-column mappings and their prompt snapshots stay unchanged.
    """
    if len(columns) == 1:
        return f"`{columns[0]}`"
    return "(" + ", ".join(f"`{c}`" for c in columns) + ")"


def _join_str(source_table: str, fks: list[str], pks: list[str]) -> str:
    """Render an edge join, pairing columns positionally.

    ``t.fk -> pk`` for a single column; ``t.a -> pk_a AND t.b -> pk_b`` for a
    composite join, so the LLM knows every column pair must match.
    """
    return " AND ".join(f"`{source_table}.{fk}` -> `{pk}`" for fk, pk in zip(fks, pks, strict=True))


def format_schema_context(schema: SchemaMapping) -> str:
    """Render a schema mapping as a human-readable Markdown-ish block.

    The LLM consumes this block directly inside the system prompt. The format
    is deliberately verbose (explicit node labels, primary keys, property
    mappings, and edge directions) so the LLM can refer back to it without
    having to reconstruct the graph topology from terse identifiers. When a
    property declares a :class:`~sql2graph.mapping.SemanticType`, it is shown in
    parentheses (e.g. ``(datetime)``) so the target rules can pick a constructor
    deterministically instead of guessing the type from a literal's shape.
    """
    lines: list[str] = []

    lines.append("### Nodes")
    for node in schema.nodes:
        lines.append(f"- **{node.label}** (from table `{node.source_table}`)")
        lines.append(f"  Primary key: {_key_str(node.primary_key)}")
        lines.append("  Properties:")
        for graph_prop, sql_col in node.properties.items():
            suffix = _type_suffix(node.property_types.get(graph_prop))
            lines.append(f"    - `{graph_prop}` <- SQL column `{sql_col}`{suffix}")
        if node.list_properties:
            lines.append("  List properties (multi-valued, stored as a list on the node):")
            for graph_prop, lp in node.list_properties.items():
                suffix = _type_suffix(lp.type)
                lines.append(f"    - `{graph_prop}` (list) <- SQL column `{lp.source_table}.{lp.column}`{suffix}")

    lines.append("")
    lines.append("### Relationships (Edges)")
    for edge in schema.edges:
        lines.append(f"- **[:{edge.type}]** from `{edge.source_node}` to `{edge.target_node}`")
        lines.append(f"  Join: {_join_str(edge.source_table, edge.source_foreign_key, edge.target_primary_key)}")
        if edge.properties:
            lines.append("  Properties:")
            for graph_prop, sql_col in edge.properties.items():
                suffix = _type_suffix(edge.property_types.get(graph_prop))
                lines.append(f"    - `{graph_prop}` <- SQL column `{sql_col}`{suffix}")

    return "\n".join(lines)


def build_system_prompt(
    schema: SchemaMapping,
    target: TargetLanguage,
    features: frozenset[SqlFeature],
) -> str:
    """Assemble the full system prompt.

    *features* is the set of :class:`~sql2graph.sql_features.SqlFeature`
    values detected in the SQL being translated; it is forwarded to the
    target so it can pick the matching per-operation rule chunks, and also
    used here to gate the generic one-liners (JOIN, aggregation) that
    previously appeared unconditionally.

    The target-language-specific section is delegated to the
    :class:`~sql2graph.targets.TargetLanguage` implementation, keeping
    language-specific syntactic rules close to their corresponding extractor
    and validator.
    """
    schema_context = format_schema_context(schema)
    language = target.name

    # These always-on lines are deliberately lean: the graph-property-naming
    # rule and the "only valid code, no markdown" mandate are already stated in
    # every target's base block (its `data_model` and `output_mandate`), so
    # repeating them here would pay the same tokens twice on every translation.
    # What stays is the semantics/label/WHERE framing that the base blocks do
    # not redundantly cover.
    generic_rules = [
        f"- Translate the SQL query semantics faithfully into {language}.",
        "- CRITICAL: Use ONLY the node labels and relationship types listed in the '### Nodes' "
        "and '### Relationships (Edges)' sections above. Never output a label or relationship "
        "type that is not defined there, even if another name seems more natural for the domain.",
        "- The names in the guidance examples below (e.g. `LabelA`, `[:REL_AB]`) are placeholders "
        "for illustration only and are NOT part of this schema; substitute the schema's real names.",
        f"- Map SQL WHERE clauses to filter predicates in {language}.",
        *(
            _GENERIC_FEATURE_RULES[feat].format(language=language)
            for feat in SqlFeature
            if feat in features and feat in _GENERIC_FEATURE_RULES
        ),
    ]
    rules_block = "\n".join(generic_rules)

    return f"""You are an expert database query translator. Your task is to translate SQL queries \
into {language} queries for a graph database.

## Graph Schema

The target graph database has the following schema:

{schema_context}

## Rules
{rules_block}

## {language.upper()}-specific guidance

{target.system_prompt_section(features)}"""


def build_generate_prompt(sql_query: str) -> str:
    """Build the initial user-turn that asks for a translation."""
    return f"Translate this SQL query into a graph database query:\n\n{sql_query}"


def build_fix_prompt(
    sql_query: str,
    generated_query: str,
    errors: list[str],
    *,
    repair_hint: str | None = None,
) -> str:
    """Build the user-turn appended after a failed validation.

    Includes the original SQL, the failing generated query, and a bulleted
    list of validator errors.

    The default closing instruction ("Fix ONLY the reported errors. Do not
    change the query structure unnecessarily.") is intentional: without it,
    low-temperature models tend to restructure the entire query on each retry,
    undoing partial progress. But for some errors that advice is actively
    wrong: a clause-*ordering* error can only be fixed by a restructure. When
    the target supplies a ``repair_hint`` for the given errors (see
    :meth:`sql2graph.targets.TargetLanguage.repair_hint`), it *replaces* the
    default line, licensing the restructure the validator's terse message
    discourages.
    """
    errors_text = "\n".join(f"- {e}" for e in errors)
    guidance = (
        repair_hint
        if repair_hint is not None
        else "Fix ONLY the reported errors. Do not change the query structure unnecessarily."
    )
    return f"""The following query was generated from a SQL query but failed validation.

Original SQL:
{sql_query}

Generated query:
{generated_query}

Validation errors:
{errors_text}

{guidance}
Your response MUST contain ONLY valid query code."""


def build_escalation_prompt(
    sql_query: str,
    generated_query: str,
    errors: list[str],
    *,
    repair_hint: str | None = None,
) -> str:
    """Build the user-turn for a *stall-breaking escalation* retry.

    The loop sends this on a deliberately **fresh** context (system prompt +
    this turn only), discarding the accumulated fix history. That history is
    what poisons a stalled retry: it contains several copies of the same
    rejected query and the same error, and at low temperature the model simply
    reproduces them. Restating the problem self-containedly (plus an explicit
    "your previous attempts were identical and rejected; produce something
    DIFFERENT") combined with a higher sampling temperature at the call site,
    is what breaks the repetition fixed point.
    """
    errors_text = "\n".join(f"- {e}" for e in errors)
    hint = f"\n\n{repair_hint}" if repair_hint else ""
    return f"""Your previous attempts to translate this SQL kept producing the SAME query and \
were REJECTED with the SAME error. Do NOT repeat that query. It is wrong. Rethink the \
approach and produce a DIFFERENT, restructured query.

Original SQL:
{sql_query}

Rejected query (do not repeat it):
{generated_query}

Validation errors:
{errors_text}{hint}

Output ONLY the corrected query: no prose, no explanation."""


def normalize_query(query: str) -> str:
    """Collapse whitespace so trivial re-indentation doesn't read as a change.

    Used by the loop's stall detector: a model that merely re-indents its
    previous (still-broken) answer has made no real progress.
    """
    return re.sub(r"\s+", " ", query.strip())


def error_signature(errors: list[str]) -> frozenset[str]:
    """Reduce a validator error list to a position-independent signature.

    Two validations "say the same thing" when this signature is equal, even if
    the byte text differs in volatile details. For database validators that
    emit an ``[ERR ####]`` code (e.g. ArangoDB) the codes alone are the
    signature; otherwise each message is normalized by stripping quoted
    near-text and digits (line/column positions) and lowercasing. This lets the
    loop notice "same error two iterations running" (the hallmark of a stall)
    without being fooled by a position that shifts from ``4:3`` to ``4:1``.
    """
    sig: set[str] = set()
    for err in errors:
        codes = re.findall(r"ERR\s*\d+", err, re.IGNORECASE)
        if codes:
            sig.update(c.upper().replace(" ", "") for c in codes)
            continue
        norm = re.sub(r"'[^']*'", "", err)  # drop quoted "near '...'" snippets
        norm = re.sub(r"\d+", "", norm)  # drop line/column numbers
        norm = re.sub(r"\s+", " ", norm).strip().lower()
        sig.add(norm)
    return frozenset(sig)
