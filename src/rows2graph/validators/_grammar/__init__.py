"""Grammar-based parsing support for the deployment-free syntax validators.

:mod:`errors` holds the shared ANTLR parse routine; :mod:`generated` holds the
committed Python lexers/parsers generated from the vendored ``.g4`` grammars in
``rows2graph/validators/grammars`` (regenerate with ``scripts/generate_parsers.sh``).
"""
