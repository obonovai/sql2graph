"""rows2graph — LLM-driven SQL → graph query translator.

Public API. The framework exposes three layers:

1. **Schema mapping** — :class:`SchemaMapping`, :class:`NodeMapping`,
   :class:`EdgeMapping`. Describes how a relational schema maps to a
   property-graph model. Loaded from YAML via
   :meth:`SchemaMapping.from_yaml`.
2. **Pluggable components** — :class:`LLMClient`, :class:`TargetLanguage`,
   :class:`QueryValidator` (all :class:`~typing.Protocol`-typed). Concrete
   implementations ship for two LLM backends (Ollama, Anthropic on
   Vertex AI), two target languages (Cypher, AQL), and three validation
   modes (syntax, server, none). Factories
   (:func:`make_llm`, :func:`make_target`, :func:`make_validator`) build
   the components from their typed config objects.
3. **Orchestration** — :class:`SQLTranslator` ties the three components
   together via the generate–validate–fix loop. Returns a typed
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

from rows2graph.events import (
    CompletedEvent,
    EventHandler,
    FixGeneratedEvent,
    GeneratedEvent,
    MaxIterationsReachedEvent,
    TranslationEvent,
    ValidatedEvent,
)
from rows2graph.llm import (
    AnthropicConfig,
    AnthropicLLMClient,
    LLMClient,
    ModelConfig,
    OllamaConfig,
    OllamaLLMClient,
    load_model_config,
    make_llm,
)
from rows2graph.mapping import EdgeMapping, NodeMapping, SchemaMapping
from rows2graph.state import TranslationResult
from rows2graph.targets import AqlTarget, CypherTarget, TargetLanguage, make_target
from rows2graph.translator import SQLTranslator
from rows2graph.validators import (
    AqlServerValidator,
    AqlSyntaxValidator,
    ArangoDBConfig,
    CypherServerValidator,
    CypherSyntaxValidator,
    Neo4jConfig,
    NoopValidator,
    QueryValidator,
    ServerConfig,
    load_server_config,
    make_validator,
)

__all__ = [
    "AnthropicConfig",
    "AnthropicLLMClient",
    "AqlServerValidator",
    "AqlSyntaxValidator",
    "AqlTarget",
    "ArangoDBConfig",
    "CompletedEvent",
    "CypherServerValidator",
    "CypherSyntaxValidator",
    "CypherTarget",
    "EdgeMapping",
    "EventHandler",
    "FixGeneratedEvent",
    "GeneratedEvent",
    "LLMClient",
    "MaxIterationsReachedEvent",
    "ModelConfig",
    "Neo4jConfig",
    "NodeMapping",
    "NoopValidator",
    "OllamaConfig",
    "OllamaLLMClient",
    "QueryValidator",
    "SQLTranslator",
    "SchemaMapping",
    "ServerConfig",
    "TargetLanguage",
    "TranslationEvent",
    "TranslationResult",
    "ValidatedEvent",
    "load_model_config",
    "load_server_config",
    "make_llm",
    "make_target",
    "make_validator",
]
