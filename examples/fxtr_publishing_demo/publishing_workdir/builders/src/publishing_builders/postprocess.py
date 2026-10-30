"""Postprocess builder: aggregate the seeded notes into a ``Stats`` entity.

Pure builder — reads only its parent layers (the note preloads), no remote. The
notes arrive **bare** (the tsv-namespace / preload flow strips provenance); the
``Stats`` it pins is **provenanced** (``provenanced = true`` on the flake side),
so reading it back yields a layered ``(data, prov)`` id.
"""

from __future__ import annotations

from fxtr2.data.layered_file_store.nix_builder import BuilderContext

from demo_types import Note, Stats
from publishing_builders import NOTE_KEYS


async def build(ctx: BuilderContext) -> None:
    notes: list[Note] = []
    for key in NOTE_KEYS:
        ref = await ctx.readers[key].retrieve(key, Note)
        notes.append(await ref.load(ctx.scope))

    total_words = sum(len(n.title.split()) + len(n.body.split()) for n in notes)
    longest_title = max((n.title for n in notes), key=len, default="")
    stats = Stats(
        note_count=len(notes),
        total_words=total_words,
        longest_title=longest_title,
    )

    ref = ctx.scope.store(stats)
    await ctx.writer.pin_to_this_layer("stats", ref.entity_id, ctx.scope)
