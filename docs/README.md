# `docs/`: documentation index

**The reference docs for `sql2graph` - start here.**

New to the project? The top-level [`README.md`](../README.md) has install
instructions and a quick start. This directory holds the deeper references.

| Document | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design rationale: the plain-Python generate-validate-fix loop, `Protocol` extension points, the pre-flight gate, the mapping-builder pipeline, per-query prompt assembly, the async path, and known limitations. |
| [API.md](API.md) | The public Python API surface (every exported symbol) and the three YAML schemas: mapping, model, and server. |
| [MAPPING_BUILDER.md](MAPPING_BUILDER.md) | Generating a first-draft schema mapping from SQL `CREATE TABLE` DDL: the extract → project → LLM-refine pipeline and its preservation guardrail. |
| [SYNTAX_VALIDATION.md](SYNTAX_VALIDATION.md) | The deployment-free, grammar-based (ANTLR) syntax validators; why they replaced the original regex checks; how to regenerate the parsers. |
| [EXTENDING.md](EXTENDING.md) | Step-by-step: add a new target language, or add a new LLM provider. |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common failures (Ollama down, missing API key, Docker, stalls, unmapped input) and how to resolve them. |

Related material outside `docs/`:

- [`../eval/README.md`](../eval/README.md) and [`../eval/METRICS.md`](../eval/METRICS.md) - the evaluation harness (how to run) and the metric methodology (how the numbers are computed).
- Directory READMEs: [`../config/`](../config/README.md), [`../examples/`](../examples/README.md), [`../tests/`](../tests/README.md), and [`../src/sql2graph/validators/grammars/`](../src/sql2graph/validators/grammars/README.md).
