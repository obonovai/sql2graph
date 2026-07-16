"""sql2graph: LLM-driven SQL → graph query translator.

Public API. The framework exposes four layers:

1. **Schema mapping**: :class:`SchemaMapping`, :class:`NodeMapping`,
   :class:`EdgeMapping`. Describes how a relational schema maps to a
   property-graph model. Loaded from YAML via
   :meth:`SchemaMapping.from_yaml`.
2. **Pluggable components**: :class:`LLMClient`, :class:`TargetLanguage`,
   :class:`QueryValidator` (all :class:`~typing.Protocol`-typed). Concrete
   implementations ship for two LLM backends (Ollama, Anthropic via the
   direct API), three target languages (Cypher, AQL, Gremlin), and four
   validation modes (none, syntax, server, and auto-provisioned managed).
   Factories (:func:`make_llm`, :func:`make_target`, :func:`make_validator`)
   build the components from their typed config objects.
3. **Orchestration**: :class:`SQLTranslator` (and its async sibling
   :class:`AsyncSQLTranslator`) ties the three components together via the
   generate-validate-fix loop. Returns a typed :class:`TranslationResult`
   per call.
4. **Mapping builder**: :func:`build_mapping` (and
   :func:`build_mapping_async`) bootstrap a first-draft
   :class:`SchemaMapping` from SQL ``CREATE TABLE`` DDL, so the layer-1
   mapping need not be written entirely by hand. See ``docs/mapping/builder.md``.

A minimal end-to-end usage::

    from sql2graph import (
        SchemaMapping,
        SQLTranslator,
        load_model_config,
        make_llm,
        make_target,
        make_validator,
    )

    mapping = SchemaMapping.from_yaml("examples/mappings/tpch.yaml")
    llm = make_llm(load_model_config("config/models/anthropic.yaml"))
    target = make_target("cypher")
    validator = make_validator("cypher", "syntax")

    with SQLTranslator(mapping, llm, target, validator) as translator:
        result = translator.translate("SELECT name FROM supplier WHERE suppkey = 1337")
        print(result.generated_query)
"""

from sql2graph.engine.async_translator import AsyncSQLTranslator
from sql2graph.engine.events import (
    CompletedEvent,
    ConversationCallback,
    EventHandler,
    FixGeneratedEvent,
    GeneratedEvent,
    MaxIterationsReachedEvent,
    ParseFailedEvent,
    StalledEvent,
    TranslationEvent,
    UnmappedColumnsEvent,
    UnmappedTablesEvent,
    ValidatedEvent,
)
from sql2graph.engine.preflight import PreflightAction, find_unmapped_columns, find_unmapped_tables
from sql2graph.engine.state import TranslationResult
from sql2graph.engine.translator import SQLTranslator
from sql2graph.llm import (
    VALID_PROVIDERS,
    AnthropicConfig,
    AnthropicLLMClient,
    AsyncAnthropicLLMClient,
    AsyncLLMClient,
    AsyncOllamaLLMClient,
    ChatReply,
    LLMClient,
    ModelConfig,
    OllamaConfig,
    OllamaLLMClient,
    StreamCallback,
    TokenUsage,
    load_model_config,
    make_async_llm,
    make_llm,
)
from sql2graph.mapping import EdgeMapping, ListProperty, NodeMapping, SchemaMapping, SemanticType
from sql2graph.mapping_builder import (
    BuildResult,
    CoverageReport,
    DdlParseError,
    MappingDiff,
    RelationalSchema,
    RenameDiff,
    build_mapping,
    build_mapping_async,
    diff_mappings,
    extract_schema_from_ddl,
    mapping_to_yaml,
    project_to_mapping,
)
from sql2graph.sql_features import SqlAnalysis, analyze_sql
from sql2graph.targets import (
    VALID_TARGETS,
    AqlTarget,
    CypherTarget,
    GremlinTarget,
    TargetLanguage,
    make_target,
)
from sql2graph.validators import (
    TARGET_SERVER_TYPE,
    VALID_VALIDATION_MODES,
    AqlServerValidator,
    AqlSyntaxValidator,
    ArangoDBConfig,
    AsyncAqlServerValidator,
    AsyncAqlSyntaxValidator,
    AsyncCypherServerValidator,
    AsyncCypherSyntaxValidator,
    AsyncGremlinServerValidator,
    AsyncGremlinSyntaxValidator,
    AsyncManagedServerValidator,
    AsyncNoopValidator,
    AsyncQueryValidator,
    CypherServerValidator,
    CypherSyntaxValidator,
    GremlinConfig,
    GremlinServerValidator,
    GremlinSyntaxValidator,
    ManagedServerValidator,
    Neo4jConfig,
    NoopValidator,
    QueryValidator,
    ServerConfig,
    load_server_config,
    make_async_validator,
    make_validator,
    resolve_validation_mode,
    valid_modes_for_target,
)

__all__ = [
    "TARGET_SERVER_TYPE",
    "VALID_PROVIDERS",
    "VALID_TARGETS",
    "VALID_VALIDATION_MODES",
    "AnthropicConfig",
    "AnthropicLLMClient",
    "AqlServerValidator",
    "AqlSyntaxValidator",
    "AqlTarget",
    "ArangoDBConfig",
    "AsyncAnthropicLLMClient",
    "AsyncAqlServerValidator",
    "AsyncAqlSyntaxValidator",
    "AsyncCypherServerValidator",
    "AsyncCypherSyntaxValidator",
    "AsyncGremlinServerValidator",
    "AsyncGremlinSyntaxValidator",
    "AsyncLLMClient",
    "AsyncManagedServerValidator",
    "AsyncNoopValidator",
    "AsyncOllamaLLMClient",
    "AsyncQueryValidator",
    "AsyncSQLTranslator",
    "BuildResult",
    "ChatReply",
    "CompletedEvent",
    "ConversationCallback",
    "CoverageReport",
    "CypherServerValidator",
    "CypherSyntaxValidator",
    "CypherTarget",
    "DdlParseError",
    "EdgeMapping",
    "EventHandler",
    "FixGeneratedEvent",
    "GeneratedEvent",
    "GremlinConfig",
    "GremlinServerValidator",
    "GremlinSyntaxValidator",
    "GremlinTarget",
    "LLMClient",
    "ListProperty",
    "ManagedServerValidator",
    "MappingDiff",
    "MaxIterationsReachedEvent",
    "ModelConfig",
    "Neo4jConfig",
    "NodeMapping",
    "NoopValidator",
    "OllamaConfig",
    "OllamaLLMClient",
    "ParseFailedEvent",
    "PreflightAction",
    "QueryValidator",
    "RelationalSchema",
    "RenameDiff",
    "SQLTranslator",
    "SchemaMapping",
    "SemanticType",
    "ServerConfig",
    "SqlAnalysis",
    "StalledEvent",
    "StreamCallback",
    "TargetLanguage",
    "TokenUsage",
    "TranslationEvent",
    "TranslationResult",
    "UnmappedColumnsEvent",
    "UnmappedTablesEvent",
    "ValidatedEvent",
    "analyze_sql",
    "build_mapping",
    "build_mapping_async",
    "diff_mappings",
    "extract_schema_from_ddl",
    "find_unmapped_columns",
    "find_unmapped_tables",
    "load_model_config",
    "load_server_config",
    "make_async_llm",
    "make_async_validator",
    "make_llm",
    "make_target",
    "make_validator",
    "mapping_to_yaml",
    "project_to_mapping",
    "resolve_validation_mode",
    "valid_modes_for_target",
]
