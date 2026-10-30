"""Layer-builder entrypoints for the publishing demo (run by ``mkFxtrFnBuilder``).

Each ``build`` reads its parent layers through ``ctx.readers`` and pins a derived
entity. All three run ``provenanced = true``, so every pinned entity carries the
nix build as provenance (a layered ``(data, prov)`` id) with no per-script work —
the writer is opened in provenanced mode and ``ctx.scope`` already holds the
build's provenance context.

- :mod:`publishing_builders.postprocess` — notes → ``Stats``.
- :mod:`publishing_builders.subreport` — notes + stats → MDX ``SubReport``.
- :mod:`publishing_builders.report` — notes + stats + subreport → MDX ``RootReport``.
"""

from __future__ import annotations

#: The seeded note pin keys, shared by the builders and ``seed_data.py`` /
#: ``flake.nix`` (the preload + namespace-export keys must match these).
NOTE_KEYS: tuple[str, ...] = ("note_0", "note_1", "note_2")

__all__ = ["NOTE_KEYS"]
