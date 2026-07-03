"""Guard the two hand-maintained model lists against drifting apart.

``harness.config.RUN_MATRIX`` names the models under evaluation and
``harness.pricing.MODEL_PRICING`` names their USD rates. Ollama models
intentionally fall through to the free provider default, but an Anthropic
model missing from MODEL_PRICING would be silently billed at the Opus-class
default - plausible-looking, possibly wrong cost numbers.
"""

from __future__ import annotations

from harness.config import RUN_MATRIX
from harness.pricing import MODEL_PRICING


def test_anthropic_matrix_models_have_explicit_pricing() -> None:
    for rc in RUN_MATRIX:
        if rc.provider == "anthropic":
            assert rc.model in MODEL_PRICING, (
                f"{rc.model} is in RUN_MATRIX but has no MODEL_PRICING entry; "
                "its cost would silently use the provider default"
            )


def test_matrix_is_ldbc_only() -> None:
    # The eval is scoped to LDBC (the TPC-H gold set was dropped).
    assert {rc.dataset for rc in RUN_MATRIX} == {"ldbc"}
