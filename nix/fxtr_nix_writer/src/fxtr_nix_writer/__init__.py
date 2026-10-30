"""fxtr_nix_writer — lib-internal scaffolding for writing fxtr2 layered
file stores from inside nix derivations.

The ``fxtr-nix-writer`` console script (``cli:main``) is invoked by the
shared nix builder ``nix/lib/runFxtrWriter.nix``: it reads the
writer spec from the structured-attrs JSON (``$NIX_ATTRS_JSON_FILE``),
dispatches on the spec's ``kind`` to a writer in ``writers``, and writes a
layered file store to ``$out``. Entity types live in ``entities``.
"""
