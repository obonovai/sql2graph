"""Prompt assembly for the generate–validate–fix loop.

Three distinct prompts shape every translation:

1. **System prompt** (:func:`build_system_prompt`): establishes the LLM's
   role, embeds the user-provided schema mapping as a structured
   human-readable block, enumerates target-language-specific translation
   rules, and constrains output format ("only valid query code, no markdown,
   no commentary").
2. **Generate prompt** (:func:`build_generate_prompt`): the user-turn that
   initiates a translation. Deliberately short — the schema and rules already
   live in the system prompt.
3. **Fix prompt** (:func:`build_fix_prompt`): user-turn appended on each
   validate-fail iteration. Includes the failing query and the validator's
   error list; instructs the model to fix *only* those errors.

Keeping the three prompts as separate function calls — each producing a plain
``str`` — lets the loop accumulate them as chat messages without any framework
machinery. The LLM sees the entire history on each call, which is the
property the feedback loop relies on for iterative refinement.
"""

from __future__ import annotations

from rows2graph.mapping import SchemaMapping
from rows2graph.targets import TargetLanguage


def format_schema_context(schema: SchemaMapping) -> str:
    """Render a schema mapping as a human-readable Markdown-ish block.

    The LLM consumes this block directly inside the system prompt. The format
    is deliberately verbose — explicit node labels, primary keys, property
    mappings, and edge directions — so the LLM can refer back to it without
    having to reconstruct the graph topology from terse identifiers.
    """
    lines: list[str] = []

    lines.append("### Nodes")
    for node in schema.nodes:
        lines.append(f"- **{node.label}** (from table `{node.source_table}`)")
        lines.append(f"  Primary key: `{node.primary_key}`")
        lines.append("  Properties:")
        for graph_prop, sql_col in node.properties.items():
            lines.append(f"    - `{graph_prop}` <- SQL column `{sql_col}`")

    lines.append("")
    lines.append("### Relationships (Edges)")
    for edge in schema.edges:
        lines.append(f"- **[:{edge.type}]** from `{edge.source_node}` to `{edge.target_node}`")
        lines.append(f"  Join: `{edge.source_table}.{edge.source_foreign_key}` -> `{edge.target_primary_key}`")
        if edge.properties:
            lines.append("  Properties:")
            for graph_prop, sql_col in edge.properties.items():
                lines.append(f"    - `{graph_prop}` <- SQL column `{sql_col}`")

    return "\n".join(lines)


def build_system_prompt(schema: SchemaMapping, target: TargetLanguage) -> str:
    """Assemble the full system prompt.

    The target-language-specific section is delegated to the
    :class:`~rows2graph.targets.TargetLanguage` implementation, keeping
    language-specific syntactic rules close to their corresponding extractor
    and validator.
    """
    schema_context = format_schema_context(schema)
    language = target.name
    return f"""You are an expert database query translator. Your task is to translate SQL queries \
into {language} queries for a graph database.

## Graph Schema

The target graph database has the following schema:

{schema_context}

## Rules
- Translate the SQL query semantics faithfully into {language}.
- Use the node labels and relationship types EXACTLY as defined in the schema above.
- Use the graph property names (not the SQL column names) in the generated query.
- Map SQL JOINs to relationship traversals.
- Map SQL WHERE clauses to filter predicates in {language}.
- Map SQL aggregations (GROUP BY, COUNT, SUM, etc.) to {language} equivalents.
- Your response MUST contain ONLY valid {language} code.
- Do NOT include explanations, markdown code blocks, or any non-query text.

## {language.upper()}-specific guidance

{target.system_prompt_section()}"""


def build_generate_prompt(sql_query: str) -> str:
    """Build the initial user-turn that asks for a translation."""
    return f"Translate this SQL query into a graph database query:\n\n{sql_query}"


def build_fix_prompt(
    sql_query: str,
    generated_query: str,
    errors: list[str],
) -> str:
    """Build the user-turn appended after a failed validation.

    Includes the original SQL, the failing generated query, and a bulleted
    list of validator errors. The instruction "Fix ONLY the reported errors"
    is intentional: without it, low-temperature models tend to restructure
    the entire query on each retry, undoing partial progress.
    """
    errors_text = "\n".join(f"- {e}" for e in errors)
    return f"""The following query was generated from a SQL query but failed validation.

Original SQL:
{sql_query}

Generated query:
{generated_query}

Validation errors:
{errors_text}

Fix ONLY the reported errors. Do not change the query structure unnecessarily.
Your response MUST contain ONLY valid query code."""
