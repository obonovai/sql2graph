"""rows2graph: LLM-driven SQL → graph query translator.

Public API. The framework exposes three layers:

1. **Schema mapping**: :class:`SchemaMapping`, :class:`NodeMapping`,
   :class:`EdgeMapping`. Describes how a relational schema maps to a
   property-graph model. Loaded from YAML via
   :meth:`SchemaMapping.from_yaml`.
2. **Pluggable components**: :class:`LLMClient`, :class:`TargetLanguage`,
   :class:`QueryValidator` (all :class:`~typing.Protocol`-typed). Concrete
   implementations ship for two LLM backends (Ollama, Anthropic on
   Vertex AI), three target languages (Cypher, AQL, Gremlin), and three
   validation modes (syntax, server, none). Factories
   (:func:`make_llm`, :func:`make_target`, :func:`make_validator`) build
   the components from their typed config objects.
3. **Orchestration**: :class:`SQLTranslator` ties the three components
   together via the generate-validate-fix loop. Returns a typed
   :class:`TranslationResult` per call.

A minimal end-to-end usage::

    from rows2graph import (
        SchemaMapping,
        SQLTranslator,
        load_model_config,
        make_llm,
        make_target,
        make_validator,
    )

    mapping = SchemaMapping.from_yaml("config/mappings/tpch.yaml")
    llm = make_llm(load_model_config("config/models/anthropic.yaml"))
    target = make_target("cypher")
    validator = make_validator("cypher", "syntax")

    with SQLTranslator(mapping, llm, target, validator) as translator:
        result = translator.translate("SELECT name FROM supplier WHERE suppkey = 1337")
        print(result.generated_query)
"""

from rows2graph.async_translator import AsyncSQLTranslator
from rows2graph.events import (
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
from rows2graph.llm import (
    VALID_PROVIDERS,
    AnthropicConfig,
    AnthropicLLMClient,
    AsyncAnthropicLLMClient,
    AsyncLLMClient,
    AsyncOllamaLLMClient,
    LLMClient,
    ModelConfig,
    OllamaConfig,
    OllamaLLMClient,
    StreamCallback,
    load_model_config,
    make_async_llm,
    make_llm,
)
from rows2graph.mapping import EdgeMapping, NodeMapping, SchemaMapping
from rows2graph.preflight import PreflightAction
from rows2graph.sql_features import SqlAnalysis, analyze_sql
from rows2graph.state import TranslationResult
from rows2graph.targets import (
    VALID_TARGETS,
    AqlTarget,
    CypherTarget,
    GremlinTarget,
    TargetLanguage,
    make_target,
)
from rows2graph.translator import SQLTranslator
from rows2graph.validators import (
    TARGET_SERVER_TYPE,
    VALID_VALIDATION_MODES,
    AqlServerValidator,
    ArangoDBConfig,
    AsyncAqlServerValidator,
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
    "AqlTarget",
    "ArangoDBConfig",
    "AsyncAnthropicLLMClient",
    "AsyncAqlServerValidator",
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
    "CompletedEvent",
    "ConversationCallback",
    "CypherServerValidator",
    "CypherSyntaxValidator",
    "CypherTarget",
    "EdgeMapping",
    "EventHandler",
    "FixGeneratedEvent",
    "GeneratedEvent",
    "GremlinConfig",
    "GremlinServerValidator",
    "GremlinSyntaxValidator",
    "GremlinTarget",
    "LLMClient",
    "ManagedServerValidator",
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
    "SQLTranslator",
    "SchemaMapping",
    "ServerConfig",
    "SqlAnalysis",
    "StalledEvent",
    "StreamCallback",
    "TargetLanguage",
    "TranslationEvent",
    "TranslationResult",
    "UnmappedColumnsEvent",
    "UnmappedTablesEvent",
    "ValidatedEvent",
    "analyze_sql",
    "load_model_config",
    "load_server_config",
    "make_async_llm",
    "make_async_validator",
    "make_llm",
    "make_target",
    "make_validator",
    "resolve_validation_mode",
    "valid_modes_for_target",
]
