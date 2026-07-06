"""Grammar-based parsing support for the deployment-free syntax validators.

:mod:`runtime` holds the shared ANTLR parse routine; :mod:`generated` holds the
committed Python lexers/parsers generated from the vendored ``.g4`` grammar
sources in ``sql2graph/validators/_grammar/sources`` (regenerate with
``scripts/generate_parsers.sh``).
"""
